---
name: owner-mail
description: Use this skill when Claude must send an email to the owner (plain text, an exact HTML body, and/or attachments such as an HTML report). The send runs on the owner's relay server through the Gmail sender already installed there; no mail credentials or sending code live on this machine.
---

# Owner Mail

Send one letter to the owner through the relay server's existing Gmail client.
This machine holds neither OAuth state nor the Gmail API code: the script copies
the payload to a scratch directory on the relay over ssh, runs the relay's own
`skills/gmail-client/scripts/send_email.py`, and removes the scratch directory.

## Scope

Use it when:

- a task result, report, or question must reach the owner by email
- a finished HTML document should be delivered as an attachment or as the body

Do not use it for third-party recipients: the relay sender refuses any address
outside its own `allowed_recipients` list, and that refusal is the boundary.

## Installation

The relay location is installation data, kept in the gitignored
`data/owner-relay.json` (override with `OWNER_RELAY_CONFIG`):

```json
{
  "ssh": "user@relay-host",
  "remote_root": "/path/to/companion-agent checkout on the relay",
  "remote_python": ".venv/bin/python",
  "mail_config": ".state/gmail/<runtime>.json"
}
```

`remote_root` is the relay checkout that owns `skills/gmail-client/`.
`mail_config` is that checkout's mail runtime config; the owner address is its
`notification_recipient`, and the sender checks it against `allowed_recipients`.
The file names places only, never secrets. Do not copy tokens, `.env` files, or
sender code from the relay to this machine.

## Send

Text or lightweight Markdown body (the relay renders matching `text/plain` and
`text/html` parts), optional attachments:

```bash
python3 -I skills/owner-mail/scripts/send_owner_mail.py \
  --subject "Тема" --body-file /path/body.md --attach /path/report.html
```

Exact HTML body, with `--body`/`--body-file` as the `text/plain` alternative;
the message is built locally with the Python standard library and uploaded as raw
MIME (`send_email.py --raw-message`):

```bash
python3 -I skills/owner-mail/scripts/send_owner_mail.py \
  --subject "Тема" --body "Краткий текст" --html-body /path/letter.html \
  --attach /path/data.csv
```

Flags: `--body` or `--body-file` (one required), `--html-body`, repeatable
`--attach` (unique basenames), `--to` (only an allowlisted address).

Success prints the relay's `Email sent. Message ID: <gmail-id>`; record that id
in the task's `verification.md` when the send is evidence. A non-zero exit means
nothing was confirmed sent. The relay also keeps its own sent-message receipt.

## Notes

- Write owner-facing letters in Russian unless the task says otherwise.
- A detailed document goes as one HTML attachment; a short status goes in the
  body without a duplicate attachment.
- If the relay reports an invalid Gmail token, its sender starts its own reauth
  flow and prints a Google URL; pass it to the owner rather than working around it.
