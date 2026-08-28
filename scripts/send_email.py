#!/usr/bin/env python3
"""Send a plain-text status email via SMTP, credentials from the environment.

Used by scripts/warm_cache_nightly.sh to report the nightly warm-cache
status. Credentials live in ~/.astor_notify.env (outside the repo, mode
600), never in source control -- this script only ever reads them from
the environment, so it has no secret to accidentally commit.

Usage: send_email.py "subject line" < body.txt
"""

from __future__ import annotations

import os
import smtplib
import sys
from email.mime.text import MIMEText


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: send_email.py SUBJECT  (body on stdin)", file=sys.stderr)
        return 2

    subject = sys.argv[1]
    body = sys.stdin.read()

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    to = os.environ.get("SMTP_TO", user)

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.sendmail(user, [to], msg.as_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
