"""Play Duskbloods: a small launcher for The Duskbloods network test on Eglantine-based emulators.

Starts the stand-in servers (bundled in the exe) and the chosen emulator GUI (Eglantine
eglantine.exe, Lichii lichii.exe, Eden eden.exe, yuzu yuzu.exe) with the game. Offline, nothing
leaves 127.0.0.1. Online rooms: one player hosts the servers and friends with the same room
password join them (only a hash of the password is shared). Settings are kept in
launcher_settings.json next to it.
"""
import hashlib
import ipaddress
import json
import os
import re
import runpy
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path


def _tolerant(method):
    """Unreadable folders (e.g. R:\\WpSystem -> WinError 1337) count as 'not there' instead of
    crashing the launcher while it scans drives for emulators."""
    def check(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except (OSError, ValueError):
            return False
    return check


for _name in ('exists', 'is_file', 'is_dir'):
    setattr(Path, _name, _tolerant(getattr(Path, _name)))

VERSION = '1.1.0'
PORT = 18443
ROOM_OFFSET = 2          # online play: the room service listens two ports above the game port
WEB_PORT = 18090
GAME_SHA = '2ef7b7bcd78905f30069d03aa44e1e124d83bd83bdcc1766bd3d01ea2ad137a3'
GAME_FOLDER = 'THE DUSKBLOODS NETWORK TEST VER'
IS_WINDOWS = os.name == 'nt'
EMULATOR_KINDS = ('eglantine', 'lichii', 'eden', 'yuzu')
EMULATOR_NAMES = tuple(f'{kind}.exe' for kind in EMULATOR_KINDS) if IS_WINDOWS else EMULATOR_KINDS
FROZEN = getattr(sys, 'frozen', False)

HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
BUNDLE = Path(getattr(sys, '_MEIPASS', '')) if FROZEN else Path(__file__).resolve().parent
TOOLS = BUNDLE / 'tools' if (BUNDLE / 'tools').exists() else HERE / 'tools'
ASSETS = BUNDLE / 'assets'
SERVER = TOOLS / 'npln_server'
PKI_SOURCE_NOTE = 'generated on first run'
FRONT = TOOLS / 'dusk_server'
SETTINGS = HERE / 'launcher_settings.json'
ONLINE_GUIDE = 'https://github.com/sikoGD/playDB-CNT/blob/main/ONLINE.md'


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
    folder = Path(folder)
    for name in EMULATOR_NAMES:
        if (folder / name).is_file():
            return folder / name
    if not IS_WINDOWS:  # AppImages are named like Eglantine-x86_64.AppImage
        try:
            for item in sorted(folder.iterdir()):
                if item.name.lower().endswith('.appimage') and \
                        item.name.lower().startswith(EMULATOR_KINDS) and item.is_file():
                    return item
        except OSError:
            pass
    return None


def default_emulators():
    """Emulator executables near the launcher: its own folder, sibling build folders, the
    workspace's builds and a Lichii folder next to the workspace."""
    found = []
    roots = [HERE, HERE.parent]
    for parent in HERE.parents[:3]:
        roots += [parent / 'DB-Eglantine Windows Builds', parent / 'build-msvc/bin', parent / 'Lichii']
    roots.append(Path.home() / 'Downloads/Lichii')
    if not IS_WINDOWS:
        roots += [Path.home() / 'Applications', Path.home() / '.local/bin', Path.home() / 'Downloads']
        found += [Path(path) for path in (shutil.which(name) for name in EMULATOR_KINDS) if path]
    for root in roots:
        if not root.is_dir():
            continue
        if (exe := emulator_exe_in(root)):
            found.append(exe)
        try:
            children = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)
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
    name = exe.name.lower()
    kind = next((k.capitalize() if k != 'yuzu' else k for k in EMULATOR_KINDS if name.startswith(k)),
                exe.stem)
    return f'{kind}  -  {exe.parent.name}'


def data_dir_for(exe):
    """Portable builds keep their data in <folder>/user; Lichii and Eden use %APPDATA%\\eden."""
    portable = exe.parent / 'user'
    if portable.is_dir():
        return portable
    if IS_WINDOWS:
        appdata = Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming'))
    else:  # Linux: ~/.local/share/<emulator>
        appdata = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    for name in ('eden', 'lichii', 'eglantine', 'yuzu'):
        if (appdata / name).is_dir():
            return appdata / name
    return appdata / 'eden'


def config_file_for(data):
    """qt-config.ini: inside a portable `user` folder, else (Linux) ~/.config/<emulator>."""
    config = data / 'config/qt-config.ini'
    if config.exists() or IS_WINDOWS:
        return config
    base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    for name in (data.name, 'eden', 'lichii', 'eglantine', 'yuzu'):
        if (base / name / 'qt-config.ini').exists():
            return base / name / 'qt-config.ini'
    return config


def open_folder(folder):
    if IS_WINDOWS:
        os.startfile(str(folder))
    else:
        subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(folder)])


def test_dir_for(data):
    return data / 'test_data' if (data / 'test_data').is_dir() else data / 'duskbloods_test'


# --- Launch ---------------------------------------------------------------------------------

def _reuse(sock):
    """Linux keeps closed ports in TIME_WAIT and refuses to bind them without SO_REUSEADDR (Windows
    would instead let a second listener share the port, so it must not be set there)."""
    if not IS_WINDOWS:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def free_port(preferred):
    """`preferred` if nothing is listening on it, else any free local port."""
    for port in (preferred, 0):
        with socket.socket() as probe:
            _reuse(probe)
            try:
                probe.bind(('127.0.0.1', port))
                return probe.getsockname()[1]
            except OSError:
                continue
    return preferred


def port_in_use(port):
    with socket.socket() as probe:
        _reuse(probe)
        try:
            probe.bind(('127.0.0.1', port))
            return False
        except OSError:
            return True


def kill_tree(proc):
    """Stop a process and everything it started. The packaged exe runs every server as a wrapper
    process plus a child Python; terminating only the wrapper leaves the child holding its port."""
    if proc.poll() is not None:  # already gone; its PID may be reused by an unrelated process
        return
    try:
        if IS_WINDOWS:
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=10)
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def wait_for_port(port, seconds):
    """True once nothing else is bound to `port` (polls for up to `seconds`)."""
    end = time.monotonic() + seconds
    while True:
        if not port_in_use(port):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.25)


def room_id(password):
    """The room everyone with this password shares. Only this hash leaves the PC, never the password."""
    return hashlib.sha256(('duskbloods-room-v1:' + password.strip()).encode('utf-8')).hexdigest()[:32]


def local_addresses():
    """This PC's private IPv4 addresses (LAN, VPN): what friends connect to when you host."""
    found = []
    try:
        found += [i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    try:  # the address this PC uses to reach other networks (a UDP connect sends nothing)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(('192.0.2.1', 9))
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    vpn_ranges = [ipaddress.IPv4Network(n) for n in (
        '100.64.0.0/10',   # Tailscale
        '26.0.0.0/8',      # Radmin VPN
        '25.0.0.0/8')]     # Hamachi
    out = []
    for address in found:
        try:
            ip = ipaddress.IPv4Address(address)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            continue
        if (ip.is_private or any(ip in n for n in vpn_ranges)) and address not in out:
            out.append(address)
    return out


def parse_server(text):
    """'host' or 'host:port' -> (host, port)."""
    text = text.strip().replace('http://', '').replace('https://', '').strip('/')
    if not text:
        return None, PORT
    host, _, port = text.partition(':')
    try:
        port = int(port) if port else PORT
    except ValueError:
        return None, PORT
    if port == PORT + ROOM_OFFSET:  # someone pasted the room-service port: use the game port
        port = PORT
    return host.strip() or None, port


def http_get(url, data=None, timeout=8):
    request = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                     headers={'Content-Type': 'application/json'} if data is not None else {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


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

    def __init__(self, game_main, emulator, log, online=None):
        self.online = online or {'mode': 'offline'}
        self.mode = self.online.get('mode', 'offline')
        self.servers = False
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
        seed = next((p / 'seed' for p in (self.emulator.parent, HERE) if (p / 'seed').is_dir()), None)
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
        config = config_file_for(self.data)
        if not config.exists():
            self.log('No qt-config.ini yet: the emulator starts with its defaults '
                     '(start it once on its own, then play from here).')
            return
        text = config.read_text(encoding='utf-8')
        for key, value in (('nand_directory', nand.as_posix()), ('save_directory', save.as_posix()),
                           ('airplane_mode', 'false'), ('use_gdbstub', 'false'),
                           ('check_for_updates', 'false')):
            text = set_key(text, key, value)
        # A network adapter name saved on another PC (e.g. a VPN) does not exist here and makes the
        # game report "no internet connection": always fall back to the automatic adapter.
        text = re.sub(r'(?m)^network_interface=.*$', 'network_interface=', text)
        text = re.sub(r'(?m)^network_interface\\default=.*$', r'network_interface\\default=true', text)
        config.write_text(text, encoding='utf-8')

    def start_servers(self, run):
        """The local servers: loopback only, or (hosting) open to the network for friends."""
        no_window = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        own_group = {} if IS_WINDOWS else {'start_new_session': True}
        hosting = self.mode == 'host'
        pki = self.test / 'pki'
        if not (pki / 'ca.der').exists():
            self.log('Creating private test certificates for this PC (first run)...')
            made = subprocess.run(python_command(SERVER / 'make_pki.py') + ['--out', str(pki)],
                                  cwd=SERVER, capture_output=True, creationflags=no_window)
            if made.returncode != 0 or not (pki / 'ca.der').exists():
                self.log('Could not create the test certificates.', error=True)
                return False
        wanted = [PORT] + ([PORT + ROOM_OFFSET] if hosting else [])
        for port in wanted:
            if port_in_use(port):
                self.log('Waiting for the previous session to release its port...')
                if not wait_for_port(port, 15):
                    self.log(f'Port {port} is still in use by another program (an earlier launcher or '
                             'emulator session still running?). Close it and try again.', error=True)
                    return False
        upstream_port = free_port(PORT + 1)
        web_port = free_port(WEB_PORT)
        stub_hosts = ['127.0.0.1']
        if hosting:
            addresses = local_addresses()
            stub_hosts += addresses
            self.log('Starting the room servers (friends can reach this PC)...')
        else:
            self.log('Starting the local servers (127.0.0.1 only)...')
        stub_args = ['--port', str(upstream_port), '--host', ','.join(stub_hosts), '--web-port', str(web_port),
                     '--data', str(run / 'server_data'), '--pki', str(pki), '--log', str(run / 'server.log')]
        if hosting and len(stub_hosts) > 1:
            stub_args += ['--nat-ip', stub_hosts[1]]
        self.servers = True
        server = subprocess.Popen(
            python_command(SERVER / 'npln_stub.py') + stub_args,
            cwd=SERVER, stdout=(run / 'server_console.log').open('w'), stderr=subprocess.STDOUT,
            creationflags=no_window, **own_group)
        front_env = dict(os.environ, DUSK_REDIRECT_VALUE='nochange', DUSK_FRONTEND='1', PYTHONUNBUFFERED='1')
        front_args = ['--port', str(PORT), '--upstream', str(upstream_port), '--ca-dir', str(pki),
                      '--cert-dir', str(self.test / 'frontdoor_pki'), '--log', str(run / 'frontdoor.log')]
        if hosting:
            front_env.update(DUSK_ONLINE='1', DUSK_HOST_IP=stub_hosts[1] if len(stub_hosts) > 1 else '')
            front_args += ['--bind', '0.0.0.0', '--room-port', str(PORT + ROOM_OFFSET),
                           '--room-name', self.online.get('name') or 'Duskbloods room']
        front = subprocess.Popen(
            python_command(FRONT / 'frontdoor.py') + front_args,
            cwd=FRONT, env=front_env, stdout=(run / 'frontdoor_console.log').open('w'),
            stderr=subprocess.STDOUT, creationflags=no_window, **own_group)
        self.processes = [front, server]
        time.sleep(3)
        if server.poll() is not None or front.poll() is not None:
            self.log(f'A local server stopped at start; see {run}', error=True)
            self.stop()
            return False
        if hosting:
            try:
                http_get(f'http://127.0.0.1:{PORT + ROOM_OFFSET}/register', {'room': room_id(self.online['password'])})
            except OSError as error:
                self.log(f'The room service did not start ({error}); see {run}', error=True)
                self.stop()
                return False
            shown = ', '.join(a for a in local_addresses()) or 'this PC\'s address'
            self.log(f'Room open. Friends join with the address {shown} and the same password.')
        return True

    def join_server(self, run):
        """Join someone's room: fetch their certificate and register the room. -> (host, port, ca file)."""
        host, port = parse_server(self.online.get('server', ''))
        if not host:
            self.log('Type the address of the PC that hosts the room (the host sees it in their Online tab).', error=True)
            return None
        base = f'http://{host}:{port + ROOM_OFFSET}'
        self.log(f'Contacting {host}...')
        try:
            hello = json.loads(http_get(base + '/hello'))
            if hello.get('service') != 'duskbloods-online':
                raise ValueError('that is not a Duskbloods room')
            ca = self.test / 'online_ca' / (re.sub(r'[^0-9A-Za-z.-]', '_', host) + '.der')
            ca.parent.mkdir(parents=True, exist_ok=True)
            ca.write_bytes(http_get(base + '/ca.der'))
            http_get(base + '/register', {'room': room_id(self.online['password'])})
        except (OSError, ValueError) as error:
            self.log(f'Could not reach the room at {host}: {error}\n'
                     f'Check the address, that the host pressed Start game first, and that ports {port} and '
                     f'{port + ROOM_OFFSET} (TCP) are open between you (firewall, router or VPN).', error=True)
            return None
        self.log(f'Joined room at {host} ({hello.get("name", "")}).')
        return host, port, ca

    def start(self):
        if not self.check_keys():
            return None
        nand, save = self.seed()
        self.prepare_config(nand, save)
        self.run_dir = self.test / 'runs' / time.strftime('%Y%m%d_%H%M%S')
        run = self.run_dir
        (run / 'server_data').mkdir(parents=True)
        pki = self.test / 'pki'
        server_address, ca_file = f'127.0.0.1:{PORT}', pki / 'ca.der'
        if self.mode == 'join':
            joined = self.join_server(run)
            if joined is None:
                return None
            server_address, ca_file = f'{joined[0]}:{joined[1]}', joined[2]
        elif not self.start_servers(run):
            return None
        env = {k: v for k, v in os.environ.items() if not k.startswith(('EDEN_NX2_', 'EDEN_NATIVE_'))}
        env.update(EDEN_NX2_DIAG_CODEC_CLIENT_REPLY='1', EDEN_NX2_EXPERIMENTAL_CODEC_DECODE='1',
                   EDEN_NX2_EXPERIMENTAL_SYSMIX_OUTPUT='1', EDEN_NX2_SYSMIX_DRAIN_MULTIPLIER='1',
                   EDEN_NX2_APP_POOL_PROBE='1152', EDEN_NX2_GPU_MAP_FLAG_PROBE='1',
                   EDEN_NX2_SILENT_BAD_OPUS='1',
                   EDEN_NX2_ONLINE_HOSTS='npln.srv.nintendo.net,npln.nintendo.net,n.n.srv.nintendo.net,'
                                         'p.srv.nintendo.net,fromsoftware-game.net',
                   EDEN_NX2_ONLINE_SERVER=server_address,
                   EDEN_NX2_ONLINE_CA=str(ca_file),
                   EDEN_NX2_CRASH_DUMP=str(run), EDEN_NX2_TRACE_TERMINATE='1')
        if self.mode == 'offline':  # offline: nothing but this PC
            env.update(EDEN_NX2_LOOPBACK_ONLY='1')
        env.update({k: v for k, v in os.environ.items() if k.startswith('EDEN_NX2_') and k not in env})
        env.pop('SDL_AUDIODRIVER', None)
        if not IS_WINDOWS and not os.access(self.emulator, os.X_OK):
            try:
                os.chmod(self.emulator, self.emulator.stat().st_mode | 0o111)
            except OSError:
                self.log(f'{self.emulator} is not executable: run  chmod +x "{self.emulator}"', error=True)
                self.stop()
                return None
        console = (run / 'console.log').open('w')
        game = subprocess.Popen([str(self.emulator), '-g', str(self.game_main)],
                                cwd=self.emulator.parent, env=env, stdout=console,
                                stderr=subprocess.STDOUT)
        self.processes.insert(0, game)
        self.log(f'{emulator_label(self.emulator)} started. Logs: {run}')
        return game

    def stop(self):
        for proc in reversed(self.processes):  # the emulator, then both servers
            kill_tree(proc)
        if self.servers:
            wait_for_port(PORT, 10)
            if self.mode == 'host':
                wait_for_port(PORT + ROOM_OFFSET, 5)
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
        data = json.loads(SETTINGS.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
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

    # Colours sampled from the game's own menus (top tab bar, Arquivo screen, matchmaking button).
    BG = '#1a1214'         # window
    PAGE = '#241a1b'       # page background (Arquivo)
    BAR = '#2c191e'        # top tab bar
    BAR_EDGE = '#4a2a33'
    TAB_ON = '#5e4c47'     # selected tab
    DIVIDER = '#88736c'
    ITEM = '#311b1d'       # list items, fields, buttons
    ITEM_EDGE = '#43292a'
    ITEM_ON = '#3a2629'
    ITEM_ON_EDGE = '#a48c80'
    LINE = '#5c3835'       # thin ornament lines
    GOLD = '#ece0d2'       # titles
    TEXT = '#d9cdc0'
    DIM = '#9a8a80'
    ERR = '#e0745a'
    GOOD = '#9fb98a'
    SERIF = 'Georgia'
    MONO = 'Consolas'

    settings = load_settings()
    root = tk.Tk()
    if not IS_WINDOWS:  # Georgia / Consolas are Windows fonts
        from tkinter import font as tkfont
        families = set(tkfont.families())
        SERIF = next((f for f in ('DejaVu Serif', 'Liberation Serif', 'Noto Serif', 'FreeSerif')
                      if f in families), 'serif')
        MONO = next((f for f in ('DejaVu Sans Mono', 'Liberation Mono', 'Noto Sans Mono')
                     if f in families), 'monospace')
    root.title(f'The Duskbloods - Network Test Launcher {VERSION}')
    root.configure(bg=BG)
    root.resizable(False, False)
    try:
        if IS_WINDOWS and (ASSETS / 'icon.ico').exists():
            root.iconbitmap(default=str(ASSETS / 'icon.ico'))
        elif not IS_WINDOWS and (ASSETS / 'icon.png').exists():
            icon_image = tk.PhotoImage(file=str(ASSETS / 'icon.png'))
            root.iconphoto(True, icon_image)
            root.icon_image = icon_image
    except tk.TclError:
        pass

    style = ttk.Style()
    style.theme_use('clam')
    style.configure('Dusk.TCombobox', fieldbackground=ITEM, background=ITEM, foreground=TEXT,
                    arrowcolor=GOLD, bordercolor=ITEM_EDGE, lightcolor=ITEM, darkcolor=ITEM)
    style.map('Dusk.TCombobox', fieldbackground=[('readonly', ITEM)], foreground=[('readonly', TEXT)],
              selectbackground=[('readonly', ITEM)], selectforeground=[('readonly', GOLD)])
    root.option_add('*TCombobox*Listbox.background', ITEM)
    root.option_add('*TCombobox*Listbox.foreground', TEXT)
    root.option_add('*TCombobox*Listbox.selectBackground', TAB_ON)
    root.option_add('*TCombobox*Listbox.font', (SERIF, 10))

    # --- drawing helpers ------------------------------------------------------------------------
    def clickable(widget, command):
        for w in [widget] + list(widget.winfo_children()):
            w.bind('<Button-1>', lambda _e: command())
            w.configure(cursor='hand2')

    def heading(parent, title, bg=PAGE):
        """A section title with the game's thin line and diamond underneath ("Tutoriais")."""
        box = tk.Frame(parent, bg=bg)
        box.pack(fill='x', pady=(8, 4))
        tk.Label(box, text=title, font=(SERIF, 13), fg=GOLD, bg=bg, anchor='w').pack(fill='x', padx=(16, 0))
        line = tk.Canvas(box, height=9, bg=bg, highlightthickness=0, bd=0)
        line.pack(fill='x')

        def draw(event):
            line.delete('all')
            line.create_line(4, 4, event.width, 4, fill=LINE)
            line.create_polygon(8, 4, 12, 0, 16, 4, 12, 8, fill=bg, outline=DIVIDER)
        line.bind('<Configure>', draw)
        return box

    def entry(parent, variable, **options):
        return tk.Entry(parent, textvariable=variable, font=(SERIF, 11), bg=ITEM, fg=TEXT,
                        insertbackground=GOLD, relief='flat', highlightthickness=1,
                        highlightbackground=ITEM_EDGE, highlightcolor=ITEM_ON_EDGE, **options)

    def button(parent, text, command, bg=PAGE):
        b = tk.Label(parent, text=text, font=(SERIF, 10), fg=TEXT, bg=ITEM, padx=14, pady=4,
                     highlightthickness=1, highlightbackground=ITEM_EDGE, cursor='hand2')
        b.bind('<Button-1>', lambda _e: command())
        b.bind('<Enter>', lambda _e: b.configure(bg=ITEM_ON, highlightbackground=ITEM_ON_EDGE, fg=GOLD))
        b.bind('<Leave>', lambda _e: b.configure(bg=ITEM, highlightbackground=ITEM_EDGE, fg=TEXT))
        return b

    def note(parent, text, color=DIM, size=10, bg=PAGE, wrap=420):
        return tk.Label(parent, text=text, font=(SERIF, size), fg=color, bg=bg, anchor='w', justify='left',
                        wraplength=wrap)

    # --- top tab bar (L | Setup | Online | R) ---------------------------------------------------
    tabbar = tk.Frame(root, bg=BAR, highlightthickness=1, highlightbackground=BAR_EDGE)
    tabbar.pack(fill='x')
    pages, tab_widgets, order = {}, {}, ['Setup', 'Online']
    current = {'tab': 'Setup'}

    def show_tab(name):
        current['tab'] = name
        for tab, (frame, accent, label) in tab_widgets.items():
            on = tab == name
            frame.configure(bg=TAB_ON if on else BAR)
            label.configure(bg=TAB_ON if on else BAR, fg=GOLD if on else TEXT)
            accent.configure(bg=ITEM_ON_EDGE if on else BAR)
        if name == 'Online' and mode_var.get() == 'host':
            show_addresses()
        for tab, page in pages.items():
            page.pack_forget()
        pages[name].pack(fill='both', expand=True)

    def step_tab(delta):
        show_tab(order[(order.index(current['tab']) + delta) % len(order)])

    for i, name in enumerate(order):
        if i:
            tk.Frame(tabbar, bg=DIVIDER, width=1).pack(side='left', fill='y', pady=8)
        frame = tk.Frame(tabbar, bg=BAR)
        frame.pack(side='left', fill='y')
        accent = tk.Frame(frame, bg=BAR, height=2)
        accent.pack(fill='x')
        label = tk.Label(frame, text=name, font=(SERIF, 13), fg=TEXT, bg=BAR, width=13, pady=7)
        label.pack(fill='both', expand=True)
        clickable(frame, lambda n=name: show_tab(n))
        tab_widgets[name] = (frame, accent, label)
    tk.Frame(tabbar, bg=DIVIDER, width=1).pack(side='left', fill='y', pady=8)
    tk.Label(tabbar, text=f'v{VERSION}', font=(SERIF, 9), fg=DIM, bg=BAR).pack(side='right', padx=12)
    root.bind('<Control-Tab>', lambda _e: step_tab(1))
    root.bind('<Control-Shift-Tab>', lambda _e: step_tab(-1))

    if (ASSETS / 'banner.png').exists():
        banner_image = tk.PhotoImage(file=str(ASSETS / 'banner.png'))
        tk.Label(root, image=banner_image, bg=BG, bd=0).pack()
        root.banner_image = banner_image
    else:
        tk.Label(root, text='THE DUSKBLOODS', font=(SERIF, 30), fg='#9b3b22', bg=BG).pack(pady=20)

    holder = tk.Frame(root, bg=PAGE, highlightthickness=1, highlightbackground=ITEM_EDGE)
    holder.pack(fill='both', padx=14, pady=(0, 8))
    body = tk.Frame(holder, bg=PAGE, padx=14, pady=4)
    online_tab = tk.Frame(holder, bg=PAGE, padx=10, pady=6)
    pages.update(Setup=body, Online=online_tab)

    # --- Setup page -----------------------------------------------------------------------------
    heading(body, 'Game')
    game_frame = tk.Frame(body, bg=PAGE)
    game_frame.pack(fill='x', padx=(16, 0))
    game_var = tk.StringVar(value=settings.get('game', ''))
    if not game_main_from(game_var.get()):
        for folder in default_game_folders():
            if game_main_from(folder):
                game_var.set(str(folder))
                break
    entry(game_frame, game_var).pack(side='left', fill='x', expand=True, ipady=4)

    def browse_game():
        chosen = filedialog.askdirectory(title='Select the THE DUSKBLOODS NETWORK TEST VER folder')
        if chosen:
            game_var.set(chosen)
            refresh()

    button(game_frame, 'Browse', browse_game).pack(side='left', padx=(8, 0))
    game_status = note(body, '', size=9, wrap=640)
    game_status.pack(fill='x', padx=(16, 0), pady=(2, 0))

    heading(body, 'Emulator')
    emu_frame = tk.Frame(body, bg=PAGE)
    emu_frame.pack(fill='x', padx=(16, 0))
    emulators = default_emulators()
    saved_emulator = settings.get('emulator')
    if saved_emulator and Path(saved_emulator).is_file() and \
            Path(saved_emulator).resolve() not in [e.resolve() for e in emulators]:
        emulators.insert(0, Path(saved_emulator))
    labels = [emulator_label(e) for e in emulators]
    emu_var = tk.StringVar()
    combo = ttk.Combobox(emu_frame, textvariable=emu_var, values=labels, state='readonly',
                         style='Dusk.TCombobox', font=(SERIF, 10))
    combo.pack(side='left', fill='x', expand=True, ipady=3)
    if saved_emulator and Path(saved_emulator).is_file():
        for exe, label in zip(emulators, labels):
            if exe.resolve() == Path(saved_emulator).resolve():
                emu_var.set(label)
    if not emu_var.get() and labels:
        own = emulator_exe_in(HERE)
        emu_var.set(emulator_label(own) if own else labels[0])

    def browse_emulator():
        chosen = filedialog.askopenfilename(
            title='Select the emulator (Eglantine, Lichii, Eden or yuzu)',
            filetypes=([('Emulator', ' '.join(EMULATOR_NAMES)), ('Programs', '*.exe')] if IS_WINDOWS
                       else [('All files', '*')]))
        if chosen:
            exe = Path(chosen)
            if exe.resolve() not in [e.resolve() for e in emulators]:
                emulators.insert(0, exe)
                labels.insert(0, emulator_label(exe))
                combo.configure(values=labels)
            emu_var.set(emulator_label(exe))
            refresh()

    button(emu_frame, 'Browse', browse_emulator).pack(side='left', padx=(8, 0))
    emu_status = note(body, '', size=9, wrap=640)
    emu_status.pack(fill='x', padx=(16, 0), pady=(2, 6))

    def selected_emulator():
        label = emu_var.get()
        for exe, item in zip(emulators, labels):
            if item == label:
                return exe
        return None

    # --- Online page: a list on the left, the chosen entry on the right (the game's Arquivo) ----
    saved_online = settings.get('online') if isinstance(settings.get('online'), dict) else {}
    mode_var = tk.StringVar(value=saved_online.get('mode', 'offline'))
    if mode_var.get() not in ('offline', 'join', 'host'):
        mode_var.set('offline')
    password_var = tk.StringVar(value=saved_online.get('password', ''))
    server_var = tk.StringVar(value=saved_online.get('server', ''))
    show_var = tk.IntVar(value=0)

    MODES = {
        'offline': ('Just me', 'Offline test',
                    'Play on your own. The game only talks to the small servers the launcher runs on '
                    'this PC; nothing leaves it.'),
        'join': ('Join a room', 'Join a room',
                 'Play with a friend who hosts a room. Type the same room password as them and the address '
                 'their Online tab shows. They must press Start game first. Then start matchmaking in the game: '
                 'everyone with the same password is matched together.'),
        'host': ('Host a room', 'Host a room',
                 'This PC runs the room for everyone. Choose a password, give your friends the password and '
                 'one of the addresses below, then press Start game and start matchmaking in the game.'),
    }

    left = tk.Frame(online_tab, bg=PAGE, width=200)
    left.pack(side='left', fill='y', padx=(4, 0))
    tk.Label(left, text='Play mode', font=(SERIF, 11), fg=DIM, bg=PAGE, anchor='w').pack(fill='x', padx=(18, 0), pady=(4, 2))
    list_line = tk.Canvas(left, height=7, width=196, bg=PAGE, highlightthickness=0, bd=0)
    list_line.pack(fill='x')
    list_line.create_line(2, 3, 196, 3, fill=LINE)
    list_line.create_polygon(6, 3, 9, 0, 12, 3, 9, 6, fill=PAGE, outline=DIVIDER)
    mode_items = {}
    for value, (title, _, _) in MODES.items():
        item = tk.Label(left, text=title, font=(SERIF, 12), fg=TEXT, bg=ITEM, anchor='w', padx=12, pady=6,
                        width=18, highlightthickness=1, highlightbackground=ITEM_EDGE, cursor='hand2')
        item.pack(fill='x', pady=(6, 0))
        item.bind('<Button-1>', lambda _e, v=value: (mode_var.set(v), update_online()))
        mode_items[value] = item

    tk.Frame(online_tab, bg=LINE, width=1).pack(side='left', fill='y', padx=14)
    right = tk.Frame(online_tab, bg=PAGE)
    right.pack(side='left', fill='both', expand=True)
    tk.Label(right, text='Online', font=(SERIF, 10), fg=DIM, bg=PAGE, anchor='w').pack(fill='x')
    mode_title = tk.Label(right, text='', font=(SERIF, 17), fg=GOLD, bg=PAGE, anchor='w')
    mode_title.pack(fill='x')
    title_line = tk.Canvas(right, height=7, bg=PAGE, highlightthickness=0, bd=0)
    title_line.pack(fill='x', pady=(0, 6))

    def draw_title_line(event):
        title_line.delete('all')
        title_line.create_line(2, 3, event.width * 0.8, 3, fill=LINE)
        title_line.create_polygon(6, 3, 9, 0, 12, 3, 9, 6, fill=PAGE, outline=DIVIDER)
    title_line.bind('<Configure>', draw_title_line)
    mode_text = note(right, '', TEXT, size=11, wrap=440)
    mode_text.pack(fill='x', pady=(0, 8))

    pw_row = tk.Frame(right, bg=PAGE)
    tk.Label(pw_row, text='Room password', font=(SERIF, 11), fg=GOLD, bg=PAGE, width=14, anchor='w').pack(side='left')
    pw_entry = entry(pw_row, password_var, show='*')
    pw_entry.pack(side='left', fill='x', expand=True, ipady=3)

    def toggle_password():
        pw_entry.configure(show='' if show_var.get() else '*')

    tk.Checkbutton(pw_row, text='show', variable=show_var, command=toggle_password, font=(SERIF, 9), fg=DIM,
                   bg=PAGE, selectcolor=ITEM, activebackground=PAGE, activeforeground=GOLD,
                   highlightthickness=0).pack(side='left', padx=(6, 0))

    join_box = tk.Frame(right, bg=PAGE)
    tk.Label(join_box, text='Host\'s address', font=(SERIF, 11), fg=GOLD, bg=PAGE, width=14, anchor='w').pack(side='left')
    entry(join_box, server_var).pack(side='left', fill='x', expand=True, ipady=3)

    host_box = tk.Frame(right, bg=PAGE)
    address_rows = tk.Frame(host_box, bg=PAGE)
    address_rows.pack(fill='x')
    shown_addresses = {'list': None}

    def copy_address(address):
        root.clipboard_clear()
        root.clipboard_append(address)
        log(f'Copied {address}')

    def show_addresses():
        """List this PC's addresses again (a VPN may have connected since the launcher opened)."""
        addresses = local_addresses()[:4]
        if addresses == shown_addresses['list']:
            return
        shown_addresses['list'] = addresses
        for child in address_rows.winfo_children():
            child.destroy()
        for address in addresses or ['(no network found)']:
            row = tk.Frame(address_rows, bg=PAGE)
            row.pack(fill='x', pady=1)
            tk.Label(row, text='Your address', font=(SERIF, 11), fg=GOLD, bg=PAGE, width=14,
                     anchor='w').pack(side='left')
            tk.Label(row, text=address, font=(MONO, 12), fg=GOLD, bg=PAGE, anchor='w', width=16).pack(side='left')
            if addresses:
                button(row, 'Copy', lambda a=address: copy_address(a)).pack(side='left', padx=6)
    note(host_box, 'Friends must reach this PC: the same home network, or a VPN such as Tailscale, ZeroTier or '
                   'Radmin (share that VPN\'s address). Over the internet, forward '
                   f'TCP {PORT} and {PORT + ROOM_OFFSET}, TCP+UDP 3478-3479 and UDP 10025, 10125, 33334, 34343. '
                   'Allow the launcher and the emulator in the Windows firewall when asked.', size=9, wrap=440).pack(fill='x', pady=(6, 0))
    guide_row = tk.Frame(right, bg=PAGE)
    guide_row.pack(side='bottom', fill='x')

    def open_guide():
        import webbrowser
        webbrowser.open(ONLINE_GUIDE)

    button(guide_row, 'How to play online', open_guide).pack(side='left')
    note(guide_row, 'Experimental: online matches are still being tested.', DIM, size=9,
         wrap=260).pack(side='left', padx=(10, 0))

    def update_online(*_):
        mode = mode_var.get()
        for value, item in mode_items.items():
            on = value == mode
            item.configure(bg=ITEM_ON if on else ITEM, fg=GOLD if on else TEXT,
                           highlightbackground=ITEM_ON_EDGE if on else ITEM_EDGE)
        _, title, text = MODES[mode]
        mode_title.configure(text=title)
        mode_text.configure(text=text)
        for box in (pw_row, join_box, host_box):
            box.pack_forget()
        if mode in ('join', 'host'):
            pw_row.pack(fill='x', pady=3)
        if mode == 'join':
            join_box.pack(fill='x', pady=3)
        elif mode == 'host':
            show_addresses()
            host_box.pack(fill='x', pady=3)
        refresh_footer()

    # --- log, the matchmaking-style PLAY bar and button prompts ---------------------------------
    log_box = tk.Text(root, height=6, font=(MONO, 9), bg=ITEM, fg=TEXT, relief='flat', padx=8, pady=4,
                      highlightthickness=1, highlightbackground=ITEM_EDGE, wrap='word', state='disabled')
    log_box.pack(fill='x', padx=14)
    log_box.tag_configure('error', foreground=ERR)

    def log(message, error=False):
        def append():
            log_box.configure(state='normal')
            log_box.insert('end', message + '\n', 'error' if error else '')
            log_box.see('end')
            log_box.configure(state='disabled')
        root.after(0, append)

    footer = tk.Frame(root, bg=BG)
    footer.pack(fill='x', padx=14, pady=(10, 4))
    play = tk.Frame(footer, bg=ITEM, highlightthickness=1, highlightbackground=ITEM_EDGE, cursor='hand2')
    play.pack(side='left')
    play_label = tk.Label(play, text='Start game', font=(SERIF, 14), fg=GOLD, bg=ITEM, anchor='w', width=17,
                          padx=18, pady=7)
    play_label.pack(side='left')
    state = {'session': None, 'can_play': False}

    def hover(on):
        if state['can_play']:
            color = ITEM_ON if on else ITEM
            for w in (play, play_label):
                w.configure(bg=color)
            play.configure(highlightbackground=ITEM_ON_EDGE if on else ITEM_EDGE)

    for w in (play, play_label):
        w.bind('<Button-1>', lambda _e: play_clicked())
        w.bind('<Enter>', lambda _e: hover(True))
        w.bind('<Leave>', lambda _e: hover(False))

    footer_note = tk.Label(root, text='', font=(SERIF, 8), fg=DIM, bg=BG)
    footer_note.pack(pady=(2, 8))

    def refresh_footer():
        footer_note.configure(text='Offline: everything stays on this PC (127.0.0.1)' if mode_var.get() == 'offline'
                              else 'Online room: only a hash of the password and your address are shared; '
                                   'keys and game files are never sent')

    def refresh(*_):
        main = game_main_from(game_var.get())
        game_status.configure(text='The Duskbloods network test found' if main else
                              'Not found: choose the folder that holds base/exefs/main',
                              fg=GOOD if main else ERR)
        exe = selected_emulator()
        if exe:
            data = data_dir_for(exe)
            keys = (data / 'keys/prod.keys').exists()
            emu_status.configure(text=f'{exe}\nData: {data}' + ('' if keys else '   (no prod.keys yet)'),
                                 fg=DIM if keys else ERR)
        else:
            emu_status.configure(text='No emulator selected', fg=ERR)
        running = state['session'] is not None
        state['can_play'] = not running and bool(main) and bool(exe)
        play_label.configure(text='Playing...' if running else 'Start game',
                             fg=GOLD if state['can_play'] else DIM)
        hover(False)

    def play_clicked():
        if not state['can_play']:
            return
        main = game_main_from(game_var.get())
        exe = selected_emulator()
        if not main or not exe:
            return
        online = {'mode': mode_var.get(), 'password': password_var.get().strip(), 'server': server_var.get().strip()}
        if online['mode'] != 'offline' and not online['password']:
            show_tab('Online')
            log('Type a room password in the Online tab first: everyone with the same password plays together.',
                error=True)
            return
        if online['mode'] == 'join' and not online['server']:
            show_tab('Online')
            log('Type the host\'s address in the Online tab first.', error=True)
            return
        settings.update(game=game_var.get(), emulator=str(exe), online=online)
        save_settings(settings)
        session = Session(main, exe, log, online)
        state['session'] = session
        refresh()

        def worker():
            try:
                game = session.start()
                if game is not None:
                    game.wait()
                    code = game.returncode
                    log('Game closed (' + (f'exit {code:#x}' if code >= 0 else f'signal {-code}') + ').')
            except Exception as error:  # report anything to the window
                log(f'Launch failed: {error}', error=True)
            finally:
                session.stop()
                log('Local servers stopped.' if session.servers else 'Session ended.')
                state['session'] = None
                root.after(0, refresh)

        threading.Thread(target=worker, daemon=True).start()

    def open_logs():
        exe = selected_emulator()
        folder = test_dir_for(data_dir_for(exe)) / 'runs' if exe else HERE
        folder.mkdir(parents=True, exist_ok=True)
        open_folder(folder)

    def on_close():
        session = state['session']
        if session is not None:
            for proc in session.processes[:1]:
                proc.terminate()
            session.stop()
        root.destroy()

    button(footer, 'Open logs', open_logs).pack(side='left', padx=12)

    game_var.trace_add('write', refresh)
    combo.bind('<<ComboboxSelected>>', refresh)
    update_online()
    refresh()
    # Keep the window the same size on every tab: size the page area for the tallest page.
    sizes = []
    chosen_mode = mode_var.get()
    for name in order:
        show_tab(name)
        for mode in (MODES if name == 'Online' else [chosen_mode]):  # every Online mode must fit
            mode_var.set(mode)
            update_online()
            root.update_idletasks()
            sizes.append((holder.winfo_reqwidth(), holder.winfo_reqheight()))
    mode_var.set(chosen_mode)
    update_online()
    holder.configure(width=max(w for w, _ in sizes), height=max(h for _, h in sizes) + 30)
    holder.pack_propagate(False)
    show_tab('Setup')
    log('Choose the game folder and an emulator, then press Start game.')

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
