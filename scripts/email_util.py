#!/usr/bin/env python3
"""Shared Gmail helpers for monitor alert emails."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECIPIENTS_FILE = Path(
    os.getenv(
        "MONITOR_ALERT_RECIPIENTS_FILE",
        ROOT / ".cursor" / "automation" / "alert_recipients.txt",
    )
)


def sender_credentials() -> tuple[str, str]:
    email = os.environ.get("UANALYZE_EMAIL") or os.environ.get("CB_ALERT_EMAIL")
    password = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    if not email or not password:
        raise RuntimeError("缺少 UANALYZE_EMAIL / GMAIL_APP_PASSWORD，無法寄送 Email")
    return email, password


def resolve_recipients(sender: str | None = None) -> list[str]:
    """Return unique recipients: sender + MONITOR_ALERT_EMAILS + recipients file."""
    emails: list[str] = []

    def add(addr: str) -> None:
        addr = addr.strip()
        if addr and "@" in addr and addr.lower() not in {e.lower() for e in emails}:
            emails.append(addr)

    if sender:
        add(sender)
    else:
        add(os.environ.get("UANALYZE_EMAIL") or os.environ.get("CB_ALERT_EMAIL") or "")

    for part in (os.environ.get("MONITOR_ALERT_EMAILS") or "").split(","):
        add(part)

    if RECIPIENTS_FILE.exists():
        for line in RECIPIENTS_FILE.read_text(encoding="utf-8").splitlines():
            add(line.split("#", 1)[0])

    if not emails:
        raise RuntimeError("沒有可寄送的收件人")
    return emails
