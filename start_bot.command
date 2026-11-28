#!/bin/bash
# Starts the bot in this Terminal window. Close the window (or press Ctrl+C) to stop it.
cd "$(dirname "$0")" || exit 1
echo "Odd Finder: http://localhost:8765  (close this window to stop the bot)"
exec /usr/bin/python3 app.py
