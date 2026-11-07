# playDB-CNT - Play Duskbloods (Network Test) launcher

A small launcher (Windows and Linux) for running **The Duskbloods Network Test** on Eglantine-based Switch 2
emulators (Eglantine, Lichii, Eden-style builds) with the online features served by local
stand-in servers, either for yourself (offline, loopback only) or for a room of friends
(**online rooms, new in 1.1.0**: see [ONLINE.md](ONLINE.md)).

It gives you a window styled after the game's menus where you pick:

* **the game folder** (the folder that contains `base/exefs/main`; the version is checked), and
* **the emulator** (`eglantine.exe`, `lichii.exe`, `eden.exe` or `yuzu.exe`; on Linux `eglantine`,
  `lichii`, `eden`, `yuzu` or an AppImage), found automatically next to the launcher, in sibling
  build folders and in a `Lichii` folder, or chosen with Browse.

Press **Start game** and it starts the stand-in servers, launches the emulator with the game, and
stops the servers when the game closes. Logs are written next to the emulator's data (`Open logs`).

## Online play (experimental)

The **Online** tab has three modes: **Just me** (offline, as before), **Host a room** and
**Join a room**. Everyone who types the **same room password** is matched together: one friend
hosts, the others type the host's address and join.

The in-game match connects the emulators directly, and the server has no relay for that. Players
need the same home network, a VPN such as **ZeroTier** (recommended), **Tailscale** or **Radmin VPN**,
or router port forwards. The Online tab lists the ports.

**Step-by-step guide, VPN setup and troubleshooting: [ONLINE.md](ONLINE.md).**

## What is in here

| Path | What |
|---|---|
| `play_duskbloods.py` | the launcher (Tk window, game/emulator discovery, process management) |
| `tools/npln_server/` | local stand-in for the online login/session service (127.0.0.1; also the host's network addresses when hosting a room) |
| `tools/dusk_server/` | "front door" stand-in for the game's own web API, plus the room matchmaking used by online rooms |
| `ONLINE.md` | how to play online with friends (VPN recommendations, ports, troubleshooting) |
| `assets/` | window art and the icon, taken from the game's title screen |
| `build_exe.py` | builds the single-file `Play Duskbloods.exe` with PyInstaller |
| `linux/` | `play-duskbloods.sh` (starts the launcher on Linux) and `README-LINUX.md` |

## Using it (Windows)

1. Download `Play Duskbloods.exe` from the Releases page, put it anywhere (next to your emulator
   build is easiest).
2. Run it, pick the game folder and the emulator.
3. Have your own `prod.keys` and `title.keys` in the emulator's `keys` folder (portable builds:
   `user/keys`; Lichii/Eden: `%APPDATA%\eden\keys`). The launcher tells you if they are missing.
4. Press **Start game**. To play with friends, see [ONLINE.md](ONLINE.md).

On the first run the launcher creates private test certificates for your PC (in the emulator's
data folder) and seeds an empty test NAND/save. Packaged builds that already have a
`user/test_data` folder keep using it.

## Using it (Linux)

The launcher itself runs on Linux. **It does not include an emulator**: you need a Linux build of
Eglantine (or Lichii / Eden / yuzu) that supports the Duskbloods network test. If you are not used
to Linux, follow these steps in order; commands go in a terminal.

1. **Install the requirements** (once). Debian / Ubuntu / Mint:

   ```
   sudo apt install python3 python3-venv python3-tk
   ```

   Fedora: `sudo dnf install python3 python3-tkinter`. Arch: `sudo pacman -S python tk`.
2. **Download** `Play-Duskbloods-linux.tar.gz` from the
   [Releases page](https://github.com/sikoGD/playDB-CNT/releases) and unpack it:

   ```
   tar xf Play-Duskbloods-linux.tar.gz
   cd Play-Duskbloods-linux
   ```
3. **Start it**:

   ```
   ./play-duskbloods.sh
   ```

   The first start needs internet for a minute: it creates a private Python environment in
   `~/.local/share/duskbloods-launcher/venv` and installs two small libraries (`grpcio`,
   `cryptography`). Later starts are instant. (If you get "Permission denied", run
   `chmod +x play-duskbloods.sh` once.)
4. In the window, pick the **game folder** (the one containing `base/exefs/main`) and the
   **emulator** (it looks in the launcher folder, `~/Applications`, `~/.local/bin`, `~/Downloads`
   and on your `PATH`; use Browse for anything else), then press **Start game**.
5. Put your own `prod.keys` and `title.keys` in the emulator's `keys` folder. The launcher shows the
   exact path if they are missing.

Where things are on Linux:

| What | Path |
|---|---|
| Emulator data and keys | `~/.local/share/eden` (or `lichii`, `eglantine`, `yuzu`); portable builds use `<emulator folder>/user` |
| Emulator config | `~/.config/eden/qt-config.ini` (or the portable `user/config`) |
| Launcher saves and logs | `<data folder>/duskbloods_test/`, logs in `runs/<time>/` (**Open logs** uses `xdg-open`) |

Linux troubleshooting:

* *"... is not executable"*: run `chmod +x ./your-emulator` (the launcher also tries this itself).
* *"Port 18443 is already in use"*: an earlier session is still running; close it and retry.
* *The window shows no text or odd fonts*: install `fonts-dejavu` (`sudo apt install fonts-dejavu`).
* *No window and "Tk is missing"*: install the Tk package from step 1.

The Linux package is small (about 330 KB) because it only holds the launcher's scripts and
images; it uses your system Python. The Windows exe is about 18 MB because it carries its own
Python.

## Building the exe

```
pip install pyinstaller cryptography grpcio
python build_exe.py
```

The exe appears in `dist/`. Python 3.10+ with Tk is required. On Linux there is nothing to build:
run `./linux/play-duskbloods.sh` from a checkout of this repository, or use the release tarball.

## Privacy and safety

* Offline (**Just me**), the servers bind to **127.0.0.1 only**; nothing is sent to any real
  service, and the emulator is started in its loopback-only online mode.
* Online rooms talk only to the host's PC, never to any real service. The room password is never
  sent; only a hash of it is. Hosting opens the ports listed in [ONLINE.md](ONLINE.md) while the
  game runs, so only play with people you trust.
* No keys, game files, saves or certificates are in this repository or the exe. Certificates are
  generated on your own PC the first time you play.
* The launcher stores your selected paths and Online tab choices (mode, room password, host
  address) in `launcher_settings.json` beside the exe.
* Before each launch it resets the emulator's saved network adapter to automatic (an adapter name
  copied from another PC, e.g. a VPN, makes the game report "no internet connection").

## Not included / legal

You must provide your own legally obtained game and keys. This project is not affiliated with
FromSoftware, Nintendo or any emulator project, and contains no copyrighted game files; the
banner and icon are screenshots of the game's title screen used for identification only.

## About

Made by **sikoGD** as part of an effort to get The Duskbloods Network Test running on Switch 2
emulators: the launcher here, plus the emulator-side work (rendering, performance, local servers)
done alongside it.

If this saved you time and you'd like to support the work:

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-d8a657?logo=buymeacoffee&logoColor=white)](https://buymeacoffee.com/rickjrbfl)

https://buymeacoffee.com/rickjrbfl
