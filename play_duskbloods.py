"""Play Duskbloods: a small launcher for The Duskbloods network test on Eglantine-based emulators.

Starts the loopback-only stand-in servers (bundled in the exe) and the chosen emulator GUI
(Eglantine eglantine.exe, Lichii lichii.exe, Eden eden.exe, yuzu yuzu.exe) with the game.
Nothing is sent anywhere but 127.0.0.1. Settings are kept in launcher_settings.json next to it.
"""
import hashlib
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

PORT = 18443
WEB_PORT = 18090
GAME_SHA = '2ef7b7bcd78905f30069d03aa44e1e124d83bd83bdcc1766bd3d01ea2ad137a3'
GAME_FOLDER = 'THE DUSKBLOODS NETWORK TEST VER'
EMULATOR_NAMES = ('eglantine.exe', 'lichii.exe', 'eden.exe', 'yuzu.exe')
FROZEN = getattr(sys, 'frozen', False)

HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
BUNDLE = Path(getattr(sys, '_MEIPASS', '')) if FROZEN else Path(__file__).resolve().parent
TOOLS = BUNDLE / 'tools' if (BUNDLE / 'tools').exists() else HERE / 'tools'
ASSETS = BUNDLE / 'assets'
SERVER = TOOLS / 'npln_server'
PKI_SOURCE_NOTE = 'generated on first run'
FRONT = TOOLS / 'dusk_server'
SETTINGS = HERE / 'launcher_settings.json'


def safe_is_file(path):
    try:
        return Path(path).is_file()
    except OSError:  # protected or unreadable locations (for example Windows app folders)
        return False


def safe_is_dir(path):
    try:
        return Path(path).is_dir()
    except OSError:
        return False


# --- Game and emulator discovery ------------------------------------------------------------

def is_game(main_file):
    try:
        return main_file.is_file() and hashlib.sha256(main_file.read_bytes()).hexdigest() == GAME_SHA
    except OSError:
        return False


def game_main_from(path):
    if not path:
        return None
    path = Path(path)
    for candidate in (path, path / 'base/exefs/main', path / 'exefs/main',
                      path / GAME_FOLDER / 'base/exefs/main'):
        if is_game(candidate):
            return candidate
    return None


def default_game_folders():
    folders = [Path(os.environ['DUSK_GAME'])] if os.environ.get('DUSK_GAME') else []
    for parent in (HERE, *HERE.parents[:3]):
        folders.append(parent / GAME_FOLDER)
    return folders


def emulator_exe_in(folder):
    for name in EMULATOR_NAMES:
        if safe_is_file(Path(folder) / name):
            return Path(folder) / name
    return None


def default_emulators():
    """Emulator executables near the launcher: its own folder, sibling build folders, the
    workspace's builds and a Lichii folder next to the workspace."""
    found = []
    roots = [HERE, HERE.parent]
    for parent in HERE.parents[:3]:
        roots += [parent / 'DB-Eglantine Windows Builds', parent / 'build-msvc/bin', parent / 'Lichii']
    roots.append(Path.home() / 'Downloads/Lichii')
    for root in roots:
        if not safe_is_dir(root):
            continue
        if (exe := emulator_exe_in(root)):
            found.append(exe)
        try:
            children = sorted((p for p in root.iterdir() if safe_is_dir(p)), reverse=True)
        except OSError:
            continue
        for child in children:
            if (exe := emulator_exe_in(child)):
                found.append(exe)
    unique = []
    for exe in found:
        if exe.resolve() not in [u.resolve() for u in unique]:
            unique.append(exe)
    return unique


def emulator_label(exe):
    kind = {'eglantine.exe': 'Eglantine', 'lichii.exe': 'Lichii', 'eden.exe': 'Eden',
            'yuzu.exe': 'yuzu'}.get(exe.name.lower(), exe.stem)
    return f'{kind}  -  {exe.parent.name}'


def data_dir_for(exe):
    """Portable builds keep their data in <folder>/user; Lichii and Eden use %APPDATA%\\eden."""
    portable = exe.parent / 'user'
    if safe_is_dir(portable):
        return portable
    appdata = Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming'))
    for name in ('eden', 'lichii', 'eglantine', 'yuzu'):
        if safe_is_dir(appdata / name):
            return appdata / name
    return appdata / 'eden'


def test_dir_for(data):
    return data / 'test_data' if safe_is_dir(data / 'test_data') else data / 'duskbloods_test'


# --- Launch ---------------------------------------------------------------------------------

def python_command(script):
    if FROZEN:
        return [sys.executable, '--run-script', str(script)]
    return [sys.executable, '-u', str(script)]


def set_key(text, key, value):
    pattern = re.compile(rf'(?m)^{re.escape(key)}=.*$')
    if pattern.search(text):
        text = pattern.sub(lambda _: f'{key}={value}', text)
        text = re.sub(rf'(?m)^{re.escape(key)}\\default=(true|false)$',
                      lambda _: f'{key}\\default=false', text)
    return text


class Session:
    """One play session: servers + emulator. Messages go to `log`."""

    def __init__(self, game_main, emulator, log):
        self.game_main = game_main
        self.emulator = emulator
        self.log = log
        self.data = data_dir_for(emulator)
        # Packaged builds keep their saves in user/test_data; elsewhere use a folder of our own.
        self.test = test_dir_for(self.data)
        self.processes = []
        self.run_dir = None

    def check_keys(self):
        keys = self.data / 'keys'
        if (keys / 'prod.keys').exists():
            return True
        workspace_keys = [p / 'build-msvc/bin/user/keys' for p in HERE.parents[:3]]
        for source in workspace_keys:
            if (source / 'prod.keys').exists():
                keys.mkdir(parents=True, exist_ok=True)
                for name in ('prod.keys', 'title.keys'):
                    if (source / name).exists():
                        shutil.copyfile(source / name, keys / name)
                self.log(f'Copied your keys into {keys}')
                return True
        keys.mkdir(parents=True, exist_ok=True)
        self.log(f'Put your own prod.keys and title.keys in:\n  {keys}', error=True)
        return False

    def seed(self):
        nand, save = self.test / 'nand', self.test / 'save'
        seed = next((p / 'seed' for p in (self.emulator.parent, HERE) if safe_is_dir(p / 'seed')), None)
        if not nand.exists():
            if seed and (seed / 'nand').exists():
                shutil.copytree(seed / 'nand', nand)
            else:
                nand.mkdir(parents=True)
            cache = nand / 'system/save/bcat/0400D1D02399A000/Regulation'
            cache.mkdir(parents=True, exist_ok=True)
            regulation = self.game_main.parents[1] / 'romfs/regulation_nt.bin'
            if regulation.exists():
                shutil.copyfile(regulation, cache / 'regulation_nt_1_0.bin')
        if not save.exists():
            if seed and (seed / 'save').exists():
                shutil.copytree(seed / 'save', save)
            else:
                save.mkdir(parents=True)
        return nand, save

    def prepare_config(self, nand, save):
        config = self.data / 'config/qt-config.ini'
        if not config.exists():
            self.log('No qt-config.ini yet: the emulator starts with its defaults '
                     '(start it once on its own, then play from here).')
            return
        text = config.read_text(encoding='utf-8')
        for key, value in (('nand_directory', nand.as_posix()), ('save_directory', save.as_posix()),
                           ('airplane_mode', 'false'), ('use_gdbstub', 'false'),
                           ('check_for_updates', 'false')):
            text = set_key(text, key, value)
        config.write_text(text, encoding='utf-8')

    def start(self):
        if not self.check_keys():
            return None
        nand, save = self.seed()
        self.prepare_config(nand, save)
        self.run_dir = self.test / 'runs' / time.strftime('%Y%m%d_%H%M%S')
        run = self.run_dir
        (run / 'server_data').mkdir(parents=True)
        no_window = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        pki = self.test / 'pki'
        if not (pki / 'ca.der').exists():
            self.log('Creating private test certificates for this PC (first run)...')
            made = subprocess.run(python_command(SERVER / 'make_pki.py') + ['--out', str(pki)],
                                  cwd=SERVER, capture_output=True, creationflags=no_window)
            if made.returncode != 0 or not (pki / 'ca.der').exists():
                self.log('Could not create the test certificates.', error=True)
                return None
        self.log('Starting the local servers (127.0.0.1 only)...')
        server = subprocess.Popen(
            python_command(SERVER / 'npln_stub.py') +
            ['--port', str(PORT + 1), '--host', '127.0.0.1', '--web-port', str(WEB_PORT),
             '--data', str(run / 'server_data'), '--pki', str(pki),
             '--log', str(run / 'server.log')],
            cwd=SERVER, stdout=(run / 'server_console.log').open('w'), stderr=subprocess.STDOUT,
            creationflags=no_window)
        front_env = dict(os.environ, DUSK_REDIRECT_VALUE='nochange', DUSK_FRONTEND='1',
                         PYTHONUNBUFFERED='1')
        front = subprocess.Popen(
            python_command(FRONT / 'frontdoor.py') +
            ['--port', str(PORT), '--upstream', str(PORT + 1), '--ca-dir', str(pki),
             '--cert-dir', str(self.test / 'frontdoor_pki'), '--log', str(run / 'frontdoor.log')],
            cwd=FRONT, env=front_env, stdout=(run / 'frontdoor_console.log').open('w'),
            stderr=subprocess.STDOUT, creationflags=no_window)
        self.processes = [front, server]
        time.sleep(3)
        if server.poll() is not None or front.poll() is not None:
            self.log(f'A local server stopped at start; see {run}', error=True)
            self.stop()
            return None
        env = {k: v for k, v in os.environ.items() if not k.startswith(('EDEN_NX2_', 'EDEN_NATIVE_'))}
        env.update(EDEN_NX2_DIAG_CODEC_CLIENT_REPLY='1', EDEN_NX2_EXPERIMENTAL_CODEC_DECODE='1',
                   EDEN_NX2_EXPERIMENTAL_SYSMIX_OUTPUT='1', EDEN_NX2_SYSMIX_DRAIN_MULTIPLIER='1',
                   EDEN_NX2_APP_POOL_PROBE='1152', EDEN_NX2_GPU_MAP_FLAG_PROBE='1',
                   EDEN_NX2_SILENT_BAD_OPUS='1', EDEN_NX2_LOOPBACK_ONLY='1',
                   EDEN_NX2_ONLINE_HOSTS='npln.srv.nintendo.net,npln.nintendo.net,n.n.srv.nintendo.net,'
                                         'p.srv.nintendo.net,fromsoftware-game.net',
                   EDEN_NX2_ONLINE_SERVER=f'127.0.0.1:{PORT}',
                   EDEN_NX2_ONLINE_CA=str(pki / 'ca.der'),
                   EDEN_NX2_CRASH_DUMP=str(run), EDEN_NX2_TRACE_TERMINATE='1')
        env.update({k: v for k, v in os.environ.items() if k.startswith('EDEN_NX2_') and k not in env})
        env.pop('SDL_AUDIODRIVER', None)
        console = (run / 'console.log').open('w')
        game = subprocess.Popen([str(self.emulator), '-g', str(self.game_main)],
                                cwd=self.emulator.parent, env=env, stdout=console,
                                stderr=subprocess.STDOUT)
        self.processes.insert(0, game)
        self.log(f'{emulator_label(self.emulator)} started. Logs: {run}')
        return game

    def stop(self):
        for proc in self.processes[1:] if self.processes else []:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        log = self.data / 'log/eden_log.txt'
        if self.run_dir and log.exists():
            try:
                shutil.copyfile(log, self.run_dir / 'emulator.log')
            except OSError:
                pass
        self.processes = []


# --- GUI ------------------------------------------------------------------------------------

def load_settings():
    try:
        return json.loads(SETTINGS.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    try:
        SETTINGS.write_text(json.dumps(settings, indent=2), encoding='utf-8')
    except OSError:
        pass


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, ttk

    BG = '#0d0b0c'
    PANEL = '#1a1314'
    EDGE = '#6b3d3a'
    GOLD = '#d8c8b8'
    RUST = '#9b3b22'
    TEXT = '#d8cfc4'
    DIM = '#8c8077'
    SERIF = 'Georgia'

    settings = load_settings()
    root = tk.Tk()
    root.title('The Duskbloods - Network Test Launcher')
    root.configure(bg=BG)
    root.resizable(False, False)
    if (ASSETS / 'icon.ico').exists():
        try:
            root.iconbitmap(default=str(ASSETS / 'icon.ico'))
        except tk.TclError:
            pass

    header = tk.Frame(root, bg='#3a2328', highlightthickness=1, highlightbackground='#5a3a3a')
    header.pack(fill='x', padx=6, pady=(6, 0))
    tk.Label(header, text='Network Test Launcher', font=(SERIF, 15), fg='#e6d9cc', bg='#3a2328',
             anchor='w', padx=22, pady=7).pack(fill='x')
    if (ASSETS / 'banner.png').exists():
        banner_image = tk.PhotoImage(file=str(ASSETS / 'banner.png'))
        tk.Label(root, image=banner_image, bg=BG, bd=0).pack()
        root.banner_image = banner_image
    else:
        tk.Label(root, text='THE DUSKBLOODS', font=(SERIF, 30), fg=RUST, bg=BG).pack(pady=20)

    def ornament(parent):
        line = tk.Frame(parent, bg=EDGE, height=1)
        line.pack(fill='x', padx=28, pady=(6, 10))

    body = tk.Frame(root, bg=BG)
    body.pack(fill='both', padx=28)

    style = ttk.Style()
    style.theme_use('clam')
    style.configure('Dusk.TCombobox', fieldbackground=PANEL, background=PANEL, foreground=TEXT,
                    arrowcolor=GOLD, bordercolor=EDGE, lightcolor=PANEL, darkcolor=PANEL)
    style.map('Dusk.TCombobox', fieldbackground=[('readonly', PANEL)], foreground=[('readonly', TEXT)],
              selectbackground=[('readonly', PANEL)], selectforeground=[('readonly', GOLD)])
    root.option_add('*TCombobox*Listbox.background', PANEL)
    root.option_add('*TCombobox*Listbox.foreground', TEXT)
    root.option_add('*TCombobox*Listbox.selectBackground', RUST)
    root.option_add('*TCombobox*Listbox.font', (SERIF, 10))

    def section(title):
        tk.Label(body, text=title, font=(SERIF, 13), fg=GOLD, bg='#2a1c1e', anchor='w', padx=12, pady=3,
                 highlightthickness=1, highlightbackground=EDGE).pack(fill='x', pady=(4, 0))
        frame = tk.Frame(body, bg=BG)
        frame.pack(fill='x', pady=(2, 2))
        return frame

    def button(parent, text, command, primary=False):
        b = tk.Button(parent, text=text, command=command, font=(SERIF, 10 if not primary else 15),
                      fg=TEXT if not primary else '#f3e6d0', bg=PANEL if not primary else RUST,
                      activebackground='#5a2416' if primary else EDGE, activeforeground=GOLD,
                      relief='flat', bd=0, padx=14 if not primary else 40, pady=4 if not primary else 8,
                      highlightthickness=1, highlightbackground=EDGE, cursor='hand2')
        return b

    # Game folder.
    game_frame = section('Game')
    game_var = tk.StringVar(value=settings.get('game', ''))
    if not game_main_from(game_var.get()):
        for folder in default_game_folders():
            if game_main_from(folder):
                game_var.set(str(folder))
                break
    game_entry = tk.Entry(game_frame, textvariable=game_var, font=(SERIF, 10), bg=PANEL, fg=TEXT,
                          insertbackground=GOLD, relief='flat', highlightthickness=1,
                          highlightbackground=EDGE, highlightcolor=GOLD)
    game_entry.pack(side='left', fill='x', expand=True, ipady=4)
    game_status = tk.Label(body, text='', font=(SERIF, 9), fg=DIM, bg=BG, anchor='w')

    def browse_game():
        chosen = filedialog.askdirectory(title='Select the THE DUSKBLOODS NETWORK TEST VER folder')
        if chosen:
            game_var.set(chosen)
            refresh()

    button(game_frame, 'Browse', browse_game).pack(side='left', padx=(8, 0))
    game_status.pack(fill='x')
    ornament(body)

    # Emulator.
    emu_frame = section('Emulator')
    emulators = default_emulators()
    saved_emulator = settings.get('emulator')
    if saved_emulator and safe_is_file(saved_emulator) and \
            Path(saved_emulator).resolve() not in [e.resolve() for e in emulators]:
        emulators.insert(0, Path(saved_emulator))
    labels = [emulator_label(e) for e in emulators]
    emu_var = tk.StringVar()
    combo = ttk.Combobox(emu_frame, textvariable=emu_var, values=labels, state='readonly',
                         style='Dusk.TCombobox', font=(SERIF, 10))
    combo.pack(side='left', fill='x', expand=True, ipady=3)
    if saved_emulator and safe_is_file(saved_emulator):
        for exe, label in zip(emulators, labels):
            if exe.resolve() == Path(saved_emulator).resolve():
                emu_var.set(label)
    if not emu_var.get() and labels:
        own = emulator_exe_in(HERE)
        emu_var.set(emulator_label(own) if own else labels[0])
    emu_status = tk.Label(body, text='', font=(SERIF, 9), fg=DIM, bg=BG, anchor='w')

    def browse_emulator():
        chosen = filedialog.askopenfilename(
            title='Select the emulator (eglantine.exe, lichii.exe, eden.exe, yuzu.exe)',
            filetypes=[('Emulator', ' '.join(EMULATOR_NAMES)), ('Programs', '*.exe')])
        if chosen:
            exe = Path(chosen)
            if exe.resolve() not in [e.resolve() for e in emulators]:
                emulators.insert(0, exe)
                labels.insert(0, emulator_label(exe))
                combo.configure(values=labels)
            emu_var.set(emulator_label(exe))
            refresh()

    button(emu_frame, 'Browse', browse_emulator).pack(side='left', padx=(8, 0))
    emu_status.pack(fill='x')
    ornament(body)

    def selected_emulator():
        label = emu_var.get()
        for exe, item in zip(emulators, labels):
            if item == label:
                return exe
        return None

    # Status and buttons.
    log_box = tk.Text(body, height=7, font=('Consolas', 9), bg=PANEL, fg=TEXT, relief='flat',
                      highlightthickness=1, highlightbackground=EDGE, wrap='word', state='disabled')
    log_box.pack(fill='x', pady=(0, 10))
    log_box.tag_configure('error', foreground='#e0745a')

    def log(message, error=False):
        def append():
            log_box.configure(state='normal')
            log_box.insert('end', message + '\n', 'error' if error else '')
            log_box.see('end')
            log_box.configure(state='disabled')
        root.after(0, append)

    buttons = tk.Frame(root, bg=BG)
    buttons.pack(pady=(0, 16))
    state = {'session': None}

    def refresh(*_):
        main = game_main_from(game_var.get())
        game_status.configure(text='The Duskbloods network test found' if main else
                              'Not found: choose the folder that holds base/exefs/main',
                              fg='#8fae7a' if main else '#e0745a')
        exe = selected_emulator()
        if exe:
            data = data_dir_for(exe)
            keys = safe_is_file(data / 'keys/prod.keys')
            emu_status.configure(text=f'{exe}\nData: {data}' + ('' if keys else '   (no prod.keys yet)'),
                                 fg=DIM if keys else '#e0745a')
        else:
            emu_status.configure(text='No emulator selected', fg='#e0745a')
        running = state['session'] is not None
        play.configure(state='disabled' if running or not main or not exe else 'normal',
                       text='Playing...' if running else 'PLAY')

    def play_clicked():
        main = game_main_from(game_var.get())
        exe = selected_emulator()
        if not main or not exe:
            return
        settings.update(game=game_var.get(), emulator=str(exe))
        save_settings(settings)
        session = Session(main, exe, log)
        state['session'] = session
        refresh()

        def worker():
            try:
                game = session.start()
                if game is not None:
                    game.wait()
                    log(f'Game closed (exit {game.returncode:#x}).')
            except Exception as error:  # report anything to the window
                log(f'Launch failed: {error}', error=True)
            finally:
                session.stop()
                log('Local servers stopped.')
                state['session'] = None
                root.after(0, refresh)

        threading.Thread(target=worker, daemon=True).start()

    def open_logs():
        exe = selected_emulator()
        folder = test_dir_for(data_dir_for(exe)) / 'runs' if exe else HERE
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))

    play = button(buttons, 'PLAY', play_clicked, primary=True)
    play.pack(side='left', padx=8)
    button(buttons, 'Open logs', open_logs).pack(side='left', padx=8)
    tk.Label(root, text='Local test servers on 127.0.0.1 only  -  your keys and game are never sent anywhere',
             font=(SERIF, 8), fg=DIM, bg=BG).pack(pady=(0, 10))

    game_var.trace_add('write', refresh)
    combo.bind('<<ComboboxSelected>>', refresh)
    refresh()
    log('Choose the game folder and an emulator, then press PLAY.')

    def on_close():
        session = state['session']
        if session is not None:
            for proc in session.processes[:1]:
                proc.terminate()
            session.stop()
        root.destroy()

    root.protocol('WM_DELETE_WINDOW', on_close)
    root.mainloop()


def run_script():
    """`--run-script <file.py> args...`: run a bundled server script with this interpreter."""
    script = Path(sys.argv[2])
    sys.argv = [str(script)] + sys.argv[3:]
    sys.path.insert(0, str(script.parent))
    os.chdir(script.parent)
    runpy.run_path(str(script), run_name='__main__')


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == '--run-script':
        run_script()
    else:
        run_gui()
