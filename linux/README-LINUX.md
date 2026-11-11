# Play Duskbloods launcher - Linux

The launcher starts the stand-in servers (offline: 127.0.0.1 only, nothing is sent anywhere) and
then your emulator with the game. To play with friends (Online tab, room passwords), read
`ONLINE.md` in this folder. **It does not include an emulator**: you need a Linux build of
Eglantine (or Lichii / Eden / yuzu) that has the Duskbloods support.

## Requirements
- Python 3.8+ with Tk and venv: `sudo apt install python3 python3-venv python3-tk`
  (Fedora: `sudo dnf install python3 python3-tkinter`)
- A Linux emulator binary or AppImage named like `eglantine`, `eden`, `lichii`, `yuzu` or
  `Eglantine-*.AppImage`
- Your own `prod.keys` / `title.keys` and the game files (never bundled)

## Run
    tar xf Play-Duskbloods-linux.tar.gz
    cd Play-Duskbloods-linux
    ./play-duskbloods.sh

The first run creates a private Python environment in `~/.local/share/duskbloods-launcher/venv`
and installs `grpcio`, `cryptography` and `zstandard`.

The launcher looks for emulators next to itself, in `~/Applications`, `~/.local/bin`,
`~/Downloads` and on your `PATH`; use **Browse** for anything else. The game folder is the
`THE DUSKBLOODS NETWORK TEST VER` folder (containing `base/exefs/main`) or set `DUSK_GAME`.

## Where things live
| What | Path |
|------|------|
| Emulator data / keys | `~/.local/share/eden` (or `lichii`, `eglantine`, `yuzu`), or `<emulator folder>/user` for a portable build |
| Emulator config | `~/.config/eden/qt-config.ini` (or the portable `user/config`) |
| Saves and logs of this launcher | `<data folder>/duskbloods_test/` (logs in `runs/<time>/`) |
| Keys | `<data folder>/keys/prod.keys` |

## Troubleshooting
- "not executable": `chmod +x ./your-emulator` (the launcher also tries this itself).
- "Port 18443 is already in use": an earlier session is still running; close it.
- Open the **Open logs** button (uses `xdg-open`) and attach the newest `runs/<time>` folder when
  reporting a problem.
