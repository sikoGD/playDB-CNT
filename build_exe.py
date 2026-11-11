"""Builds dist/Play Duskbloods.exe with PyInstaller (pip install pyinstaller cryptography grpcio zstandard)."""
import subprocess
import sys

MODULES = ('argparse base64 copy json math queue secrets socket struct threading datetime select ssl '
           'hashlib hmac ipaddress sqlite3 getpass http.server collections concurrent.futures grpc '
           'tkinter tkinter.filedialog tkinter.ttk').split()
command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
           '--icon', 'assets/icon.ico', '--name', 'Play Duskbloods',
           '--add-data', 'tools;tools', '--add-data', 'assets;assets',
           '--collect-submodules', 'cryptography', '--collect-submodules', 'grpc',
           '--collect-submodules', 'zstandard', '--hidden-import', 'dusk_mods']
for module in MODULES:
    command += ['--hidden-import', module]
subprocess.check_call(command + ['play_duskbloods.py'])
