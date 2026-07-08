#!/bin/zsh
set -e

cd "/Users/Cheelix/Documents/xau"
mkdir -p secrets

echo "OANDA local token setup"
echo "Important: use a NEW token. Revoke any token that was pasted into chat or screenshots."
echo

printf "Environment [practice/live] (default: practice): "
read OANDA_ENV_VALUE
if [ -z "$OANDA_ENV_VALUE" ]; then
  OANDA_ENV_VALUE="practice"
fi

printf "Paste NEW OANDA API token: "
stty -echo
read OANDA_TOKEN_VALUE
stty echo
echo

printf "OANDA account ID (optional; leave blank to auto-detect first account): "
read OANDA_ACCOUNT_ID_VALUE

umask 077
cat > secrets/oanda.env <<EOF
OANDA_ENV=$OANDA_ENV_VALUE
OANDA_API_TOKEN=$OANDA_TOKEN_VALUE
OANDA_ACCOUNT_ID=$OANDA_ACCOUNT_ID_VALUE
OANDA_INSTRUMENT=XAU_USD
EOF

chmod 600 secrets/oanda.env
echo
echo "Saved to /Users/Cheelix/Documents/xau/secrets/oanda.env"
echo "Next test:"
echo "  python3 tools/oanda_xauusd_fetch.py"
echo
echo "Press Enter to close this window."
read _
