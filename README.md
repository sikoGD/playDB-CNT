# playDB-CNT - Play Duskbloods (Network Test) launcher

A small Windows launcher for running **The Duskbloods Network Test** on Eglantine-based Switch 2
emulators (Eglantine, Lichii, Eden-style builds) with the online features served by local,
loopback-only stand-in servers.

It gives you a window styled after the game's menus where you pick:

* **the game folder** (the folder that contains `base/exefs/main`; the version is checked), and
* **the emulator** (`eglantine.exe`, `lichii.exe`, `eden.exe` or `yuzu.exe`), found automatically
  next to the launcher, in sibling build folders and in a `Lichii` folder, or chosen with Browse.

Press **PLAY** and it starts the stand-in servers, launches the emulator with the game, and stops
the servers when the game closes. Logs are written next to the emulator's data (`Open logs`).

## What is in here

| Path | What |
|---|---|
| `play_duskbloods.py` | the launcher (Tk window, game/emulator discovery, process management) |
| `tools/npln_server/` | local stand-in for the online login/session service (listens on 127.0.0.1) |
| `tools/dusk_server/` | local "front door" stand-in for the game's own web API (127.0.0.1) |
| `assets/` | window art and the icon, taken from the game's title screen |
| `build_exe.py` | builds the single-file `Play Duskbloods.exe` with PyInstaller |

## Using it

1. Download `Play Duskbloods.exe` from the Releases page, put it anywhere (next to your emulator
   build is easiest).
2. Run it, pick the game folder and the emulator.
3. Have your own `prod.keys` and `title.keys` in the emulator's `keys` folder (portable builds:
   `user/keys`; Lichii/Eden: `%APPDATA%\eden\keys`). The launcher tells you if they are missing.
4. Press **PLAY**.

On the first run the launcher creates private test certificates for your PC (in the emulator's
data folder) and seeds an empty test NAND/save. Packaged builds that already have a
`user/test_data` folder keep using it.

## Building the exe

```
pip install pyinstaller cryptography grpcio
python build_exe.py
```

The exe appears in `dist/`. Python 3.10+ with Tk is required.

## Privacy and safety

* The servers bind to **127.0.0.1 only**; nothing is sent to any real service. The emulator is
  started in its loopback-only online mode.
* No keys, game files, saves or certificates are in this repository or the exe. Certificates are
  generated on your own PC the first time you play.
* The launcher stores only your two selected paths in `launcher_settings.json` beside the exe.

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
