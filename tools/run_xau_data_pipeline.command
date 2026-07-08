#!/bin/zsh
set -e

cd "/Users/Cheelix/Documents/xau"

PYTHON="/opt/homebrew/bin/python3"
if [ ! -x "$PYTHON" ]; then
  PYTHON="/usr/bin/python3"
fi

echo "XAU data pipeline"
echo "Project: /Users/Cheelix/Documents/xau"
echo "Mode: data refresh (--skip-fred --open-html, GLD options attempted with cache fallback)"
echo

if [ -f "secrets/oanda.env" ]; then
  echo "OANDA token found. Refreshing XAU_USD/XAG_USD primary spot data..."
  "$PYTHON" tools/oanda_xauusd_fetch.py || echo "OANDA spot refresh failed; continuing with available local data."
  echo
fi

"$PYTHON" tools/xau_data_pipeline.py --skip-fred --open-html

echo
echo "Done. Press Enter to close this window."
read _
