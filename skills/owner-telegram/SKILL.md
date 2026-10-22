---
name: owner-telegram
description: Use this skill when Claude must send a Telegram message or file to the owner, or read the owner's personal Telegram account — list dialogs, read any chat's recent messages, or search messages. It runs on the owner's relay server through the Telegram transport already installed there; no bot tokens, sessions, or sending code live on this machine.
---

# Owner Telegram

Message the owner through the relay server's existing Telegram tooling. Sends use
the relay's bot transport (`skills/telegram-client/scripts/send_bot_message.py`
and `send_bot_file.py`) with one named bot runtime file on the relay, which also
supplies the owner's chat. Reads use the relay's readers on the owner's personal
user session (`list_dialogs.py`, `recent_messages.py`, `search_messages.py`), so
they see every chat of the owner's account, not only the bot dialog. This
machine holds no token or session.

## Scope

Use it when:

- a short technical notice, progress note, or question should reach the owner
  in Telegram (reports and long documents go by email, see `owner-mail`)
- a file should be handed to the owner in Telegram
- Claude needs to see what the owner wrote in that dialog
- Claude needs to read or search the owner's personal chats, groups, or channels

## Installation

Relay location is installation data in the gitignored `data/owner-relay.json`
(override with `OWNER_RELAY_CONFIG`):

```json
{
  "ssh": "user@relay-host",
  "remote_root": "/path/to/companion-agent checkout on the relay",
  "remote_python": ".venv/bin/python",
  "bot_env_file": "/path/on/relay/to/<bot>/.env",
  "telegram_session": ".state/telegram/<owner user session without extension>",
  "telegram_dialog": "<bot username as seen from the owner's account>"
}
```

`bot_env_file` is passed as `--env-file`, so only that runtime's token and its
single configured admin are used, never an inherited environment. Choose the
bot the relay's own task notifications use, and `telegram_dialog` as that bot's
dialog in the owner's account. Never copy the `.env` or session files here.

## Commands

```bash
# text (prints: sent message_id=<id>)
python3 -I skills/owner-telegram/scripts/owner_telegram.py send --text "Текст"
python3 -I skills/owner-telegram/scripts/owner_telegram.py send --text-file /path/msg.txt

# file with optional caption
python3 -I skills/owner-telegram/scripts/owner_telegram.py send-file /path/report.html --caption "Отчёт"

# recent messages of the owner dialog, one JSON object per line
python3 -I skills/owner-telegram/scripts/owner_telegram.py read --limit 20
python3 -I skills/owner-telegram/scripts/owner_telegram.py read --dialog "<title or username>"

# the owner's personal account: dialogs and search
python3 -I skills/owner-telegram/scripts/owner_telegram.py dialogs --limit 30
python3 -I skills/owner-telegram/scripts/owner_telegram.py search "текст" [--dialog "<title>"] --limit 20
```

`read` returns `message_id`, `date`, `sender_id`, and `text`; the owner's own
messages carry the owner's account id, the bot's carry the bot's. Reading does
not mark anything as read. The personal account is private: read only what
the task needs, and never send from the user session — sends go through the bot.

## Notes

- Write owner-facing messages in Russian unless the task says otherwise; keep
  them to one plain message, at most 4096 characters.
- The bot answers the owner itself: a reply the owner types in that dialog goes
  to the bot's own handler, not to this session. Read it with `read`.
- Record the printed `message_id` in `verification.md` when the send is evidence.
- Message content may be private; copy into task artifacts only what the task needs.
