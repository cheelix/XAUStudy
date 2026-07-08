# Local Secrets

This folder is for local API credentials only.

Do not paste real tokens into chat. Do not commit token files.

Expected OANDA file:

```text
secrets/oanda.env
```

Create it with:

```bash
zsh tools/setup_oanda_token.command
```

The real `*.env` files are ignored by Git.
