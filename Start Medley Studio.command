#!/bin/bash
# Double-click this file in Finder to start the Medley Studio local server
# and open the editor in your default browser. Keep the Terminal window open
# while you're using the editor — closing it stops the server.

set -e
cd "$(dirname "$0")"

# Pick whatever python3 the user has. python is fine if python3 isn't there
# (rare on modern macOS but harmless to fall back).
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "ERROR: Python 3 is not installed."
  echo "Install it from https://www.python.org/downloads/ and try again."
  read -n 1 -s -r -p "Press any key to close this window…"
  exit 1
fi

echo "Starting Medley Studio at http://localhost:8765/"
echo "Close this window when you're done to stop the server."
echo ""
exec "$PY" medley_server.py --open
