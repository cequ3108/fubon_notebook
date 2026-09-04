#!/usr/bin/env python3
"""Monitor Taiwan stock public subscriptions (公開申購) from HiStock.

Source: https://histock.tw/stock/public.aspx

Email: UANALYZE_EMAIL + GMAIL_APP_PASSWORD
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape, unescape
from pathlib import Path
from typing import Any

TPE = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = Path(
    os.getenv(
        "STOCK_SUB_STATE_PATH",
        ROOT / ".cursor" / "automation" / "stock_subscription_state.json",
    )
)
SOURCE_URL = "https://histock.tw/stock/public.aspx"
USER_AGENT = "Mozilla/5.0 (compatible; stock-subscription-monitor/1.0)"

ACTIVE_STATUSES = {"open", "closing_today", "upcoming"}


@dataclass
class Subscription:
    stock_code: str
    name: str
    lottery_date: str
    market: str
    apply_period: str
    allot_date: str
    underwrite_lots: int | None
    offer_price: float | None
    market_price: float | None
    profit_twd: int | None
    return_pct: float | None
    apply_lots: int | None
    qualified_apps: int | None
    win_rate_pct: float | None
    note: str
    status: str  # upcoming / open / closing_today / closed / unknown
    detail_url: str


def http_text(url: str, retries: int = 3) -> str:
    last_err: Exception | None = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise RuntimeError(f"無法讀取 {url}: {last_err}")


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = unescape(value)
    value = value.replace("\xa0", " ").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", value).strip()


def parse_number(value: str) -> float | None:
    text = clean_text(value).replace(",", "").replace("%", "").strip()
    if not text or text in {"-", "—", "N/A"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: str) -> int | None:
    num = parse_number(value)
    return int(num) if num is not None else None


def classify_status(note: str, apply_period: str, today: datetime.date) -> str:
    note = note.strip()
    if note == "已截止":
        return "closed"
    if note == "截止日":
        return "closing_today"
    if note == "申購中":
        return "open"
    # upcoming: period in the future, empty note
    m = re.search(r"(\d{2})/(\d{2})\s*~\s*(\d{2})/(\d{2})", apply_period)
    if m:
        y = today.year
        start = datetime(y, int(m.group(1)), int(m.group(2)), tzinfo=TPE).date()
        end = datetime(y, int(m.group(3)), int(m.group(4)), tzinfo=TPE).date()
        # year rollover safety (Dec -> Jan)
        if end < start:
            end = end.replace(year=y + 1)
        if today < start:
            return "upcoming"
        if start <= today <= end:
            return "open"
        return "closed"
    return "unknown" if not note else "unknown"


def fetch_subscriptions(today: datetime.date | None = None) -> list[Subscription]:
    today = today or datetime.now(TPE).date()
    html = http_text(SOURCE_URL)
    pattern = re.compile(
        r"<tr[^>]*>\s*<td[^>]*>\s*(\d{4}/\d{2}/\d{2})\s*</td>"
        r".*?<a href='/stock/([^']+)'[^>]*>\s*([^<]+?)\s*</a>"
        r"(.*?)</tr>",
        re.S | re.I,
    )
    rows: list[Subscription] = []
    for match in pattern.finditer(html):
        lottery_date, href, raw_name, rest = match.groups()
        cells = [clean_text(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", rest, re.S | re.I)]
        if len(cells) < 12:
            continue
        full_name = clean_text(raw_name)
        code = href.strip()
        # "7856 漢測" -> name without code
        name = re.sub(rf"^{re.escape(code)}\s*", "", full_name).strip() or full_name
        note = cells[11]
        apply_period = cells[1]
        status = classify_status(note, apply_period, today)
        rows.append(
            Subscription(
                stock_code=code,
                name=name,
                lottery_date=lottery_date.replace("/", "-"),
                market=cells[0],
                apply_period=apply_period,
                allot_date=cells[2],
                underwrite_lots=parse_int(cells[3]),
                offer_price=parse_number(cells[4]),
                market_price=parse_number(cells[5]),
                profit_twd=parse_int(cells[6]),
                return_pct=parse_number(cells[7]),
                apply_lots=parse_int(cells[8]),
                qualified_apps=parse_int(cells[9]),
                win_rate_pct=parse_number(cells[10]),
                note=note,
                status=status,
                detail_url=f"https://histock.tw/stock/{code}",
            )
        )
    return rows


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"subscriptions": {}}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def detect_alerts(
    current: list[Subscription], previous: dict[str, Any]
) -> tuple[list[Subscription], list[str]]:
    alerts: list[Subscription] = []
    reasons: list[str] = []
    prev_map = previous.get("subscriptions", {})
    for item in current:
        if item.status not in ACTIVE_STATUSES:
            continue
        prev = prev_map.get(item.stock_code)
        if prev is None:
            alerts.append(item)
            reasons.append(f"新標的：{item.name} ({item.stock_code}) [{item.note or item.status}]")
            continue
        if prev.get("status") != item.status:
            alerts.append(item)
            reasons.append(
                f"狀態變更：{item.name} {prev.get('status')} -> {item.status}"
                f"（{item.note or '無備註'}）"
            )
        elif item.status in {"open", "closing_today"} and (
            prev.get("offer_price") != item.offer_price
            or prev.get("market_price") != item.market_price
            or prev.get("return_pct") != item.return_pct
        ):
            alerts.append(item)
            reasons.append(f"行情更新：{item.name} 承銷價/市價/報酬率有變動")
    return alerts, reasons


def fmt_money(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value:,}"


def format_item_lines(item: Subscription) -> list[str]:
    ret = f"{item.return_pct:.1f}%" if item.return_pct is not None else "—"
    offer = f"{item.offer_price:g}" if item.offer_price is not None else "—"
    mkt = f"{item.market_price:g}" if item.market_price is not None else "—"
    win = f"{item.win_rate_pct:g}%" if item.win_rate_pct is not None else "—"
    status_zh = {
        "upcoming": "即將開始",
        "open": "申購中",
        "closing_today": "今日截止",
        "closed": "已截止",
        "unknown": "未知",
    }.get(item.status, item.status)
    lines = [
        f"{item.name} ({item.stock_code}) / {item.market} / {status_zh}",
        f"  申購期間：{item.apply_period} | 抽籤：{item.lottery_date} | 撥券：{item.allot_date}",
        f"  承銷價 {offer} 元 | 市價 {mkt} 元 | 預估獲利 {fmt_money(item.profit_twd)} 元"
        f"（報酬率 {ret}）",
        f"  承銷張數 {fmt_money(item.underwrite_lots)} | 申購張數 {item.apply_lots or '—'}"
        f" | 中籤率 {win}",
        f"  詳情：{item.detail_url}",
    ]
    if item.note:
        lines.append(f"  備註：{item.note}")
    if item.return_pct is not None:
        if item.return_pct >= 20:
            lines.append("  建議：報酬率偏高，可優先評估申購（仍需考量中籤率與資金凍結）。")
        elif item.return_pct >= 5:
            lines.append("  建議：有正報酬空間，可依資金與中籤率決定是否參與。")
        elif item.return_pct > 0:
            lines.append("  建議：報酬偏低，留意手續費與資金成本。")
        else:
            lines.append("  建議：目前市價低於承銷價，申購優勢較弱。")
    return lines


def render_text_report(
    focus: list[Subscription],
    active: list[Subscription],
    reasons: list[str],
) -> str:
    lines = [
        "【股票公開申購監控】",
        f"來源：{SOURCE_URL}",
        f"時間：{datetime.now(TPE).strftime('%Y-%m-%d %H:%M')}（台北）",
        f"進行中／即將開始：{len(active)} 檔",
        "",
    ]
    if reasons:
        lines.append("異動摘要：")
        for reason in reasons:
            lines.append(f"- {reason}")
        lines.append("")
    if not focus:
        lines.append("目前沒有需關注的申購標的。")
    else:
        lines.append("重點標的：")
        for item in focus:
            lines.extend(format_item_lines(item))
            lines.append("")
    if active and focus != active:
        lines.append("全部進行中／即將開始：")
        for item in active:
            ret = f"{item.return_pct:.1f}%" if item.return_pct is not None else "—"
            lines.append(
                f"- {item.name} ({item.stock_code}) {item.note or item.status}"
                f" | 報酬率 {ret} | {item.apply_period}"
            )
        lines.append("")
    lines.append("— 本報告僅供研究參考，不構成投資建議 —")
    return "\n".join(lines)


def render_html_report(text: str, focus: list[Subscription]) -> str:
    cards: list[str] = []
    for item in focus:
        ret_color = "#c62828" if (item.return_pct or 0) >= 0 else "#2e7d32"
        cards.append(
            "<div style='border:1px solid #e0e0e0;border-radius:10px;padding:12px;"
            "margin:0 0 12px;background:#fff;'>"
            f"<h3 style='margin:0 0 8px;'>{escape(item.name)} "
            f"({escape(item.stock_code)})</h3>"
            f"<p style='margin:0 0 6px;color:#555;'>{escape(item.market)}｜"
            f"{escape(item.note or item.status)}｜申購 {escape(item.apply_period)}</p>"
            f"<p style='margin:0;font-size:18px;color:{ret_color};font-weight:700;'>"
            f"報酬率 {escape(f'{item.return_pct:.1f}%' if item.return_pct is not None else '—')}"
            f"｜預估獲利 {escape(fmt_money(item.profit_twd))} 元</p>"
            f"<p style='margin:8px 0 0;'><a href='{escape(item.detail_url)}'>HiStock 詳情</a></p>"
            "</div>"
        )
    return (
        "<html><body style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        "line-height:1.5;color:#222;'>"
        "<h2>股票公開申購監控</h2>"
        + "".join(cards)
        + "<pre style='background:#f7f7f7;padding:12px;border-radius:8px;"
        "white-space:pre-wrap;'>"
        + escape(text)
        + "</pre>"
        "<p style='color:#666;font-size:12px;'>本報告僅供研究參考，不構成投資建議。"
        f"資料來源：<a href='{SOURCE_URL}'>HiStock 公開申購</a></p>"
        "</body></html>"
    )


def send_email(subject: str, text: str, html: str) -> None:
    email = os.environ.get("UANALYZE_EMAIL") or os.environ.get("CB_ALERT_EMAIL")
    password = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    if not email or not password:
        raise RuntimeError("缺少 UANALYZE_EMAIL / GMAIL_APP_PASSWORD，無法寄送 Email")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email
    msg["To"] = email
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(email, password)
        smtp.sendmail(email, [email], msg.as_string())


def run(args: argparse.Namespace) -> int:
    today = datetime.now(TPE).date()
    rows = fetch_subscriptions(today)
    active = [r for r in rows if r.status in ACTIVE_STATUSES]
    # prioritize: closing today -> open high return -> upcoming high return
    active.sort(
        key=lambda x: (
            0 if x.status == "closing_today" else 1 if x.status == "open" else 2,
            -(x.return_pct or -999),
        )
    )

    state = load_state()
    alerts, reasons = detect_alerts(rows, state)
    focus = alerts if alerts else active[:5]
    report = render_text_report(focus, active, reasons)
    print(report)

    should_notify = args.force_notify or (args.notify and bool(alerts))
    if should_notify and not args.dry_run:
        subject = "[股票申購]"
        if alerts:
            subject += f" {alerts[0].name} 等 {len(alerts)} 檔需關注"
        else:
            subject += " 監控摘要"
        html = render_html_report(report, focus)
        send_email(subject, report, html)
        print(f"\n已寄送 Email 至 {os.environ.get('UANALYZE_EMAIL')}")

    if not args.dry_run:
        # Persist active / upcoming / alerted rows for next-run diff.
        keep_codes = {r.stock_code for r in active} | {a.stock_code for a in alerts}
        save_state(
            {
                "last_run": datetime.now(TPE).isoformat(),
                "subscriptions": {
                    r.stock_code: asdict(r) for r in rows if r.stock_code in keep_codes
                },
            }
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor Taiwan stock public subscriptions")
    parser.add_argument("--dry-run", action="store_true", help="不寫入 state、不寄信")
    parser.add_argument("--notify", action="store_true", help="有異動才寄信")
    parser.add_argument("--force-notify", action="store_true", help="無論是否有異動都寄信")
    args = parser.parse_args()
    if args.force_notify:
        args.notify = True
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
