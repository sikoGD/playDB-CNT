#!/usr/bin/env sh
# Play Duskbloods launcher for Linux. First run sets up a private Python environment
# (grpcio + cryptography, ~30 MB) under ~/.local/share/duskbloods-launcher, then starts the GUI.
set -e
cd "$(dirname "$(readlink -f "$0")")"
[ -f play_duskbloods.py ] || cd ..   # running from a repository checkout (script is in linux/)

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "Python 3 is required (sudo apt install python3 python3-venv python3-tk)." >&2
    exit 1
fi
if ! "$PY" -c 'import tkinter' >/dev/null 2>&1; then
    echo "Tk is missing. Install it with: sudo apt install python3-tk   (Fedora: sudo dnf install python3-tkinter)" >&2
    exit 1
fi

VENV="${XDG_DATA_HOME:-$HOME/.local/share}/duskbloods-launcher/venv"
if [ ! -x "$VENV/bin/python" ]; then
    echo "Setting up the launcher (first run only)..."
    "$PY" -m venv "$VENV" || { echo "python3-venv is missing: sudo apt install python3-venv" >&2; exit 1; }
fi
if ! "$VENV/bin/python" -c 'import grpc, cryptography' >/dev/null 2>&1; then
    "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check grpcio cryptography
fi

exec "$VENV/bin/python" play_duskbloods.py "$@"
