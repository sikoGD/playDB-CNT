#!/bin/sh
# Play Duskbloods launcher for macOS. Double-click to start. The first run sets up a private Python
# environment (grpcio + cryptography + zstandard, ~35 MB) under
# ~/Library/Application Support/duskbloods-launcher, then starts the window.
cd "$(dirname "$0")"
[ -f play_duskbloods.py ] || cd ..   # running from a repository checkout (script is in mac/)

# Files from a downloaded zip are quarantined; the launcher runs its own tools (the servers, ooz).
xattr -dr com.apple.quarantine . 2>/dev/null || true

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for candidate in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
                     /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import tkinter' >/dev/null 2>&1; then
            PY="$candidate"
            break
        fi
    done
fi
if [ -z "$PY" ]; then
    osascript -e 'display alert "Play Duskbloods needs Python 3 with Tk" message "Install Python 3 from https://www.python.org/downloads/macos/ (it includes Tk), then open Play Duskbloods again."' >/dev/null 2>&1
    echo "Python 3 with Tk is required: install it from https://www.python.org/downloads/macos/" >&2
    exit 1
fi

VENV="$HOME/Library/Application Support/duskbloods-launcher/venv"
if [ ! -x "$VENV/bin/python" ]; then
    echo "Setting up the launcher (first run only)..."
    "$PY" -m venv "$VENV" || { echo "Could not create the Python environment." >&2; exit 1; }
fi
if ! "$VENV/bin/python" -c 'import grpc, cryptography, zstandard' >/dev/null 2>&1; then
    "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check grpcio cryptography zstandard
fi

exec "$VENV/bin/python" play_duskbloods.py "$@"
