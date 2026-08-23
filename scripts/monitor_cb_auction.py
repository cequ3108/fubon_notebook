#!/usr/bin/env python3
"""Monitor Taiwan CB auction announcements and suggest bid ranges.

Data sources:
- Auction list: money-link imp09_tw JSON API
- CB terms (conversion price, premium): cyclesinvest cbipo table
- Stock price: TWSE/TPEx daily close via TWSE open API
- Bond terms (putback, coupon): money-link bnd001_tw when listed

Notification uses Gmail (UANALYZE_EMAIL + GMAIL_APP_PASSWORD secrets).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from typing import Any

TPE = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = Path(os.getenv("CB_AUCTION_STATE_PATH", ROOT / ".data" / "cb_auction_state.json"))
MONEY_LINK_AUCTION = "https://www.money-link.com.tw/p/?G=m&pg=imp09_tw&id="
MONEY_LINK_BOND = "https://www.money-link.com.tw/p/?G=m&pg=bnd001_tw&id={stock}"
CYCLES_CB_IPO = "https://www.cyclesinvest.com/cbipo.php"
TWSE_DAY = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={date}&stockNo={stock}&response=json"
USER_AGENT = "Mozilla/5.0 (compatible; cb-auction-monitor/1.0)"


@dataclass
class CbIpoMeta:
    stock_code: str
    bond_code: str
    name: str
    tcri: str = ""
    collateral: str = ""
    issue_amount_100m: float | None = None
    broker: str = ""
    auction_schedule: str = ""
    premium_pct: float | None = None
    conversion_price: float | None = None
    listing_date: str = ""


@dataclass
class AuctionRow:
    bond_code: str
    name: str
    bond_type: str
    auction_method: str
    market_label: str
    bid_period: str
    open_date: str
    listing_date: str
    broker: str
    auction_lots: int | None
    floor_price: float
    min_lot: int
    min_win_price: float | None = None
    max_win_price: float | None = None
    underwriting_price: float | None = None
    cancelled: str = ""
    status: str = "unknown"
    stock_code: str = ""
    meta: CbIpoMeta | None = None
    stock_price: float | None = None
    parity: float | None = None
    bid_low: float | None = None
    bid_high: float | None = None
    fair_value: float | None = None
    advice: str = ""
    notes: list[str] = field(default_factory=list)


def http_get_json(url: str, timeout: int = 30) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def http_get_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def ts_to_date(ts: int | None) -> date | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, TPE).date()


def fmt_date(ts: int | None) -> str:
    d = ts_to_date(ts)
    return d.isoformat() if d else "-"


def infer_stock_code(bond_code: str) -> str:
    digits = re.sub(r"\D", "", bond_code)
    if len(digits) >= 4:
        return digits[:4]
    return digits


def fetch_auctions() -> list[dict[str, Any]]:
    data = http_get_json(MONEY_LINK_AUCTION)
    return list(data.get("result", {}).get("d1") or [])


def fetch_cb_ipo_table() -> dict[str, CbIpoMeta]:
    html = http_get_text(CYCLES_CB_IPO)
    rows: dict[str, CbIpoMeta] = {}
    for row_html in re.findall(r"<tr[^>]*>.*?</tr>", html, re.S):
        texts = [t.strip() for t in re.findall(r">([^<]+)<", row_html) if t.strip()]
        if len(texts) < 16:
            continue
        if not re.fullmatch(r"\d{4,5}", texts[2]) or not re.fullmatch(r"\d{4,5}", texts[3]):
            continue
        stock, bond, name = texts[2], texts[3], texts[4]
        tcri_col = texts[5]
        tcri_parts = tcri_col.split("/")
        premium = None
        conv = None
        try:
            if texts[14].endswith("%"):
                premium = float(texts[14].rstrip("%"))
        except ValueError:
            pass
        try:
            conv = float(texts[15])
        except ValueError:
            pass
        amount_val = None
        try:
            amount_val = float(texts[6])
        except ValueError:
            pass
        rows[bond] = CbIpoMeta(
            stock_code=stock,
            bond_code=bond,
            name=name,
            tcri=tcri_parts[0].replace("TCRI", "") if tcri_parts else "",
            collateral=tcri_parts[1] if len(tcri_parts) > 1 else "",
            issue_amount_100m=amount_val,
            broker=texts[7],
            auction_schedule=texts[11],
            premium_pct=premium,
            conversion_price=conv,
            listing_date=texts[16] if len(texts) > 16 else "",
        )
    return rows


def fetch_stock_close(stock_code: str, as_of: date | None = None, cache: dict[str, float | None] | None = None) -> float | None:
    if cache is not None and stock_code in cache:
        return cache[stock_code]
    as_of = as_of or datetime.now(TPE).date()
    price = None
    for offset in range(0, 10):
        d = as_of - timedelta(days=offset)
        if d.weekday() >= 5:
            continue
        q = urllib.parse.urlencode({
            "date": f"{d.year}{d.month:02d}{d.day:02d}",
            "stockNo": stock_code,
            "response": "json",
        })
        url = f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?{q}"
        try:
            data = http_get_json(url)
        except (urllib.error.URLError, json.JSONDecodeError):
            continue
        if data.get("stat") != "OK" or not data.get("data"):
            continue
        fields = data.get("fields") or []
        close_idx = fields.index("收盤價") if "收盤價" in fields else 6 if len(fields) > 6 else 1
        for row in reversed(data["data"]):
            if len(row) <= close_idx or row[0] == "月平均收盤價":
                continue
            try:
                price = float(str(row[close_idx]).replace(",", ""))
                break
            except ValueError:
                continue
        if price is not None:
            break
    if cache is not None:
        cache[stock_code] = price
    return price


def fetch_bond_terms(stock_code: str, bond_code: str) -> dict[str, Any]:
    try:
        data = http_get_json(MONEY_LINK_BOND.format(stock=stock_code))
    except urllib.error.URLError:
        return {}
    for row in data.get("result", {}).get("d1") or []:
        if str(row.get("v1")) == bond_code:
            return row
    return {}


def historical_premium_stats(raw_rows: list[dict[str, Any]]) -> dict[str, float]:
    premiums: list[float] = []
    for row in raw_rows:
        if row.get("v16") is None or row.get("v9") is None:
            continue
        floor = float(row["v9"])
        if floor <= 0:
            continue
        premiums.append((float(row["v16"]) / floor - 1) * 100)
    if not premiums:
        return {"median": 6.0, "p25": 3.5, "p75": 12.0, "count": 0}
    premiums.sort()
    n = len(premiums)
    return {
        "median": premiums[n // 2],
        "p25": premiums[max(0, n // 4)],
        "p75": premiums[min(n - 1, (3 * n) // 4)],
        "count": n,
    }


def classify_status(row: dict[str, Any], today: date) -> str:
    if row.get("v19"):
        return "cancelled"
    bid_start = ts_to_date(row.get("v20On"))
    bid_end = ts_to_date(row.get("v21On"))
    open_day = ts_to_date(row.get("v1On"))
    if bid_start and bid_end and bid_start <= today <= bid_end:
        return "bidding"
    if bid_start and today < bid_start:
        return "upcoming"
    if open_day and today <= open_day:
        return "awaiting_result"
    if row.get("v16"):
        return "completed"
    return "past"


def analyze_bid_range(
    floor: float,
    parity: float | None,
    premium_stats: dict[str, float],
    putback: float | None = None,
) -> tuple[float, float, float, str, list[str]]:
    notes: list[str] = []
    p25 = premium_stats["p25"]
    median = premium_stats["median"]
    p75 = premium_stats["p75"]

    if parity is not None and parity >= 100:
        fair = min(parity, floor * (1 + median / 100))
        low = max(floor, min(parity * 0.98, fair * 0.97))
        high = min(parity * 1.02, fair * 1.03)
        advice = "轉換價值高於面額，可偏重轉股價值，但仍留意股價波動與競標熱度。"
        notes.append(f"轉換價值 {parity:.1f}%，屬價內標的。")
    else:
        fair = floor * (1 + median / 100)
        low = max(floor, floor * (1 + p25 / 100))
        high = floor * (1 + p75 / 100)
        advice = "目前偏債性，建議以底標加歷史得標溢價為主，不建議追高過度偏離債底。"
        if parity is not None:
            notes.append(f"轉換價值 {parity:.1f}%，屬價外，主要受底標與信用/賣回條件支撐。")

    if putback and putback > floor:
        notes.append(f"賣回價 {putback:.2f} 元，可視為軟性下限參考。")
        low = max(low, min(putback, floor * 1.01))

    low = round(low, 2)
    high = round(max(high, low), 2)
    fair = round(fair, 2)
    return low, high, fair, advice, notes


def normalize_auction(
    row: dict[str, Any],
    ipo_map: dict[str, CbIpoMeta],
    premium_stats: dict[str, float],
    today: date,
    *,
    enrich: bool = True,
    stock_cache: dict[str, float | None] | None = None,
    bond_cache: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> AuctionRow:
    bond_code = str(row.get("v3", ""))
    stock_code = infer_stock_code(bond_code)
    meta = ipo_map.get(bond_code)
    if meta and not stock_code:
        stock_code = meta.stock_code

    floor = float(row.get("v9") or 0)
    auction = AuctionRow(
        bond_code=bond_code,
        name=str(row.get("v2", "")),
        bond_type=str(row.get("v4", "")),
        auction_method=str(row.get("v5", "")),
        market_label=str(row.get("v4", "")),
        bid_period=str(row.get("v6", "")),
        open_date=fmt_date(row.get("v1On")),
        listing_date=fmt_date(row.get("v11On")),
        broker=str(row.get("v12", "")),
        auction_lots=int(row["v8"] // 1000) if row.get("v8") else None,
        floor_price=floor,
        min_lot=int(row.get("v10") or 1),
        min_win_price=float(row["v16"]) if row.get("v16") is not None else None,
        max_win_price=float(row["v17"]) if row.get("v17") is not None else None,
        underwriting_price=float(row["v18"]) if row.get("v18") is not None else None,
        cancelled=str(row.get("v19") or ""),
        status=classify_status(row, today),
        stock_code=stock_code,
        meta=meta,
    )

    if enrich:
        conversion = meta.conversion_price if meta and meta.conversion_price else None
        putback = None
        if stock_code:
            cache_key = (stock_code, bond_code)
            bond_terms = {}
            if bond_cache is not None and cache_key in bond_cache:
                bond_terms = bond_cache[cache_key]
            else:
                bond_terms = fetch_bond_terms(stock_code, bond_code)
                if bond_cache is not None:
                    bond_cache[cache_key] = bond_terms
            if bond_terms.get("v28"):
                conversion = float(bond_terms["v28"])
            if bond_terms.get("v31"):
                putback = float(bond_terms["v31"])

            auction.stock_price = fetch_stock_close(stock_code, today, stock_cache)
        if auction.stock_price and conversion:
            auction.parity = round(auction.stock_price / conversion * 100, 2)

        low, high, fair, advice, notes = analyze_bid_range(floor, auction.parity, premium_stats, putback)
        auction.bid_low = low
        auction.bid_high = high
        auction.fair_value = fair
        auction.advice = advice
        auction.notes = notes
        if meta and meta.premium_pct is not None:
            auction.notes.append(f"公告轉換溢價率 {meta.premium_pct:.2f}%")
        if conversion:
            auction.notes.insert(0, f"轉換價 {conversion:.2f} 元")
        if auction.stock_price:
            auction.notes.insert(0, f"正股 {stock_code} 最近收盤 {auction.stock_price:.2f} 元")
    return auction


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"auctions": {}, "last_run": None}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def auction_snapshot_key(a: AuctionRow) -> str:
    return a.bond_code


def detect_alerts(current: list[AuctionRow], previous: dict[str, Any]) -> tuple[list[AuctionRow], list[str]]:
    alerts: list[AuctionRow] = []
    reasons: list[str] = []
    prev_map = previous.get("auctions", {})
    for item in current:
        if "轉換公司債" not in item.bond_type and "轉換" not in item.name:
            continue
        if item.status in ("cancelled", "completed", "past"):
            continue
        key = auction_snapshot_key(item)
        prev = prev_map.get(key)
        if prev is None:
            alerts.append(item)
            reasons.append(f"新標的：{item.name} ({item.bond_code})")
            continue
        if prev.get("status") != item.status and item.status in ("bidding", "upcoming", "awaiting_result"):
            alerts.append(item)
            reasons.append(f"狀態變更：{item.name} {prev.get('status')} -> {item.status}")
        elif item.status == "bidding" and (
            prev.get("bid_low") != item.bid_low or prev.get("stock_price") != item.stock_price
        ):
            alerts.append(item)
            reasons.append(f"投標中更新：{item.name} 股價/估值已更新")
    return alerts, reasons


def render_text_report(alerts: list[AuctionRow], all_active: list[AuctionRow], reasons: list[str]) -> str:
    lines = [
        "可轉債競拍監控報告",
        f"產生時間：{datetime.now(TPE):%Y-%m-%d %H:%M} (Asia/Taipei)",
        "",
    ]
    if reasons:
        lines.append("【本次通知原因】")
        lines.extend(f"- {r}" for r in reasons)
        lines.append("")
    if alerts:
        lines.append("【需關注標的】")
        for a in alerts:
            lines.extend(format_auction_lines(a))
            lines.append("")
    lines.append("【所有進行中/即將開始競拍】")
    if not all_active:
        lines.append("- 無")
    else:
        for a in all_active:
            lines.extend(format_auction_lines(a))
            lines.append("")
    lines.append("— 本報告僅供研究參考，不構成投資建議 —")
    return "\n".join(lines)


def format_auction_lines(a: AuctionRow) -> list[str]:
    return [
        f"{a.name} ({a.bond_code}) / 正股 {a.stock_code or '-'} / 狀態 {a.status}",
        f"  投標期間：{a.bid_period} | 開標：{a.open_date} | 底標：{a.floor_price:.2f}",
        f"  建議投標區間：{a.bid_low:.2f} ~ {a.bid_high:.2f} 元 | 合理價參考：{a.fair_value:.2f} 元",
        f"  建議：{a.advice}",
        *(f"  - {n}" for n in a.notes),
    ]


def render_html_report(text: str, alerts: list[AuctionRow]) -> str:
    parts = [
        "<html><body style='font-family:sans-serif;line-height:1.5;color:#111;'>",
        "<h2>可轉債競拍監控報告</h2>",
        f"<p>產生時間：{escape(datetime.now(TPE).strftime('%Y-%m-%d %H:%M'))} (Asia/Taipei)</p>",
    ]
    if alerts:
        parts.append("<h3>需關注標的</h3>")
        for a in alerts:
            parts.append("<div style='border:1px solid #ddd;border-radius:8px;padding:12px;margin:12px 0;'>")
            parts.append(f"<h4 style='margin:0 0 8px;'>{escape(a.name)} ({escape(a.bond_code)})</h4>")
            parts.append("<ul style='margin:0;padding-left:18px;'>")
            for line in format_auction_lines(a)[1:]:
                parts.append(f"<li>{escape(line.strip())}</li>")
            parts.append("</ul></div>")
    parts.append("<pre style='background:#f7f7f7;padding:12px;border-radius:8px;white-space:pre-wrap;'>")
    parts.append(escape(text))
    parts.append("</pre><p style='color:#666;font-size:12px;'>本報告僅供研究參考，不構成投資建議。</p></body></html>")
    return "".join(parts)


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


def serialize_auction(a: AuctionRow) -> dict[str, Any]:
    data = asdict(a)
    if a.meta:
        data["meta"] = asdict(a.meta)
    return data


def run(args: argparse.Namespace) -> int:
    today = datetime.now(TPE).date()
    raw = fetch_auctions()
    ipo_map = fetch_cb_ipo_table()
    premium_stats = historical_premium_stats(raw)

    stock_cache: dict[str, float | None] = {}
    bond_cache: dict[tuple[str, str], dict[str, Any]] = {}
    today_rows: list[AuctionRow] = []
    for row in raw:
        basic = normalize_auction(row, ipo_map, premium_stats, today, enrich=False)
        if "轉換" not in basic.bond_type and "轉換" not in basic.name:
            continue
        enrich = basic.status in ("bidding", "upcoming", "awaiting_result")
        today_rows.append(
            normalize_auction(
                row, ipo_map, premium_stats, today,
                enrich=enrich, stock_cache=stock_cache, bond_cache=bond_cache,
            )
        )
    auctions = today_rows
    active = [a for a in auctions if a.status in ("bidding", "upcoming", "awaiting_result")]

    state = load_state()
    alerts, reasons = detect_alerts(auctions, state)
    report = render_text_report(alerts if alerts else active[:3], active, reasons)
    print(report)

    should_notify = args.force_notify or (args.notify and bool(alerts))
    if should_notify and not args.dry_run:
        subject = "[可轉債競拍]"
        if alerts:
            subject += f" {alerts[0].name} 等 {len(alerts)} 檔需關注"
        else:
            subject += " 監控摘要"
        send_email(subject, report, render_html_report(report, alerts or active[:3]))
        print(f"\n已寄送 Email 至 {os.environ.get('UANALYZE_EMAIL')}")

    new_state = {
        "last_run": datetime.now(TPE).isoformat(),
        "auctions": {auction_snapshot_key(a): serialize_auction(a) for a in auctions},
    }
    if not args.dry_run:
        save_state(new_state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor Taiwan CB auctions")
    parser.add_argument("--dry-run", action="store_true", help="不寫入 state、不寄信")
    parser.add_argument("--notify", action="store_true", help="有異動才寄信（預設僅在偵測到異動時）")
    parser.add_argument("--force-notify", action="store_true", help="無論是否有異動都寄信")
    args = parser.parse_args()
    if args.force_notify:
        args.notify = True
    try:
        return run(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
