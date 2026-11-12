# playDB-CNT - Play Duskbloods (Network Test) launcher

A small launcher (Windows, Linux and macOS) for running **The Duskbloods Network Test** on Eglantine-based Switch 2
emulators (Eglantine, Lichii, Eden-style builds) with the online features served by local
stand-in servers, either for yourself (offline, loopback only) or for a room of friends
(online rooms: see [ONLINE.md](ONLINE.md)). New in 1.2.0: **Bot Match**, a full match against
7 CPU players (see below). New in 1.2.1: the window can be resized, maximized and scrolled; Lichii
160+ keeps its data in `%APPDATA%\lichii` and is handled; the Windows exe no longer trips antivirus
false positives (see below). New in 1.3.0: **Patch Lichii**, which lets stock Lichii builds play
(see below).

**Best with a Duskbloods emulator build** (Eglantine, or the Duskbloods Lichii builds v31 and
later). The game needs its online services even offline, and those builds send it to the
launcher's servers. Stock Lichii (for example build 160) and Eden boot the game but, on their own,
stop at *"Online connection is required"* / *"Could not verify Nintendo Switch Online membership"*
(error 2321-4992).

## Patch Lichii (new in 1.3.0, Windows, experimental)

When the chosen emulator lacks the Duskbloods online support, the Setup tab shows a red note and a
**Patch Lichii** button (**Patch Eden** for Eden). Pressing it (and confirming) adds one file,
`version.dll`, next to the emulator. **Remove patch** deletes it again.

* The patch only acts when the launcher starts the game. It does what the Duskbloods builds do:
  sends the game's online names to the launcher's servers and, offline, keeps all traffic on this PC.
  Lichii started on its own runs exactly as before.
* The launcher also writes a small certificate archive with its test certificate into its own test
  NAND, so the game's own encrypted connections trust the launcher's servers.
* Tested with stock Lichii build 160: the game logs in, reaches the online main menu and starts a
  game. Performance and graphics are Lichii's own; the Duskbloods builds still run the game better.
* It never replaces a `version.dll` that belongs to another program. Source and build steps:
  `tools/online_shim/`.

It gives you a window styled after the game's menus where you pick:

* **the game**: a **`.nup` package** (Browse; opened by emulators with NUP support, such as Eglantine
  v30+ and Lichii) or the unpacked game **folder** that contains `base/exefs/main` (Folder). The
  version is checked either way. And
* **the emulator** (`eglantine.exe`, `lichii.exe`, `eden.exe` or `yuzu.exe`; on Linux `eglantine`,
  `lichii`, `eden`, `yuzu` or an AppImage), found automatically next to the launcher, in sibling
  build folders and in a `Lichii` folder, or chosen with Browse.

Press **Start game** and it starts the stand-in servers, launches the emulator with the game, and
stops the servers when the game closes. Logs are written next to the emulator's data (`Open logs`).

## Bot Match (new in 1.2.0)

The launcher adds **Bot Match** to the game's **Battle Remembrance > Tutorials** list: a complete
Dusk Battle on the full map, you against 7 CPU players, from the first phase to the final battle.
It's the game's own hidden full-map mission, switched on by the launcher. At each start it adds the
mission to the game's regulation and its name and description to the game's menu text (all
languages), both built from your own game files. The text goes into the emulator's mod folder as
`Bot Match (playDB-CNT)`. Untick **Bot Match** in the Setup tab to use the game's own files.

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
| `build_exe.py` | builds the single-file `Play Duskbloods.exe` with PyInstaller (see its notes on the loader) |
| `dusk_mods.py` | Bot Match: patches the regulation and adds the menu text |
| `tools/online_shim/` | the Patch Lichii `version.dll` (with source): online support for stock Lichii/Eden |
| `tools/ooz/` | the open-source Kraken decompressor ooz (GPL-3.0, with source), used to read the game's menu text |
| `linux/` | `play-duskbloods.sh` (starts the launcher on Linux) and `README-LINUX.md` |
| `mac/` | `Play Duskbloods.command` (starts the launcher on macOS) and `README-MAC.md` |

## Using it (Windows)

1. Download `Play Duskbloods.exe` from the Releases page, put it anywhere (next to your emulator
   build is easiest).
2. Run it, pick the game (`.nup` file with **Browse**, or the game folder with **Folder**) and the emulator.
3. Have your own `prod.keys` and `title.keys` in the emulator's `keys` folder (portable builds:
   `user/keys`; Eden and older Lichii: `%APPDATA%\eden\keys`; Lichii 160+: `%APPDATA%\lichii\keys`).
   The launcher tells you if they are missing.
4. Press **Start game**. To play with friends, see [ONLINE.md](ONLINE.md). On a small screen, scroll
   (mouse wheel, Page Down or the scroll bar) or maximize the window to reach **Start game**.

*"Virus detected" when downloading?* Versions up to 1.2.0 were built with PyInstaller's stock
loader, which Windows Defender's machine-learning check sometimes reports as
`Trojan:Win32/Wacatac.B!ml` (a known false positive for Python programs packed into one exe).
Since 1.2.1 the exe uses a loader compiled for this project and scans clean. You can also run the
launcher from source (`python play_duskbloods.py`, see `build_exe.py` for the packages).

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
   `~/.local/share/duskbloods-launcher/venv` and installs three small libraries (`grpcio`,
   `cryptography`, `zstandard`). Later starts are instant. (If you get "Permission denied", run
   `chmod +x play-duskbloods.sh` once.)
4. In the window, pick the **game** (a `.nup` file, or the folder containing `base/exefs/main`) and the
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

## Using it (macOS)

The launcher runs on macOS (Apple Silicon and Intel). **It does not include an emulator**: you need
a macOS build of an emulator with Duskbloods (Switch 2) support.

1. Install Python 3 from <https://www.python.org/downloads/macos/> (it includes Tk).
2. Download `Play-Duskbloods-mac.zip` from the
   [Releases page](https://github.com/sikoGD/playDB-CNT/releases) and unzip it.
3. Double-click **Play Duskbloods.command**. The first time, macOS may say it is from an
   unidentified developer: right-click it, choose **Open**, then **Open** again.

The first start creates a private Python environment and installs `grpcio`, `cryptography` and
`zstandard`. Emulator apps (`Eden.app`, `Lichii.app`, ...) are found in `/Applications`,
`~/Applications`, `~/Downloads` and next to the launcher. Details: `mac/README-MAC.md`.

## Building the exe

```
pip install pyinstaller cryptography grpcio zstandard
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
