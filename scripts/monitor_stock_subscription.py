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
from email.mime.image import MIMEImage
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
CARD_DIR = Path(os.getenv("STOCK_SUB_CARD_DIR", ROOT / ".data" / "sub_cards"))
SOURCE_URL = "https://histock.tw/stock/public.aspx"
USER_AGENT = "Mozilla/5.0 (compatible; stock-subscription-monitor/1.0)"

ACTIVE_STATUSES = {"open", "closing_today", "upcoming"}
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]


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
    lines.append(f"  建議：{advice_for(item)}")
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


def advice_for(item: Subscription) -> str:
    if item.return_pct is None:
        return "留意申購期間與中籤率，自行評估資金成本。"
    if item.return_pct >= 20:
        return "報酬率偏高，可優先評估申購（仍需考量中籤率與資金凍結）。"
    if item.return_pct >= 5:
        return "有正報酬空間，可依資金與中籤率決定是否參與。"
    if item.return_pct > 0:
        return "報酬偏低，留意手續費與資金成本。"
    return "目前市價低於承銷價，申購優勢較弱。"


def _load_font(size: int):
    from PIL import ImageFont

    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_share_card(item: Subscription, out_path: Path) -> Path:
    """產生可分享的公開申購圖卡（手機轉傳友善）。"""
    from PIL import Image, ImageDraw

    width, height = 1080, 980
    bg = (16, 32, 28)
    panel = (28, 52, 46)
    line = (48, 86, 72)
    text = (236, 244, 240)
    muted = (156, 188, 172)
    accent = (255, 196, 72)
    pos_c = (72, 187, 156)
    neg_c = (248, 113, 113)

    status_zh = {
        "upcoming": "即將開始",
        "open": "申購中",
        "closing_today": "今日截止",
        "closed": "已截止",
        "unknown": "未知",
    }.get(item.status, item.status)
    ret_color = pos_c if (item.return_pct or 0) >= 0 else neg_c

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    font_title = _load_font(54)
    font_h2 = _load_font(40)
    font_body = _load_font(30)
    font_small = _load_font(24)
    font_tiny = _load_font(20)

    def text_w(s: str, font) -> int:
        box = draw.textbbox((0, 0), s, font=font)
        return box[2] - box[0]

    y = 36
    draw.text((48, y), "股票公開申購", fill=accent, font=font_small)
    y += 42
    draw.text((48, y), f"{item.name}  {item.stock_code}", fill=text, font=font_title)
    y += 70
    draw.text(
        (48, y),
        f"{item.market}　{status_zh}　申購 {item.apply_period}",
        fill=muted,
        font=font_small,
    )
    y += 52

    offer = f"{item.offer_price:g}" if item.offer_price is not None else "—"
    mkt = f"{item.market_price:g}" if item.market_price is not None else "—"
    ret = f"{item.return_pct:.1f}%" if item.return_pct is not None else "—"
    profit = fmt_money(item.profit_twd)
    metrics = [
        ("承銷價", f"{offer} 元"),
        ("市價", f"{mkt} 元"),
        ("報酬率", ret),
        ("預估獲利", f"{profit} 元"),
    ]
    box_w = (width - 48 * 2 - 18 * 3) // 4
    for i, (label, value) in enumerate(metrics):
        x = 48 + i * (box_w + 18)
        draw.rounded_rectangle((x, y, x + box_w, y + 120), radius=16, fill=panel)
        draw.text((x + 18, y + 18), label, fill=muted, font=font_tiny)
        color = ret_color if label in {"報酬率", "預估獲利"} else text
        f = font_h2 if text_w(value, font_h2) < box_w - 24 else font_body
        draw.text((x + 18, y + 56), value, fill=color, font=f)
    y += 150

    details = [
        ("抽籤日", item.lottery_date),
        ("撥券日", item.allot_date),
        ("承銷張數", fmt_money(item.underwrite_lots)),
        ("申購張數", str(item.apply_lots) if item.apply_lots is not None else "—"),
        ("中籤率", f"{item.win_rate_pct:g}%" if item.win_rate_pct is not None else "—"),
        ("合格件", fmt_money(item.qualified_apps)),
    ]
    draw.rounded_rectangle((48, y, width - 48, y + 220), radius=16, fill=panel)
    draw.text((72, y + 24), "申購資訊", fill=text, font=font_h2)
    row_y = y + 80
    for i, (label, value) in enumerate(details):
        col = i % 3
        row = i // 3
        x = 72 + col * 320
        yy = row_y + row * 70
        draw.text((x, yy), label, fill=muted, font=font_tiny)
        draw.text((x, yy + 28), value, fill=text, font=font_body)

    y += 250
    advice = advice_for(item)
    draw.text((48, y), "分享建議", fill=accent, font=font_small)
    y += 40
    if len(advice) > 28:
        draw.text((48, y), advice[:28], fill=text, font=font_body)
        y += 40
        draw.text((48, y), advice[28:], fill=text, font=font_body)
    else:
        draw.text((48, y), advice, fill=text, font=font_body)

    y = height - 90
    draw.line((48, y, width - 48, y), fill=line, width=1)
    y += 18
    draw.text((48, y), "資料來源 HiStock｜僅供研究分享，不構成投資建議", fill=muted, font=font_tiny)
    y += 32
    draw.text((48, y), item.detail_url, fill=muted, font=font_tiny)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG", optimize=True)
    return out_path


def generate_share_cards(items: list[Subscription]) -> list[tuple[Subscription, Path]]:
    results: list[tuple[Subscription, Path]] = []
    stamp = datetime.now(TPE).strftime("%Y%m%d_%H%M")
    for item in items:
        path = CARD_DIR / f"sub_{item.stock_code}_{stamp}.png"
        results.append((item, render_share_card(item, path)))
    return results


def render_html_report(
    text: str,
    focus: list[Subscription],
    card_cids: list[tuple[str, str]] | None = None,
) -> str:
    parts = [
        "<html><body style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        "line-height:1.5;color:#222;'>",
        "<h2>股票公開申購監控</h2>",
        f"<p>產生時間：{escape(datetime.now(TPE).strftime('%Y-%m-%d %H:%M'))}（台北）</p>",
        "<p>下方附上可分享圖卡（也可直接轉傳附件 PNG）。</p>",
    ]
    for cid, caption in card_cids or []:
        parts.append(
            "<div style='margin:0 0 16px;'>"
            f"<p style='margin:0 0 8px;font-weight:600;'>{escape(caption)}</p>"
            f"<img src='cid:{escape(cid)}' alt='{escape(caption)}' "
            "style='max-width:100%;border-radius:12px;'/>"
            "</div>"
        )
    for item in focus:
        ret_color = "#c62828" if (item.return_pct or 0) >= 0 else "#2e7d32"
        parts.append(
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
    parts.append(
        "<pre style='background:#f7f7f7;padding:12px;border-radius:8px;"
        "white-space:pre-wrap;'>"
        + escape(text)
        + "</pre>"
        "<p style='color:#666;font-size:12px;'>本報告僅供研究參考，不構成投資建議。"
        f"資料來源：<a href='{SOURCE_URL}'>HiStock 公開申購</a></p>"
        "</body></html>"
    )
    return "".join(parts)


def send_email(
    subject: str,
    text: str,
    html: str,
    card_paths: list[Path] | None = None,
) -> None:
    from email_util import resolve_recipients, sender_credentials

    email, password = sender_credentials()
    recipients = resolve_recipients(email)

    def build_msg(to_addr: str) -> MIMEMultipart:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = email
        msg["To"] = to_addr

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(text, "plain", "utf-8"))

        related = MIMEMultipart("related")
        related.attach(MIMEText(html, "html", "utf-8"))
        for path in card_paths or []:
            with path.open("rb") as f:
                img = MIMEImage(f.read(), _subtype="png")
            img.add_header("Content-ID", f"<{path.stem}>")
            img.add_header("Content-Disposition", "inline", filename=path.name)
            related.attach(img)
        alt.attach(related)
        msg.attach(alt)

        for path in card_paths or []:
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
    print(f"已分別寄送至 {len(recipients)} 位收件人")

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

    card_pairs = generate_share_cards(focus)
    card_paths = [p for _, p in card_pairs]
    if card_paths:
        print("\n已產生分享圖卡：")
        for p in card_paths:
            print(f"  - {p}")

    should_notify = args.force_notify or (args.notify and bool(alerts))
    if should_notify and not args.dry_run:
        subject = "[股票申購]"
        if alerts:
            subject += f" {alerts[0].name} 等 {len(alerts)} 檔需關注"
        else:
            subject += " 監控摘要"
        card_cids = [(p.stem, f"{a.name} ({a.stock_code})") for a, p in card_pairs]
        html = render_html_report(report, focus, card_cids=card_cids)
        send_email(subject, report, html, card_paths=card_paths)
        print(f"\n已寄送 Email（含圖卡）至收件人清單")

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
