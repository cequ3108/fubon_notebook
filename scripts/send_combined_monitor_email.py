#!/usr/bin/env python3
"""Send one combined email with CB auction + stock subscription share cards."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path

import smtplib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import monitor_cb_auction as cb  # noqa: E402
import monitor_stock_subscription as sub  # noqa: E402

TPE = timezone(timedelta(hours=8))


def collect_auction_cards(limit: int = 5):
    """Collect active TWSE auction cards (CB + stock)."""
    today = datetime.now(TPE).date()
    raw = cb.fetch_auctions()
    ipo_map = cb.fetch_cb_ipo_table()
    cb_stats = cb.historical_premium_stats(raw, asset_kind="cb")
    stock_stats = cb.historical_premium_stats(raw, asset_kind="stock")
    shares_map = cb.load_shares_outstanding()
    stock_cache: dict = {}
    bond_cache: dict = {}
    purpose_cache: dict = {}
    rows = []
    for row in raw:
        basic = cb.normalize_auction(row, ipo_map, cb_stats, today, enrich=False)
        if basic.status not in ("bidding", "upcoming", "awaiting_result"):
            continue
        rows.append(
            cb.normalize_auction(
                row,
                ipo_map,
                cb_stats if basic.asset_kind == "cb" else stock_stats,
                today,
                enrich=True,
                stock_cache=stock_cache,
                bond_cache=bond_cache,
                shares_map=shares_map if basic.asset_kind == "cb" else None,
                purpose_cache=purpose_cache if basic.asset_kind == "cb" else None,
                stock_premium_stats=stock_stats,
            )
        )
    rows = [a for a in rows if a.position and a.position.tickets]
    # Prefer mix: stocks first if present, then CB
    stocks = [a for a in rows if a.asset_kind == "stock"]
    cbs = [a for a in rows if a.asset_kind == "cb"]
    focus = (stocks + cbs)[:limit]
    return focus, cb.generate_share_cards(focus)


def collect_sub_cards(limit: int = 5):
    today = datetime.now(TPE).date()
    rows = sub.fetch_subscriptions(today)
    active = [r for r in rows if r.status in sub.ACTIVE_STATUSES]
    active.sort(
        key=lambda x: (
            0 if x.status == "closing_today" else 1 if x.status == "open" else 2,
            -(x.return_pct or -999),
        )
    )
    focus = active[:limit]
    return focus, sub.generate_share_cards(focus)


def send_combined(cb_pairs, sub_pairs) -> None:
    from email_util import resolve_recipients, sender_credentials

    email, password = sender_credentials()
    recipients = resolve_recipients(email)

    now = datetime.now(TPE).strftime("%Y-%m-%d %H:%M")
    text_lines = [
        "【監控圖卡】TWSE 競拍（可轉債+股票） + 公開申購",
        f"時間：{now}（台北）",
        "",
        f"競拍圖卡：{len(cb_pairs)} 張",
    ]
    for a, p in cb_pairs:
        kind = "可轉債" if getattr(a, "asset_kind", "cb") == "cb" else "股票"
        text_lines.append(f"- [{kind}] {a.name} ({a.bond_code}) → {p.name}")
    text_lines.append("")
    text_lines.append(f"股票申購圖卡：{len(sub_pairs)} 張")
    for a, p in sub_pairs:
        text_lines.append(f"- {a.name} ({a.stock_code}) → {p.name}")
    text_lines.append("")
    text_lines.append("本信僅供研究分享，不構成投資建議。")
    text = "\n".join(text_lines)

    html_parts = [
        "<html><body style='font-family:sans-serif;color:#111;line-height:1.5;'>",
        "<h2>監控圖卡：TWSE 競拍 + 公開申購</h2>",
        f"<p>產生時間：{escape(now)}（台北）</p>",
        f"<h3>TWSE 競拍圖卡（{len(cb_pairs)}）</h3>",
    ]
    for a, p in cb_pairs:
        kind = "可轉債" if getattr(a, "asset_kind", "cb") == "cb" else "股票"
        html_parts.append(
            f"<p style='font-weight:600;margin:12px 0 6px;'>[{escape(kind)}] "
            f"{escape(a.name)} ({escape(a.bond_code)})</p>"
            f"<img src='cid:{escape(p.stem)}' style='max-width:100%;border-radius:12px;'/>"
        )
    html_parts.append(f"<h3>股票公開申購圖卡（{len(sub_pairs)}）</h3>")
    for a, p in sub_pairs:
        html_parts.append(
            f"<p style='font-weight:600;margin:12px 0 6px;'>{escape(a.name)} "
            f"({escape(a.stock_code)})</p>"
            f"<img src='cid:{escape(p.stem)}' style='max-width:100%;border-radius:12px;'/>"
        )
    html_parts.append(
        "<p style='color:#666;font-size:12px;'>僅供研究分享，不構成投資建議。</p>"
        "</body></html>"
    )
    html = "".join(html_parts)

    all_paths = [p for _, p in cb_pairs] + [p for _, p in sub_pairs]

    def build_msg(to_addr: str) -> MIMEMultipart:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = f"[監控] 競拍 {len(cb_pairs)} + 申購 {len(sub_pairs)} 圖卡"
        msg["From"] = email
        msg["To"] = to_addr

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(text, "plain", "utf-8"))
        related = MIMEMultipart("related")
        related.attach(MIMEText(html, "html", "utf-8"))
        for path in all_paths:
            with path.open("rb") as f:
                img = MIMEImage(f.read(), _subtype="png")
            img.add_header("Content-ID", f"<{path.stem}>")
            img.add_header("Content-Disposition", "inline", filename=path.name)
            related.attach(img)
        alt.attach(related)
        msg.attach(alt)

        for path in all_paths:
            with path.open("rb") as f:
                att = MIMEImage(f.read(), _subtype="png")
            att.add_header("Content-Disposition", "attachment", filename=path.name)
            msg.attach(att)
        return msg

    # 逐一寄送，避免收件人互看信箱
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(email, password)
        for to_addr in recipients:
            smtp.sendmail(email, [to_addr], build_msg(to_addr).as_string())

    print(f"已分別寄送合併信至 {len(recipients)} 位收件人")
    print(f"競拍圖卡 {len(cb_pairs)}、申購圖卡 {len(sub_pairs)}，附件共 {len(all_paths)} 張")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auction-limit", type=int, default=5)
    parser.add_argument("--sub-limit", type=int, default=5)
    args = parser.parse_args()
    auction_focus, auction_pairs = collect_auction_cards(args.auction_limit)
    sub_focus, sub_pairs = collect_sub_cards(args.sub_limit)
    print(f"Auction focus: {[(a.asset_kind, a.name) for a in auction_focus]}")
    print(f"Sub focus: {[a.name for a in sub_focus]}")
    for _, p in auction_pairs + sub_pairs:
        print(f"  card: {p}")
    send_combined(auction_pairs, sub_pairs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
