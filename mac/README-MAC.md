# Play Duskbloods launcher - macOS

The launcher starts the stand-in servers (offline: 127.0.0.1 only, nothing is sent anywhere) and
then your emulator with the game. To play with friends (Online tab, room passwords), read
`ONLINE.md` in this folder. **It does not include an emulator**: you need a macOS build of an
emulator with The Duskbloods (Switch 2) support, such as an Eglantine, Lichii or Eden build for Mac.

## Requirements
- macOS 11 or newer (Apple Silicon or Intel)
- Python 3.8+ with Tk: the installer from <https://www.python.org/downloads/macos/> includes Tk
  (Homebrew: `brew install python python-tk`)
- An emulator app such as `Eglantine.app`, `Lichii.app` or `Eden.app` (in `/Applications`,
  `~/Applications`, `~/Downloads` or next to the launcher; or use **Browse**)
- Your own `prod.keys` / `title.keys` and the game files (never bundled)

## Run
1. Unzip `Play-Duskbloods-mac.zip`.
2. Double-click **Play Duskbloods.command**. The first time, macOS may say it is from an
   unidentified developer: right-click it, choose **Open**, then **Open** again.

The first run creates a private Python environment in
`~/Library/Application Support/duskbloods-launcher/venv` and installs `grpcio`, `cryptography` and
`zstandard`.

## Where things live
| What | Path |
|------|------|
| Emulator data / keys | `~/Library/Application Support/eden` (or `lichii`, `eglantine`, `yuzu`), or `<emulator folder>/user` for a portable build |
| Launcher saves and logs | `<emulator data>/duskbloods_test` (**Open logs**) |
| Bot Match text mod | `<emulator data>/load/0400D1D02399A000/Bot Match (playDB-CNT)` |

`tools/ooz/ooz` is a universal (Apple Silicon + Intel) build of the open-source Kraken
decompressor; the launcher uses it to add the Bot Match name and description. Its source is in
`tools/ooz/source` (GPL-3.0).
