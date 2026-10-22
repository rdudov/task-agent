#!/usr/bin/env python3
"""Message the owner and read the owner's Telegram account through the relay host.

Nothing here talks to Telegram. Sends run the relay's own bot transport with the
relay's own bot runtime file; reads (dialogs, history, search) run the relay's own
user-account readers on the owner's personal session. Files are copied to a scratch directory on the relay and removed after.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = REPO / "data" / "owner-relay.json"
REQUIRED = ("ssh", "remote_root", "remote_python", "bot_env_file",
            "telegram_session", "telegram_dialog")
SCRIPTS = "skills/telegram-client/scripts"


def load_config() -> dict:
    path = Path(os.environ.get("OWNER_RELAY_CONFIG", DEFAULT_CONFIG))
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"owner relay config unreadable: {path}: {exc}")
    missing = [key for key in REQUIRED if not config.get(key)]
    if missing:
        sys.exit(f"owner relay config {path} lacks: {', '.join(missing)}")
    return config


def ssh(config: dict, command: str) -> str:
    proc = subprocess.run(["ssh", "-o", "BatchMode=yes", config["ssh"], command],
                          text=True, capture_output=True)
    if proc.returncode != 0:
        sys.exit(f"remote command failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def relay(config: dict, script: str, *args: str) -> str:
    q = shlex.quote
    command = " ".join([q(config["remote_python"]), f"{SCRIPTS}/{script}", *map(q, args)])
    return ssh(config, f"cd {q(config['remote_root'])} && {command}").strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    send = sub.add_parser("send", help="Send a text message to the owner.")
    text = send.add_mutually_exclusive_group(required=True)
    text.add_argument("--text")
    text.add_argument("--text-file", type=Path)
    send_file = sub.add_parser("send-file", help="Send one file to the owner.")
    send_file.add_argument("file", type=Path)
    send_file.add_argument("--caption")
    read = sub.add_parser("read", help="Print recent messages of one dialog of the owner's account.")
    read.add_argument("--limit", type=int, default=20)
    read.add_argument("--dialog", help="Dialog title or username; default: the bot dialog from config.")
    dialogs = sub.add_parser("dialogs", help="List dialogs of the owner's account.")
    dialogs.add_argument("--limit", type=int, default=30)
    search = sub.add_parser("search", help="Search messages in the owner's account.")
    search.add_argument("query")
    search.add_argument("--dialog", help="Restrict to one dialog title or username.")
    search.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    config = load_config()
    env = ("--env-file", config["bot_env_file"])

    if args.action == "send":
        body = args.text if args.text is not None else args.text_file.read_text(encoding="utf-8")
        if not body.strip():
            sys.exit("empty message")
        print(relay(config, "send_bot_message.py", *env, "--", body))
    elif args.action == "send-file":
        if not args.file.is_file():
            sys.exit(f"not a file: {args.file}")
        remote = ssh(config, "mktemp -d /tmp/claude-owner-telegram.XXXXXX").strip()
        try:
            subprocess.run(["scp", "-q", "-o", "BatchMode=yes", str(args.file),
                            f"{config['ssh']}:{remote}/"], check=True)
            caption = ("--caption", args.caption) if args.caption else ()
            print(relay(config, "send_bot_file.py", *env, *caption, "--",
                        f"{remote}/{args.file.name}"))
        finally:
            ssh(config, f"rm -rf {shlex.quote(remote)}")
    else:
        session = ("--session", config["telegram_session"])
        limit = ("--limit", str(args.limit))
        if args.action == "dialogs":
            print(relay(config, "list_dialogs.py", *session, *limit))
        elif args.action == "search":
            dialog = ("--dialog", args.dialog) if args.dialog else ()
            print(relay(config, "search_messages.py", *session, "--query", args.query,
                        *dialog, *limit))
        else:
            print(relay(config, "recent_messages.py", *session,
                        "--dialog", args.dialog or config["telegram_dialog"], *limit))


if __name__ == "__main__":
    main()
