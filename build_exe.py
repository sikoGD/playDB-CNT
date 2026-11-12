"""Builds dist/Play Duskbloods.exe with PyInstaller (pip install pyinstaller cryptography grpcio zstandard).

Antivirus false positives: the loader PyInstaller ships prebuilt is shared by many packed programs,
some of them malware, so Windows Defender's machine-learning check may report the exe as
Trojan:Win32/Wacatac.B!ml. Release builds use a loader compiled from PyInstaller's own source
instead (Visual Studio Build Tools, in a "x64 Native Tools" prompt):

    pip download pyinstaller==<version> --no-binary :all: --no-deps
    (unpack it) cd pyinstaller-<version>/bootloader
    python waf --target-arch=64bit --msvc_targets=x64 all
    pip install ..                      (in a virtual environment used only for building)

then run this script with that environment's Python.
"""
import subprocess
import sys

MODULES = ('argparse base64 copy json math queue secrets socket struct threading datetime select ssl '
           'hashlib hmac ipaddress sqlite3 getpass http.server collections concurrent.futures grpc '
           'tkinter tkinter.filedialog tkinter.ttk').split()
command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed', '--noupx',
           '--icon', 'assets/icon.ico', '--version-file', 'assets/version_info.txt', '--name', 'Play Duskbloods',
           '--add-data', 'tools;tools', '--add-data', 'assets;assets',
           '--collect-submodules', 'cryptography', '--collect-submodules', 'grpc',
           '--collect-submodules', 'zstandard', '--hidden-import', 'dusk_mods']
for module in MODULES:
    command += ['--hidden-import', module]
subprocess.check_call(command + ['play_duskbloods.py'])
