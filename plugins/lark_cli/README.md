# Lark CLI Plugin

This capability plugin integrates the official
[`@larksuite/cli`](https://github.com/larksuite/cli) executable with Hermes.
It is intentionally separate from the native Feishu platform plugin:

- the platform plugin owns inbound events, message delivery, WebSocket/webhook
  lifecycle, and channel routing;
- this plugin owns user-initiated business API operations such as Calendar,
  Messenger, Docs, Drive, Tasks, Mail, Approval, Sheets, and Base;
- `event consume` is not exposed, so enabling the plugin cannot create a second
  Feishu channel or duplicate message delivery.

## Runtime contract

- `@larksuite/cli` is pinned exactly in `package-lock.json`.
- Dovie production builds copy the native executable into
  `tool-runtime/bin/` and set `HERMES_LARK_CLI_BIN`.
- Every Agent profile gets isolated CLI config, data, logs, keychain
  references, and OAuth flow state below `$HERMES_HOME/.lark-cli`.
- Child processes use argv directly, never a shell, and receive an explicit
  environment allowlist.
- Official structured success/error envelopes are preserved. Output capture is
  bounded and process groups are terminated on timeout.

## Identity and authorization

`lark_cli_auth` binds the CLI to the Feishu app already configured in the
active Hermes profile. `bot-only` is the default. `user-default` requires an
explicit Hermes approval because it lets the Agent act under an authorized
user identity.

User OAuth is a two-turn device flow:

1. `action=start` requests minimum domains/scopes, generates a required PNG QR
   code, stores the device code in a private profile file, and returns only an
   opaque `authorization_id`.
2. After the user confirms authorization in a later turn, `action=complete`
   resumes the official CLI poll. The device code is redacted from all returned
   diagnostics and the flow is deleted after use.

## Approval boundary

Models cannot pass `--yes`, `--force`, `--as`, or `--profile` through raw
arguments. When the official CLI returns its confirmation-required exit
contract, the plugin requests a real Hermes approval and only retries the exact
argv with `--yes` after approval. A missing responder, silence, denial, or
timeout remains fail-closed.

## Developer checks

```bash
.venv/bin/python -m pytest -q tests/plugins/test_lark_cli_plugin.py
.venv/bin/python -m ruff check plugins/lark_cli tests/plugins/test_lark_cli_plugin.py
node_modules/@larksuite/cli/bin/lark-cli --version
```
