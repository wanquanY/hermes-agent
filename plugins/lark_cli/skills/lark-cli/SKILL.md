---
name: lark-cli
version: 0.1.0
description: Use the official Lark/Feishu CLI through Hermes with safe identity, authorization, and approval handling.
---

# Lark CLI

Use this Skill for Lark/Feishu business operations. The existing Feishu
platform channel owns incoming and outgoing chat transport; this plugin owns
business API operations and must not start a second event consumer.

## Required workflow

1. Call `lark_cli_status` before the first operation in a conversation.
2. If the CLI is not bound, call `lark_cli_auth` with `action=bind`.
   Use `identity=bot-only` unless the user explicitly needs personal calendar,
   mail, Drive, attendance, or another user-owned resource.
3. If user authorization is needed, call `lark_cli_auth` with `action=start`
   and the smallest relevant `domains` or exact `scopes`.
4. Show `verification_url` exactly as returned, then render the image at
   `qr_code_path` directly below it. End the turn and ask the user to confirm
   after authorizing. Do not call `complete` in the same turn.
5. After the user confirms, call `lark_cli_auth` with `action=complete` and the
   returned `authorization_id`.
6. Before a business operation, call `lark_cli_skill` with `action=read` and
   the matching official embedded Skill, for example `lark-calendar`. Read
   referenced files through the same tool when that Skill directs you to one.
7. Call `lark_cli_run` with one argv entry per `arguments` item. Never embed a
   shell command.

## Safety

- Prefer bot identity when it can complete the operation.
- Request only the minimum domains/scopes required.
- Never place app secrets, access tokens, refresh tokens, or device codes in a
  response.
- Never add `--yes`, `--force`, `--as`, or `--profile` to `arguments`. The
  plugin owns those policy fields.
- If the official CLI classifies an operation as high risk, the plugin routes
  it through the Hermes approval UI. Silence or timeout is not consent.
- Use `dry_run=true` when the official command supports a useful preview.
- Do not use `event consume` as a message channel; Hermes already owns Feishu
  channel delivery.

## Official guidance

The installed CLI embeds its version-matched official Skills. Discover them
with `lark_cli_skill action=list`; do not rely on remembered command syntax
when the embedded Skill can provide the exact current contract.
