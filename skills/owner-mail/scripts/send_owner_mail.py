#!/usr/bin/env python3
"""Send one email to the owner through the relay host's existing Gmail sender.

Nothing here talks to Gmail. The payload is copied to a scratch directory on the
relay host over ssh, the relay's own `send_email.py` sends it with the relay's
own OAuth state and recipient allowlist, and the scratch directory is removed.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shlex
import subprocess
import sys
import tempfile
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = REPO / "data" / "owner-relay.json"
REQUIRED = ("ssh", "remote_root", "remote_python", "mail_config")
SENDER = "skills/gmail-client/scripts/send_email.py"


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


def ssh(config: dict, command: str, *, capture: bool = True) -> str:
    proc = subprocess.run(["ssh", "-o", "BatchMode=yes", config["ssh"], command],
                          text=True, capture_output=capture)
    if proc.returncode != 0:
        sys.exit(f"remote command failed ({proc.returncode}): {(proc.stderr or '').strip()}")
    return proc.stdout or ""


def owner_address(config: dict) -> str:
    """The relay's own notification recipient; send_email.py re-checks the allowlist."""
    script = ("import json,sys;"
              "print(json.load(open(sys.argv[1]))['notification_recipient'])")
    command = (f"cd {shlex.quote(config['remote_root'])} && python3 -c "
               f"{shlex.quote(script)} {shlex.quote(config['mail_config'])}")
    address = ssh(config, command).strip()
    if "@" not in address:
        sys.exit("relay mail config has no notification_recipient")
    return address


def raw_message(to: str, subject: str, plain: str, html: str,
                attachments: list[Path]) -> bytes:
    message = EmailMessage(policy=SMTP)
    message["To"] = to
    message["Subject"] = subject
    message.set_content(plain)
    message.add_alternative(html, subtype="html")
    for path in attachments:
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        maintype, subtype = kind.split("/", 1)
        message.add_attachment(path.read_bytes(), maintype=maintype,
                               subtype=subtype, filename=path.name)
    return bytes(message)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", required=True)
    body = parser.add_mutually_exclusive_group(required=True)
    body.add_argument("--body", help="Plain text / lightweight Markdown body.")
    body.add_argument("--body-file", type=Path)
    parser.add_argument("--html-body", type=Path,
                        help="Exact HTML body; --body stays the text/plain part.")
    parser.add_argument("--attach", type=Path, action="append", default=[])
    parser.add_argument("--to", help="Override recipient; must be in the relay allowlist.")
    args = parser.parse_args()

    for path in [*args.attach, *([args.html_body] if args.html_body else []),
                 *([args.body_file] if args.body_file else [])]:
        if not path.is_file():
            sys.exit(f"not a file: {path}")
    names = [path.name for path in args.attach]
    if len(set(names)) != len(names):
        sys.exit("attachment basenames must be unique")
    text = args.body if args.body is not None else args.body_file.read_text(encoding="utf-8")

    config = load_config()
    to = args.to or owner_address(config)
    remote = ssh(config, "mktemp -d /tmp/claude-owner-mail.XXXXXX").strip()
    try:
        with tempfile.TemporaryDirectory() as local:
            local_dir = Path(local)
            uploads: list[Path] = []
            if args.html_body:
                eml = local_dir / "message.eml"
                eml.write_bytes(raw_message(to, args.subject, text,
                                            args.html_body.read_text(encoding="utf-8"),
                                            args.attach))
                uploads.append(eml)
            else:
                body_file = local_dir / "body.md"
                body_file.write_text(text, encoding="utf-8")
                uploads += [body_file, *args.attach]
            subprocess.run(["scp", "-q", "-o", "BatchMode=yes", *map(str, uploads),
                            f"{config['ssh']}:{remote}/"], check=True)

        q = shlex.quote
        command = [q(config["remote_python"]), SENDER, "--to", q(to)]
        if args.html_body:
            command += ["--raw-message", q(f"{remote}/message.eml")]
        else:
            command += ["--subject", q(args.subject), "--body", f'"$(cat {q(remote + "/body.md")})"']
            for name in names:
                command += ["--attach", q(f"{remote}/{name}")]
        print(ssh(config, f"cd {q(config['remote_root'])} && {' '.join(command)}").strip())
    finally:
        subprocess.run(["ssh", "-o", "BatchMode=yes", config["ssh"],
                        f"rm -rf {shlex.quote(remote)}"], check=False)


if __name__ == "__main__":
    main()
