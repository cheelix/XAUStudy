#!/bin/zsh
set -e

cd "/Users/Cheelix/Documents/xau"

PYTHON="/opt/homebrew/bin/python3"
if [ ! -x "$PYTHON" ]; then
  PYTHON="/usr/bin/python3"
fi

echo "OANDA XAU_USD/XAG_USD primary spot fetch"
echo "Project: /Users/Cheelix/Documents/xau"
echo

"$PYTHON" tools/oanda_xauusd_fetch.py

echo
echo "Done. Press Enter to close this window."
read _
