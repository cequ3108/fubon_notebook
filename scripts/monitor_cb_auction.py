#!/usr/bin/env python3
"""Monitor Taiwan TWSE auctions (CB + stock); suggest bid ladders.

Sources:
- Auction list: TWSE 競價拍賣公告
  https://www.twse.com.tw/zh/announcement/auction.html
- CB terms: cyclesinvest cbipo
- Listed stock close: TWSE STOCK_DAY
- Emerging (興櫃) price: Yahoo Finance *.TWO
- Shares outstanding: TWSE / TPEx open data

Email: UANALYZE_EMAIL + GMAIL_APP_PASSWORD
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from typing import Any

TPE = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = Path(os.getenv("CB_AUCTION_STATE_PATH", ROOT / ".data" / "cb_auction_state.json"))
CARD_DIR = Path(os.getenv("CB_AUCTION_CARD_DIR", ROOT / ".data" / "cb_cards"))

TWSE_AUCTION_URL = "https://www.twse.com.tw/rwd/zh/announcement/auction"
BOND_URL = "https://www.money-link.com.tw/p/?G=m&pg=bnd001_tw&id={stock}"
CB_IPO_URL = "https://www.cyclesinvest.com/cbipo.php"
TWSE_COMPANY_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_COMPANY_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
USER_AGENT = "Mozilla/5.0 (compatible; twse-auction-monitor/2.0)"

FACE_VALUE = 100_000  # 可轉債每張面額 10 萬；股票每張 1000 股
STOCK_LOT_SHARES = 1000
# 市值越大、標單筆數越多（提高命中率）；小型至少 5 筆
TICKETS_BY_TIER = {
    "micro": 5,
    "small": 5,
    "mid": 7,
    "large": 8,
    "mega": 10,
    "unknown": 5,
}
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]


@dataclass
class BidTicket:
    price: float
    lots: int
    amount_twd: int
    role: str  # cheap / core / insure


@dataclass
class PositionPlan:
    size_tier: str
    market_cap_yi: float | None
    target_budget_twd: int
    deposit_est_twd: int
    tickets: list[BidTicket] = field(default_factory=list)
    rationale: str = ""
    purpose_category: str = "unknown"
    purpose_score: int = 50
    purpose_label: str = "用途未明"
    purpose_text: str = ""



@dataclass
class PurposeInfo:
    raw_text: str = ""
    category: str = "unknown"  # growth / working_capital / refinance / mixed / unknown
    score: int = 50  # 0-100，越高越偏成長用途
    label: str = "用途未明"
    note: str = ""


def _mops_list_cb_resolutions(stock_code: str, year: str) -> list[dict[str, str]]:
    payload = urllib.parse.urlencode({
        "encodeURIComponent": "1",
        "step": "1",
        "firstin": "1",
        "off": "1",
        "TYPEK": "sii",
        "co_id": stock_code,
        "year": year,
        "month": "",
        "b_date": "",
        "e_date": "",
    }).encode()
    req = urllib.request.Request(
        "https://mopsov.twse.com.tw/mops/web/ajax_t05st01",
        data=payload,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError):
        return []
    out: list[dict[str, str]] = []
    for row in re.findall(r"<tr[^>]*>.*?</tr>", html, re.S):
        if "轉換公司債" not in row and "可轉換公司債" not in row:
            continue
        if not any(k in row for k in ("董事會", "決議", "發行")):
            continue
        # 排除贖回／注意交易等雜訊
        if any(k in row for k in ("贖回", "注意交易", "代收價款", "終止櫃檯")):
            continue
        m = re.search(
            r"seq_no\.value='(\d+)'.*?spoke_time\.value='(\d+)'.*?spoke_date\.value='(\d+)'",
            row,
            re.S,
        )
        if not m:
            continue
        title = re.sub(r"<[^>]+>", " ", row)
        title = re.sub(r"\s+", " ", title).strip()
        out.append({
            "seq_no": m.group(1),
            "spoke_time": m.group(2),
            "spoke_date": m.group(3),
            "title": title,
        })
    return out


def _mops_resolution_detail(stock_code: str, year: str, item: dict[str, str]) -> str:
    payload = urllib.parse.urlencode({
        "encodeURIComponent": "1",
        "step": "2",
        "firstin": "1",
        "off": "1",
        "TYPEK": "sii",
        "co_id": stock_code,
        "year": year,
        "seq_no": item["seq_no"],
        "spoke_time": item["spoke_time"],
        "spoke_date": item["spoke_date"],
    }).encode()
    req = urllib.request.Request(
        "https://mopsov.twse.com.tw/mops/web/ajax_t05st01",
        data=payload,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError):
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text)


def extract_purpose_text(detail: str) -> str:
    m = re.search(r"募得價款之用途及運用計畫[:：]\s*(.+?)(?=\s*\d{1,2}\.|$)", detail)
    if not m:
        return ""
    purpose = m.group(1).strip(" 。;；")
    # 截斷承銷方式等後續欄位殘留
    purpose = re.split(r"\s+\d{1,2}\.", purpose)[0].strip()
    return purpose[:120]


def classify_purpose(purpose: str) -> PurposeInfo:
    if not purpose:
        return PurposeInfo(note="公開資訊未找到資金用途，暫不調整")

    growth_kw = ("研發", "購置", "機器", "設備", "擴充", "擴產", "廠房", "興建", "產能", "轉投資", "併購", "建廠", "資本支出")
    refinance_kw = ("償還銀行", "償還借款", "償還公司債", "償還債務", "還款", "借新還舊")
    wc_kw = ("充實營運", "營運資金", "營運週轉")

    has_g = any(k in purpose for k in growth_kw)
    has_r = any(k in purpose for k in refinance_kw)
    has_w = any(k in purpose for k in wc_kw)

    if has_g and not has_r:
        return PurposeInfo(purpose, "growth", 82, "偏成長用途", f"用途「{purpose}」偏擴產／設備／研發，較具成長想像，可相對積極")
    if has_g and has_r:
        return PurposeInfo(purpose, "mixed", 58, "成長＋還債混合", f"用途「{purpose}」含成長與還債，中性偏多，勿過度追價")
    if has_r and has_w:
        return PurposeInfo(purpose, "refinance", 32, "還債＋營運資金", f"用途「{purpose}」偏財務調度／借新還舊，建議縮小部位、標價勿衝太高")
    if has_r:
        return PurposeInfo(purpose, "refinance", 25, "偏還舊債", f"用途「{purpose}」以償債為主，成長性較弱，宜保守出價")
    if has_w:
        return PurposeInfo(purpose, "working_capital", 48, "充實營運資金", f"用途「{purpose}」偏營運週轉，中性，標價維持合理區間即可")
    return PurposeInfo(purpose, "unknown", 50, "用途未分類", f"用途「{purpose}」無法明確歸類，暫維持中性")


def fetch_cb_purpose(stock_code: str, bond_name: str = "", cache: dict[str, PurposeInfo] | None = None) -> PurposeInfo:
    if cache is not None and stock_code in cache:
        return cache[stock_code]
    info = PurposeInfo(note="查無董事會發行決議")
    # 民國年：以目前年份推估，並往前一年備援
    roc_now = datetime.now(TPE).year - 1911
    for year in (str(roc_now), str(roc_now - 1)):
        items = _mops_list_cb_resolutions(stock_code, year)
        # 優先標題含「決議…發行」且盡量對應期別
        ranked = []
        for it in items:
            score = 0
            title = it["title"]
            if "董事會" in title and "發行" in title:
                score += 5
            if bond_name and bond_name[:2] in title:
                score += 3
            # 期別提示：第三次 / 第二 etc
            m = re.search(r"第([一二三四五六七八九十]+)次", bond_name or "")
            if m and m.group(0) in title:
                score += 10
            ranked.append((score, it))
        ranked.sort(key=lambda x: -x[0])
        for score, it in ranked:
            if score < 5:
                continue
            detail = _mops_resolution_detail(stock_code, year, it)
            purpose = extract_purpose_text(detail)
            if purpose:
                info = classify_purpose(purpose)
                break
        if info.raw_text:
            break
    if cache is not None:
        cache[stock_code] = info
    return info


def apply_purpose_to_bids(
    bid_low: float,
    bid_high: float,
    fair: float,
    floor: float,
    purpose: PurposeInfo,
) -> tuple[float, float, list[str]]:
    """依資金用途壓縮或放寬標價上緣。"""
    notes: list[str] = []
    low, high = bid_low, bid_high
    cat = purpose.category
    if cat == "growth":
        # 成長案可稍微靠近上緣，但不盲目加價
        high = min(high * 1.01, high + 1.0)
        notes.append("成長用途：可相對積極，但仍以合理價附近為核心倉")
    elif cat == "refinance":
        # 還債案：上緣壓回合理價附近，避免追高
        high = min(high, max(fair * 1.02, (fair + floor) / 2 + (fair - floor) * 0.35))
        high = max(high, low)
        notes.append("還債／調度用途：建議標價勿衝太高，保險倉靠近合理價即可")
    elif cat == "working_capital":
        high = min(high, fair + (bid_high - fair) * 0.7)
        high = max(high, low)
        notes.append("營運資金用途：中性，維持合理區間、少追高")
    elif cat == "mixed":
        high = min(high, fair + (bid_high - fair) * 0.85)
        high = max(high, low)
        notes.append("混合用途：可參與但控制最高標")
    return round(low, 2), round(high, 2), notes


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
    bond_code: str  # 證券代號（可轉債或股票）
    name: str
    bond_type: str  # 發行性質
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
    conversion_price: float | None = None
    bid_low: float | None = None
    bid_high: float | None = None
    fair_value: float | None = None
    est_market_avg: float | None = None  # 預估全場得標均價
    est_clear_price: float | None = None  # 預估最低得標／清算價
    advice: str = ""
    notes: list[str] = field(default_factory=list)
    position: PositionPlan | None = None
    asset_kind: str = "cb"  # cb / stock
    deposit_ratio: float = 0.5
    max_lot: int | None = None
    otc_price: float | None = None
    discount_pct: float | None = None
    quality_score: int = 50
    sentiment_score: int = 50
    fee_per_ticket: int = 400


def http_json(url: str, timeout: int = 30) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def http_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")


def parse_num(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").replace("%", "").strip()
    if not text or text in {"-", "—", "N/A"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int_num(value: Any) -> int | None:
    num = parse_num(value)
    return int(num) if num is not None else None


def parse_twse_date(value: str) -> date | None:
    text = str(value or "").strip().replace("-", "/")
    m = re.fullmatch(r"(\d{4})/(\d{1,2})/(\d{1,2})", text)
    if not m:
        return None
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def fmt_twse_date(value: str) -> str:
    d = parse_twse_date(value)
    return d.isoformat() if d else (str(value or "-") or "-")


def is_cb_issue(issue_type: str, name: str = "") -> bool:
    text = f"{issue_type}{name}"
    return "轉換公司債" in text or "可轉換公司債" in text or (
        "轉換" in text and "公司債" in text
    )


def infer_stock_code(bond_code: str) -> str:
    digits = re.sub(r"\D", "", bond_code)
    return digits[:4] if len(digits) >= 4 else digits


def fetch_auctions() -> list[dict[str, Any]]:
    """Fetch TWSE auction announcement rows as normalized dicts."""
    data = http_json(TWSE_AUCTION_URL)
    if data.get("stat") != "OK":
        raise RuntimeError(f"TWSE auction API 失敗：{data.get('stat')}")
    rows: list[dict[str, Any]] = []
    for raw in data.get("data") or []:
        if not isinstance(raw, list) or len(raw) < 17:
            continue
        issue_type = str(raw[5] or "")
        code = str(raw[3] or "").strip()
        name = str(raw[2] or "").strip()
        rows.append(
            {
                "code": code,
                "name": name,
                "market": str(raw[4] or ""),
                "issue_type": issue_type,
                "method": str(raw[6] or ""),
                "open_date": str(raw[1] or ""),
                "bid_start": str(raw[7] or ""),
                "bid_end": str(raw[8] or ""),
                "auction_lots": parse_int_num(raw[9]),
                "floor": parse_num(raw[10]) or 0.0,
                "min_lot": parse_int_num(raw[11]) or 1,
                "max_lot": parse_int_num(raw[12]),
                "deposit_pct": parse_num(raw[13]) or 50.0,
                "fee": parse_int_num(raw[14]) or 400,
                "listing_date": str(raw[15] or ""),
                "broker": str(raw[16] or ""),
                "win_amount": parse_num(raw[17]),
                "fee_rate": parse_num(raw[18]),
                "qualified_apps": parse_int_num(raw[19]),
                "qualified_lots": parse_int_num(raw[20]),
                "min_win": parse_num(raw[21]),
                "max_win": parse_num(raw[22]),
                "avg_win": parse_num(raw[23]),
                "underwrite": parse_num(raw[24]),
                "cancelled": str(raw[25] or "").strip(),
                "asset_kind": "cb" if is_cb_issue(issue_type, name) else "stock",
            }
        )
    return rows


def fetch_cb_ipo_table() -> dict[str, CbIpoMeta]:
    html = http_text(CB_IPO_URL)
    rows: dict[str, CbIpoMeta] = {}
    for row_html in re.findall(r"<tr[^>]*>.*?</tr>", html, re.S):
        texts = [t.strip() for t in re.findall(r">([^<]+)<", row_html) if t.strip()]
        if len(texts) < 16:
            continue
        if not re.fullmatch(r"\d{4,5}", texts[2]) or not re.fullmatch(r"\d{4,5}", texts[3]):
            continue
        stock, bond, name = texts[2], texts[3], texts[4]
        tcri_parts = texts[5].split("/")
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


def fetch_emerging_price(
    stock_code: str,
    cache: dict[str, float | None] | None = None,
) -> float | None:
    """興櫃／上櫃參考價（Yahoo *.TWO）。"""
    if cache is not None and stock_code in cache:
        return cache[stock_code]
    price = None
    for suffix in (".TWO", ".TW"):
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{stock_code}{suffix}?interval=1d&range=10d"
        )
        try:
            data = http_json(url)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            continue
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            continue
        meta = result[0].get("meta") or {}
        raw_price = meta.get("regularMarketPrice") or meta.get("chartPreviousClose")
        if raw_price:
            price = float(raw_price)
            break
        closes = ((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        for c in reversed(closes):
            if c:
                price = float(c)
                break
        if price is not None:
            break
    if cache is not None:
        cache[stock_code] = price
    return price


def fetch_stock_close(
    stock_code: str,
    as_of: date | None = None,
    cache: dict[str, float | None] | None = None,
) -> float | None:
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
            data = http_json(url)
        except (urllib.error.URLError, json.JSONDecodeError):
            continue
        if data.get("stat") != "OK" or not data.get("data"):
            continue
        fields = data.get("fields") or []
        close_idx = fields.index("收盤價") if "收盤價" in fields else 6
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
    if price is None:
        price = fetch_emerging_price(stock_code, cache=None)
    if cache is not None:
        cache[stock_code] = price
    return price


def load_shares_outstanding() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for row in http_json(TWSE_COMPANY_URL):
            code = str(row.get("公司代號") or "").strip()
            shares = str(row.get("已發行普通股數或TDR原股發行股數") or "0").replace(",", "")
            if code and shares.isdigit():
                out[code] = int(shares)
    except (urllib.error.URLError, json.JSONDecodeError, TypeError):
        pass
    try:
        for row in http_json(TPEX_COMPANY_URL):
            code = str(row.get("SecuritiesCompanyCode") or "").strip()
            shares = str(row.get("IssueShares") or "0").replace(",", "")
            if code and shares.isdigit():
                out[code] = int(shares)
    except (urllib.error.URLError, json.JSONDecodeError, TypeError):
        pass
    return out


def market_cap_yi(stock_code: str, price: float | None, shares_map: dict[str, int]) -> float | None:
    if not price or stock_code not in shares_map:
        return None
    return round(shares_map[stock_code] * price / 1e8, 2)


def parse_tcri(meta: CbIpoMeta | None) -> int | None:
    if not meta or not meta.tcri:
        return None
    m = re.search(r"(\d+)", meta.tcri)
    return int(m.group(1)) if m else None


def classify_size_tier(cap_yi: float | None) -> str:
    if cap_yi is None:
        return "unknown"
    if cap_yi >= 2000:
        return "mega"
    if cap_yi >= 500:
        return "large"
    if cap_yi >= 100:
        return "mid"
    if cap_yi >= 30:
        return "small"
    return "micro"


def base_budget_for_tier(tier: str) -> int:
    # 依市值分級的基準配置（新台幣）
    return {
        "mega": 10_000_000,  # 1000 萬
        "large": 5_000_000,  # 500 萬
        "mid": 2_500_000,    # 250 萬
        "small": 1_500_000,  # 150 萬
        "micro": 800_000,    # 80 萬
        "unknown": 1_500_000,
    }[tier]


def adjust_budget(
    base: int,
    *,
    tcri: int | None,
    collateral: str,
    parity: float | None,
    issue_amount_100m: float | None,
    auction_lots: int | None,
    purpose: PurposeInfo | None = None,
) -> tuple[int, list[str]]:
    reasons: list[str] = []
    budget = float(base)

    if purpose is not None:
        if purpose.category == "growth":
            budget *= 1.15
            reasons.append(f"資金用途偏成長（{purpose.label}），部位略增")
        elif purpose.category == "refinance":
            budget *= 0.75
            reasons.append(f"資金用途偏還債／調度（{purpose.label}），部位下修")
        elif purpose.category == "working_capital":
            budget *= 0.95
            reasons.append(f"資金用途偏營運資金（{purpose.label}），部位略保守")
        elif purpose.category == "mixed":
            budget *= 0.95
            reasons.append(f"資金用途混合（{purpose.label}），部位略保守")
        elif purpose.note:
            reasons.append(purpose.note)

    if tcri is not None:
        if tcri <= 4:
            budget *= 1.15
            reasons.append(f"TCRI {tcri} 較佳，部位略增")
        elif tcri >= 7:
            budget *= 0.65
            reasons.append(f"TCRI {tcri} 偏弱，部位下修控風險")
        elif tcri == 6:
            budget *= 0.85
            reasons.append(f"TCRI {tcri} 中性偏弱，部位略減")

    if collateral and "無" not in collateral:
        budget *= 1.10
        reasons.append(f"有擔保（{collateral}），部位略增")
    elif collateral and "無" in collateral:
        reasons.append("無擔保，維持信用風險折扣")

    if parity is not None:
        if parity >= 100:
            budget *= 1.10
            reasons.append(f"價內（parity {parity:.1f}%），轉股價值支撐較強")
        elif parity < 85:
            budget *= 0.80
            reasons.append(f"深價外（parity {parity:.1f}%），以債性為主、縮小部位")

    if issue_amount_100m is not None:
        issue_twd = issue_amount_100m * 100_000_000
        cap_by_issue = issue_twd * 0.03
        if budget > cap_by_issue:
            budget = cap_by_issue
            reasons.append(f"受發行量限制，上限約 {cap_by_issue / 1e4:.0f} 萬")

    if auction_lots:
        # 單一帳戶粗估不超過競拍量 20%；單筆標單法規上限 10%
        max_total_lots = max(auction_lots // 5, 1)
        max_by_lots = max_total_lots * FACE_VALUE
        if budget > max_by_lots:
            budget = max_by_lots
            reasons.append(f"受競拍張數限制，上限約 {max_by_lots / 1e4:.0f} 萬")

    budget_i = int(max(round(budget / FACE_VALUE) * FACE_VALUE, FACE_VALUE))
    return budget_i, reasons


def tickets_for_tier(tier: str) -> int:
    return TICKETS_BY_TIER.get(tier, 5)


def _ladder_weights(n: int, mode: str) -> list[float]:
    """cheap_heavy：低價多張；fill_first：張數集中在合理價附近以提高命中率。"""
    if n <= 1:
        return [1.0]
    if mode == "cheap_heavy":
        return [float(n - i) for i in range(n)]
    # fill_first：峰値略偏合理價中上段，提高落在清算帶以上的張數占比
    peak = 0.52 * (n - 1)
    weights: list[float] = []
    for i in range(n):
        dist = abs(i - peak)
        weights.append(max(1.0, n * 1.05 - dist * 1.45))
    return weights


def _rebalance_lots_for_beat_avg(
    prices: list[float],
    lots_list: list[int],
    *,
    market_avg_cap: float | None,
    fair: float,
    min_lot: int,
) -> list[int]:
    """把過高價張數往合理價附近挪，目標投標／得標均價低於預估全場均價。"""
    if not market_avg_cap or market_avg_cap <= 0 or not lots_list:
        return lots_list
    lots = list(lots_list)
    n = len(lots)
    below = [i for i, p in enumerate(prices) if p <= market_avg_cap + 1e-9]
    if not below:
        return lots

    def recv_idx() -> int:
        # 優先落到最接近合理價、且不高於全場均價的標單
        return min(below, key=lambda i: (abs(prices[i] - fair), -prices[i]))

    # 高於目標均價的張數合計不超过約 8%（保險倉）
    total = sum(lots)
    if total > 0:
        above_cap = max(int(total * 0.08), min_lot)
        above_idx = [i for i, p in enumerate(prices) if p > market_avg_cap]
        above_lots = sum(lots[i] for i in above_idx)
        while above_lots > above_cap:
            donor = max(above_idx, key=lambda i: lots[i])
            if lots[donor] <= min_lot:
                break
            lots[donor] -= 1
            lots[recv_idx()] += 1
            above_lots -= 1

    def vwap() -> float:
        s = sum(lots)
        return sum(prices[i] * lots[i] for i in range(n)) / max(s, 1)

    # 整包加權均價壓到預估全場均價的 99.5% 以下
    guard = 0
    target = market_avg_cap * 0.995
    while vwap() > target and guard < 10_000:
        guard += 1
        donors = [i for i in range(n) if prices[i] > fair and lots[i] > min_lot]
        if not donors:
            donors = [i for i in range(n) if lots[i] > min_lot and prices[i] > prices[recv_idx()]]
        if not donors:
            break
        donor = max(donors, key=lambda i: prices[i])
        recv = recv_idx()
        if donor == recv:
            break
        lots[donor] -= 1
        lots[recv] += 1
    return lots


def build_ladder_tickets(
    *,
    budget_twd: int,
    floor: float,
    bid_low: float,
    bid_high: float,
    fair: float,
    auction_lots: int | None,
    min_lot: int,
    n_tickets: int = 5,
    lot_value_fn: Any | None = None,
    max_lot: int | None = None,
    weight_mode: str = "fill_first",
    market_avg_cap: float | None = None,
) -> list[BidTicket]:
    """多筆階梯標。

    fill_first（預設／可轉債）：張數集中合理價附近，提高命中率，並讓加權均價低於預估全場均價。
    cheap_heavy（股票等）：低價多張衝便宜成本，高價少張保命中率。

    lot_value_fn(price, lots) -> amount_twd；預設為可轉債（面額 10 萬 × 價/100）。
    """
    if lot_value_fn is None:
        lot_value_fn = lambda price, lots: int(lots * FACE_VALUE * price / 100)
        # CB: 預算約等於張數 × 10 萬（以面額計）
        total_lots = max(budget_twd // FACE_VALUE, 1)
    else:
        # 股票：用合理價估算單張金額
        unit = max(int(fair * STOCK_LOT_SHARES), 1)
        total_lots = max(budget_twd // unit, 1)

    max_per_ticket = max(int(auction_lots * 0.10), min_lot) if auction_lots else total_lots
    if max_lot:
        max_per_ticket = min(max_per_ticket, max_lot)
    max_per_ticket = max(max_per_ticket, min_lot)

    low = max(floor, bid_low)
    high = max(bid_high, low)
    fair = min(max(fair, low), high)
    n_tickets = max(min(n_tickets, total_lots // max(min_lot, 1)), 1)

    prices: list[float] = []
    for i in range(n_tickets):
        t = 0.0 if n_tickets == 1 else i / (n_tickets - 1)
        if t <= 0.5:
            p = low + (fair - low) * (t / 0.5 if n_tickets > 1 else 0.0)
        else:
            p = fair + (high - fair) * ((t - 0.5) / 0.5)
        prices.append(round(p, 2))

    raw_weights = _ladder_weights(n_tickets, weight_mode)
    weight_sum = sum(raw_weights)
    lots_list = [max(int(total_lots * w / weight_sum), 0) for w in raw_weights]
    leftover = total_lots - sum(lots_list)
    if leftover > 0:
        # 餘數優先加在權重最高（fill_first 峰値）的標單
        peak_i = max(range(n_tickets), key=lambda j: raw_weights[j])
        lots_list[peak_i] += leftover

    for i in range(n_tickets):
        if lots_list[i] == 0 and total_lots >= min_lot * (i + 1):
            donor = max(range(n_tickets), key=lambda j: lots_list[j])
            if lots_list[donor] > min_lot:
                lots_list[donor] -= min_lot
                lots_list[i] += min_lot

    if weight_mode == "fill_first":
        # 再平衡以上限取「預估全場均價」與「合理價略上方」較低者，
        # 讓多數得標張數落在全場均價之下。
        beat_cap = market_avg_cap
        if market_avg_cap is not None:
            beat_cap = min(market_avg_cap, fair * 1.004)
        lots_list = _rebalance_lots_for_beat_avg(
            prices,
            lots_list,
            market_avg_cap=beat_cap,
            fair=fair,
            min_lot=min_lot,
        )

    tickets: list[BidTicket] = []
    for i, (price, lots) in enumerate(zip(prices, lots_list)):
        if lots <= 0:
            continue
        lots = min(lots, max_per_ticket)
        if lots < min_lot:
            continue
        if i == 0:
            role = "cheap"
        elif i >= n_tickets - 1:
            role = "insure"
        else:
            role = "core"
        tickets.append(
            BidTicket(
                price=price,
                lots=lots,
                amount_twd=int(lot_value_fn(price, lots)),
                role=role,
            )
        )
    return tickets


def build_position_plan(
    auction: AuctionRow,
    shares_map: dict[str, int],
    purpose_cache: dict[str, PurposeInfo] | None = None,
) -> PositionPlan:
    cap = market_cap_yi(auction.stock_code, auction.stock_price, shares_map)
    tier = classify_size_tier(cap)
    base = base_budget_for_tier(tier)
    tcri = parse_tcri(auction.meta)
    collateral = auction.meta.collateral if auction.meta else ""
    issue_amt = auction.meta.issue_amount_100m if auction.meta else None

    purpose = fetch_cb_purpose(auction.stock_code, auction.name, purpose_cache)
    auction.notes.append(f"資金用途：{purpose.raw_text or '未取得'}（{purpose.label}，評分 {purpose.score}）")
    if purpose.note:
        auction.notes.append(purpose.note)

    budget, reasons = adjust_budget(
        base,
        tcri=tcri,
        collateral=collateral,
        parity=auction.parity,
        issue_amount_100m=issue_amt,
        auction_lots=auction.auction_lots,
        purpose=purpose,
    )

    n_tickets = tickets_for_tier(tier)
    tickets: list[BidTicket] = []
    if auction.bid_low is not None and auction.bid_high is not None and auction.fair_value is not None:
        adj_low, adj_high, bid_notes = apply_purpose_to_bids(
            auction.bid_low,
            auction.bid_high,
            auction.fair_value,
            auction.floor_price,
            purpose,
        )
        reasons.extend(bid_notes)
        # 同步回寫建議區間（反映用途後的出價策略）
        auction.bid_low = adj_low
        auction.bid_high = adj_high
        tickets = build_ladder_tickets(
            budget_twd=budget,
            floor=auction.floor_price,
            bid_low=adj_low,
            bid_high=adj_high,
            fair=auction.fair_value,
            auction_lots=auction.auction_lots,
            min_lot=auction.min_lot or 1,
            n_tickets=n_tickets,
            weight_mode="fill_first",
            market_avg_cap=auction.est_market_avg,
        )

    gross = sum(t.amount_twd for t in tickets) or int(budget * (auction.fair_value or 100) / 100)
    deposit = int(gross * 0.5)

    cap_txt = f"{cap:.0f} 億" if cap is not None else "未知"
    tier_label = {
        "mega": "超大型",
        "large": "大型",
        "mid": "中型",
        "small": "小型",
        "micro": "微型",
        "unknown": "未知",
    }[tier]
    rationale = (
        f"市值約 {cap_txt} → {tier_label}股，基準配置 {base / 1e4:.0f} 萬；"
        + ("；".join(reasons) if reasons else "無額外加減碼")
        + f"。資金用途評分 {purpose.score}/100（{purpose.label}）；"
        + f"建議 {len(tickets) or n_tickets} 筆階梯標：張數集中合理價附近以提高命中率，目標得標均價低於全場。"
    )
    return PositionPlan(
        size_tier=tier,
        market_cap_yi=cap,
        target_budget_twd=budget,
        deposit_est_twd=deposit,
        tickets=tickets,
        rationale=rationale,
        purpose_category=purpose.category,
        purpose_score=purpose.score,
        purpose_label=purpose.label,
        purpose_text=purpose.raw_text,
    )


def fetch_bond_terms(stock_code: str, bond_code: str) -> dict[str, Any]:
    try:
        data = http_json(BOND_URL.format(stock=stock_code))
    except urllib.error.URLError:
        return {}
    for row in data.get("result", {}).get("d1") or []:
        if str(row.get("v1")) == bond_code:
            return row
    return {}


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(max(int(len(sorted_vals) * p), 0), len(sorted_vals) - 1)
    return sorted_vals[idx]


def historical_premium_stats(
    raw_rows: list[dict[str, Any]],
    *,
    asset_kind: str | None = "cb",
) -> dict[str, float]:
    """歷史得標溢價（相對底標，優先 avg_win）。可依 asset_kind 篩選。"""
    premiums: list[float] = []
    for row in raw_rows:
        if asset_kind and row.get("asset_kind") != asset_kind:
            continue
        floor = float(row.get("floor") or 0)
        win = row.get("avg_win") or row.get("min_win")
        if not floor or not win or float(win) <= 0:
            continue
        premiums.append((float(win) / floor - 1) * 100)
    if not premiums:
        if asset_kind == "stock":
            return {"median": 35.0, "p25": 18.0, "p75": 70.0, "count": 0}
        return {"median": 6.0, "p25": 3.5, "p75": 12.0, "count": 0}
    premiums.sort()
    return {
        "median": _percentile(premiums, 0.5),
        "p25": _percentile(premiums, 0.25),
        "p75": _percentile(premiums, 0.75),
        "count": float(len(premiums)),
    }


def historical_win_stats(
    raw_rows: list[dict[str, Any]],
    *,
    asset_kind: str | None = "cb",
    min_auction_lots: int | None = None,
) -> dict[str, float]:
    """分別統計最低得標／得標均價相對底標溢價。"""
    avg_prems: list[float] = []
    min_prems: list[float] = []
    for row in raw_rows:
        if asset_kind and row.get("asset_kind") != asset_kind:
            continue
        lots = int(row.get("auction_lots") or 0)
        if min_auction_lots and lots < min_auction_lots:
            continue
        floor = float(row.get("floor") or 0)
        if not floor:
            continue
        avg = row.get("avg_win")
        mn = row.get("min_win")
        if avg and float(avg) > 0:
            avg_prems.append((float(avg) / floor - 1) * 100)
        if mn and float(mn) > 0:
            min_prems.append((float(mn) / floor - 1) * 100)
    if not avg_prems:
        base = historical_premium_stats(raw_rows, asset_kind=asset_kind)
        return {
            "avg_median": base["median"],
            "avg_p25": base["p25"],
            "avg_p75": base["p75"],
            "min_median": max(base["median"] - 3.0, base["p25"]),
            "min_p25": max(base["p25"] - 2.0, 0.0),
            "min_p75": base["median"],
            "count": base["count"],
        }
    avg_prems.sort()
    min_prems = sorted(min_prems) if min_prems else list(avg_prems)
    return {
        "avg_median": _percentile(avg_prems, 0.5),
        "avg_p25": _percentile(avg_prems, 0.25),
        "avg_p75": _percentile(avg_prems, 0.75),
        "min_median": _percentile(min_prems, 0.5),
        "min_p25": _percentile(min_prems, 0.25),
        "min_p75": _percentile(min_prems, 0.75),
        "count": float(len(avg_prems)),
    }


def peer_lot_floor(auction_lots: int | None) -> int | None:
    """依本案競拍張數，選可比歷史樣本的最小張數門檻。"""
    if not auction_lots:
        return None
    if auction_lots >= 20_000:
        return 10_000
    if auction_lots >= 10_000:
        return 5_000
    if auction_lots >= 3_000:
        return 1_000
    return None


def blended_cb_win_stats(
    raw_rows: list[dict[str, Any]],
    auction_lots: int | None,
) -> dict[str, float]:
    """全體樣本與同規模樣本加權，避免大型案低估熱度、小型案過度追高。"""
    all_stats = historical_win_stats(raw_rows, asset_kind="cb")
    lot_floor = peer_lot_floor(auction_lots)
    if not lot_floor:
        return all_stats
    size_stats = historical_win_stats(
        raw_rows, asset_kind="cb", min_auction_lots=lot_floor
    )
    if size_stats["count"] < 8:
        return all_stats
    if auction_lots and auction_lots >= 20_000:
        w = 0.65
    elif auction_lots and auction_lots >= 10_000:
        w = 0.55
    else:
        w = 0.40
    out: dict[str, float] = {"count": size_stats["count"]}
    for key in (
        "avg_median",
        "avg_p25",
        "avg_p75",
        "min_median",
        "min_p25",
        "min_p75",
    ):
        out[key] = w * size_stats[key] + (1.0 - w) * all_stats[key]
    return out


def classify_status(row: dict[str, Any], today: date) -> str:
    if row.get("cancelled"):
        return "cancelled"
    bid_start = parse_twse_date(str(row.get("bid_start") or ""))
    bid_end = parse_twse_date(str(row.get("bid_end") or ""))
    open_day = parse_twse_date(str(row.get("open_date") or ""))
    if bid_start and bid_end and bid_start <= today <= bid_end:
        return "bidding"
    if bid_start and today < bid_start:
        return "upcoming"
    if open_day and today <= open_day:
        return "awaiting_result"
    if row.get("avg_win") or row.get("min_win") or row.get("underwrite"):
        return "completed"
    return "past"


def score_stock_quality(row: dict[str, Any], otc_price: float | None) -> tuple[int, list[str]]:
    """粗估公司／案件體質（0-100）。"""
    score = 55
    notes: list[str] = []
    issue = str(row.get("issue_type") or "")
    floor = float(row.get("floor") or 0)
    lots = int(row.get("auction_lots") or 0)

    if "創新板" in issue:
        score -= 8
        notes.append("創新板案件，波動與流動性風險較高")
    elif "第一上市" in issue or "第一上櫃" in issue:
        score -= 5
        notes.append("第一上市／上櫃，資訊揭露與可比性較弱")
    elif "初上市" in issue:
        score += 4
        notes.append("一般板初上市，體質評分略加")
    elif "初上櫃" in issue:
        score += 2
        notes.append("一般板初上櫃")

    # 競拍規模：太小較易被炒、太大較穩
    notional = floor * lots * STOCK_LOT_SHARES / 1e8  # 億
    if notional >= 20:
        score += 6
        notes.append(f"競拍規模約 {notional:.1f} 億，規模較大")
    elif notional >= 5:
        score += 2
    elif 0 < notional < 1.5:
        score -= 6
        notes.append(f"競拍規模約 {notional:.1f} 億，偏小型、投機性較高")

    if otc_price and floor > 0:
        ratio = otc_price / floor
        if ratio >= 2.5:
            score -= 4
            notes.append(f"興櫃價為底標 {ratio:.1f} 倍，市場預期熱、泡沫風險上升")
        elif ratio >= 1.5:
            score += 3
            notes.append(f"興櫃價為底標 {ratio:.1f} 倍，具合理溢價空間")
        elif ratio < 1.1:
            score -= 5
            notes.append("興櫃價貼近底標，上檔空間有限或市場偏冷")

    return max(5, min(95, score)), notes


def score_market_sentiment(stock_stats: dict[str, float]) -> tuple[int, list[str]]:
    """依近期股票競拍得標溢價判斷市場情緒。"""
    median = stock_stats.get("median", 35)
    notes: list[str] = []
    if median >= 70:
        score = 85
        notes.append(f"近期股票競拍中位溢價約 {median:.0f}%，市場情緒偏熱")
    elif median >= 40:
        score = 68
        notes.append(f"近期股票競拍中位溢價約 {median:.0f}%，情緒中偏熱")
    elif median >= 20:
        score = 52
        notes.append(f"近期股票競拍中位溢價約 {median:.0f}%，情緒中性")
    else:
        score = 35
        notes.append(f"近期股票競拍中位溢價約 {median:.0f}%，情緒偏冷、可更保守")
    if stock_stats.get("count", 0):
        notes.append(f"樣本 {int(stock_stats['count'])} 檔已開標股票競拍")
    return score, notes


def analyze_stock_bid_range(
    floor: float,
    otc_price: float | None,
    stock_stats: dict[str, float],
    quality: int,
    sentiment: int,
) -> tuple[float, float, float, float | None, str, list[str]]:
    """股票競拍：依興櫃價折價 + 歷史溢價建議標價區間。"""
    notes: list[str] = []
    p25, median, p75 = stock_stats["p25"], stock_stats["median"], stock_stats["p75"]

    # 興櫃折價：體質好／情緒熱 → 少打折；反之多打折
    # 基準折價 18%，quality/sentiment 各可加減約 8%
    base_discount = 0.18
    adj = ((quality - 50) + (sentiment - 50)) / 100 * 0.16
    discount = max(0.06, min(0.35, base_discount - adj))

    if otc_price and otc_price > 0:
        fair = otc_price * (1 - discount)
        low = max(floor, otc_price * (1 - discount - 0.08))
        high = max(low, otc_price * (1 - max(0.03, discount - 0.07)))
        # 不要超過興櫃價本身
        high = min(high, otc_price * 0.98)
        notes.append(
            f"興櫃／參考價 {otc_price:.2f} 元，建議相對興櫃折價約 {discount * 100:.0f}%"
            f"（約 {(1 - discount) * 10:.1f} 折；體質 {quality}/情緒 {sentiment}）"
        )
        advice = (
            "以興櫃價為錨、打適當折價階梯標；低價多張搶便宜、高價少張保命中。"
            "興櫃轉上市仍有破發與流動性風險，勿用滿額追高。"
        )
    else:
        fair = floor * (1 + median / 100)
        low = max(floor, floor * (1 + p25 / 100))
        high = floor * (1 + p75 / 100)
        discount = None
        notes.append("未取得興櫃價，改以歷史股票競拍相對底標溢價估算")
        advice = "缺乏興櫃錨定價時，以歷史得標溢價為主，建議保守、控制總預算。"

    # 歷史底標溢價上緣當 sanity check：避免低於歷史過熱時仍喊太高
    hist_high = floor * (1 + p75 / 100)
    if high > hist_high * 1.15 and otc_price:
        notes.append(
            f"建議上緣已高於歷史 P75 溢價價位 {hist_high:.2f}，留意過熱追價風險"
        )

    return (
        round(low, 2),
        round(max(high, low), 2),
        round(fair, 2),
        round(discount * 100, 1) if discount is not None else None,
        advice,
        notes,
    )


def build_stock_position_plan(
    auction: AuctionRow,
    quality: int,
    sentiment: int,
) -> PositionPlan:
    """股票競拍部位：依案件熱度／股價級距給預算與階梯標單筆數。"""
    floor = auction.floor_price
    lots = auction.auction_lots or 0
    notional_yi = floor * lots * STOCK_LOT_SHARES / 1e8 if floor and lots else 0
    fair_est = auction.fair_value or auction.otc_price or floor or 1
    lot_cost = max(int(fair_est * STOCK_LOT_SHARES), 1)

    heat = (quality + sentiment) / 2
    reasons: list[str] = []

    # 高價股（單張成本高）改以「目標張數」定預算，才能排出多筆階梯
    if lot_cost >= 800_000:
        target_lots = 3
        if heat >= 55:
            target_lots = 5
        if heat >= 70:
            target_lots = 7
        if heat >= 80:
            target_lots = 8
        if quality < 40:
            target_lots = max(3, target_lots - 2)
            reasons.append("體質偏弱，減少張數")
        if "創新板" in auction.bond_type:
            target_lots = max(3, target_lots - 1)
            reasons.append("創新板風險，減少張數")
        budget = lot_cost * target_lots
        tier = "mid" if target_lots >= 5 else "small"
        n_tickets = target_lots
        reasons.append(f"高價股單張約 {lot_cost/1e4:.0f} 萬，以 {target_lots} 張規劃階梯")
        base = budget
    else:
        if heat >= 75 and notional_yi >= 5:
            tier, base = "large", 4_000_000
        elif heat >= 60:
            tier, base = "mid", 2_500_000
        elif heat >= 45:
            tier, base = "small", 1_500_000
        else:
            tier, base = "micro", 800_000
        budget = base
        if auction.otc_price and floor > 0:
            upside = auction.otc_price / floor - 1
            if upside >= 1.0:
                budget = int(budget * 1.15)
                reasons.append("興櫃相對底標空間大，部位略增")
            elif upside < 0.25:
                budget = int(budget * 0.75)
                reasons.append("興櫃相對底標空間有限，部位縮減")
        if "創新板" in auction.bond_type:
            budget = int(budget * 0.85)
            reasons.append("創新板風險折扣")
        if quality < 40:
            budget = int(budget * 0.8)
            reasons.append("體質偏弱，降低曝險")
        n_tickets = tickets_for_tier(tier)
        if heat >= 70:
            n_tickets = max(n_tickets, 7)
        if heat >= 80:
            n_tickets = max(n_tickets, 8)

    tickets: list[BidTicket] = []
    if auction.bid_low is not None and auction.bid_high is not None and auction.fair_value is not None:
        tickets = build_ladder_tickets(
            budget_twd=budget,
            floor=auction.floor_price,
            bid_low=auction.bid_low,
            bid_high=auction.bid_high,
            fair=auction.fair_value,
            auction_lots=auction.auction_lots,
            min_lot=auction.min_lot or 1,
            n_tickets=n_tickets,
            lot_value_fn=lambda price, lot: int(price * lot * STOCK_LOT_SHARES),
            max_lot=auction.max_lot,
            weight_mode="cheap_heavy",
        )

    gross = sum(t.amount_twd for t in tickets) or budget
    deposit = int(gross * auction.deposit_ratio)
    zhe = None
    if auction.discount_pct is not None:
        zhe = (100 - auction.discount_pct) / 10  # 16% off → 8.4 折

    rationale = (
        f"股票競拍｜體質 {quality}/100、情緒 {sentiment}/100；"
        f"基準配置 {base / 1e4:.0f} 萬"
        + (("；" + "；".join(reasons)) if reasons else "")
        + f"；建議 {len(tickets) or n_tickets} 筆階梯標"
        + (f"；相對興櫃折價約 {auction.discount_pct:.0f}%（約 {zhe:.1f} 折）" if zhe else "")
    )
    return PositionPlan(
        size_tier=tier,
        market_cap_yi=notional_yi or None,
        target_budget_twd=budget,
        deposit_est_twd=deposit,
        tickets=tickets,
        rationale=rationale,
        purpose_category="stock_ipo",
        purpose_score=quality,
        purpose_label="股票競拍",
        purpose_text=auction.bond_type,
    )


def analyze_bid_range(
    floor: float,
    parity: float | None,
    premium_stats: dict[str, float],
    putback: float | None = None,
    win_stats: dict[str, float] | None = None,
) -> tuple[float, float, float, float, float, str, list[str]]:
    """回傳 low, high, fair, est_market_avg, est_clear, advice, notes。

    策略：寧可少賺一點也提高命中率，並讓得標均價目標低於預估全場均價。
    核心想法：多數張數放在「預估清算價 ~ 全場均價」下半段，少量保險倉略高於均價。
    """
    notes: list[str] = []
    if win_stats:
        avg_med = win_stats["avg_median"]
        avg_p25 = win_stats["avg_p25"]
        avg_p75 = win_stats["avg_p75"]
        min_med = win_stats["min_median"]
        min_p25 = win_stats["min_p25"]
        min_p75 = win_stats["min_p75"]
    else:
        avg_med = premium_stats["median"]
        avg_p25 = premium_stats["p25"]
        avg_p75 = premium_stats["p75"]
        min_med = max(avg_med - 3.0, avg_p25)
        min_p25 = max(avg_p25 - 2.0, 0.0)
        min_p75 = avg_med

    # 清算價略打折，避免大型案同規模溢價把核心倉推到全場均價之上
    clear_raw = floor * (1 + min_med / 100)
    clear_est = clear_raw * 0.99
    # 歷史上得標均價約 = 最低得標 × 1.022；再與直接均價溢價取較保守者
    market_avg = min(clear_est * 1.022, floor * (1 + avg_med / 100))

    if parity is not None and parity >= 100:
        fair = min(parity * 0.985, clear_est * 1.008, market_avg * 0.992)
        low = max(floor, min(clear_est * 0.988, fair * 0.975))
        high = max(min(parity * 1.015, market_avg * 1.03), clear_est * 1.04)
        advice = (
            "價內標的：以提高命中率為主，標單集中在預估清算價～全場均價附近；"
            "目標得標均價低於全場均價。"
        )
        notes.append(f"轉換價值 {parity:.1f}%，屬價內標的。")
    else:
        fair = min(clear_est * 1.008, market_avg * 0.992)
        # 下緣貼近清算帶，減少「絕對到不了」的過低標
        low = max(floor, clear_est * 0.988, floor * (1 + min_p25 / 100 * 0.9))
        high = max(market_avg * 1.018, clear_est * 1.045, floor * (1 + min_p75 / 100 * 0.9))
        # 上緣不要被高分位拉太高（命中靠集中，不靠追最高）
        high = min(high, market_avg * 1.035)
        high = max(high, fair + max(floor * 0.01, 1.0))
        advice = (
            "策略偏命中率：寧可少賺一點，標單集中預估清算附近；"
            "目標得標均價低於全場競拍均價。"
        )
        if parity is not None:
            notes.append(f"轉換價值 {parity:.1f}%，屬價外，主要受底標與信用/賣回條件支撐。")

    notes.append(f"預估最低得標約 {clear_est:.2f}｜預估全場均價約 {market_avg:.2f}")
    notes.append(
        f"歷史溢價樣本：均價中位 {avg_med:.1f}%／清算中位 {min_med:.1f}%"
        f"（P25~P75 均價 {avg_p25:.1f}%~{avg_p75:.1f}%）"
    )

    if putback and putback > floor:
        notes.append(f"賣回價 {putback:.2f} 元，可視為軟性下限參考。")
        low = max(low, min(putback, floor * 1.01))

    low = min(low, fair)
    high = max(high, fair)
    return (
        round(low, 2),
        round(high, 2),
        round(fair, 2),
        round(market_avg, 2),
        round(clear_est, 2),
        advice,
        notes,
    )


def normalize_auction(
    row: dict[str, Any],
    ipo_map: dict[str, CbIpoMeta],
    premium_stats: dict[str, float],
    today: date,
    *,
    enrich: bool = True,
    stock_cache: dict[str, float | None] | None = None,
    bond_cache: dict[tuple[str, str], dict[str, Any]] | None = None,
    shares_map: dict[str, int] | None = None,
    purpose_cache: dict[str, PurposeInfo] | None = None,
    stock_premium_stats: dict[str, float] | None = None,
    raw_rows: list[dict[str, Any]] | None = None,
) -> AuctionRow:
    code = str(row.get("code") or "")
    asset_kind = str(row.get("asset_kind") or "cb")
    stock_code = code if asset_kind == "stock" else infer_stock_code(code)
    meta = ipo_map.get(code)
    if meta and meta.stock_code:
        stock_code = meta.stock_code

    floor = float(row.get("floor") or 0)
    bid_start = str(row.get("bid_start") or "")
    bid_end = str(row.get("bid_end") or "")
    bid_period = f"{bid_start.replace('/', '-')}~{bid_end.replace('/', '-')}"
    auction = AuctionRow(
        bond_code=code,
        name=str(row.get("name") or ""),
        bond_type=str(row.get("issue_type") or ""),
        auction_method=str(row.get("method") or ""),
        market_label=str(row.get("market") or ""),
        bid_period=bid_period,
        open_date=fmt_twse_date(str(row.get("open_date") or "")),
        listing_date=fmt_twse_date(str(row.get("listing_date") or "")),
        broker=str(row.get("broker") or ""),
        auction_lots=row.get("auction_lots"),
        floor_price=floor,
        min_lot=int(row.get("min_lot") or 1),
        min_win_price=row.get("min_win"),
        max_win_price=row.get("max_win"),
        underwriting_price=row.get("underwrite"),
        cancelled=str(row.get("cancelled") or ""),
        status=classify_status(row, today),
        stock_code=stock_code,
        meta=meta,
        asset_kind=asset_kind,
        deposit_ratio=(float(row.get("deposit_pct") or 50) / 100.0),
        max_lot=row.get("max_lot"),
        fee_per_ticket=int(row.get("fee") or 400),
    )

    if not enrich:
        return auction

    if asset_kind == "stock":
        otc = fetch_emerging_price(stock_code, stock_cache)
        auction.otc_price = otc
        auction.stock_price = otc
        quality, q_notes = score_stock_quality(row, otc)
        sentiment, s_notes = score_market_sentiment(stock_premium_stats or historical_premium_stats([], asset_kind="stock"))
        auction.quality_score = quality
        auction.sentiment_score = sentiment
        low, high, fair, discount, advice, notes = analyze_stock_bid_range(
            floor, otc, stock_premium_stats or historical_premium_stats([], asset_kind="stock"), quality, sentiment
        )
        auction.bid_low = low
        auction.bid_high = high
        auction.fair_value = fair
        auction.discount_pct = discount
        auction.advice = advice
        auction.notes.extend(notes)
        auction.notes.extend(q_notes)
        auction.notes.extend(s_notes)
        if auction.auction_lots:
            auction.notes.insert(0, f"競拍張數 {auction.auction_lots:,}｜單標上限 {auction.max_lot or '-'} 張")
        auction.position = build_stock_position_plan(auction, quality, sentiment)
        return auction

    # ---- CB path (保留原估值／用途／部位邏輯) ----
    conversion = meta.conversion_price if meta and meta.conversion_price else None
    putback = None
    if stock_code:
        cache_key = (stock_code, code)
        if bond_cache is not None and cache_key in bond_cache:
            bond_terms = bond_cache[cache_key]
        else:
            bond_terms = fetch_bond_terms(stock_code, code)
            if bond_cache is not None:
                bond_cache[cache_key] = bond_terms
        if bond_terms.get("v28"):
            conversion = float(bond_terms["v28"])
        if bond_terms.get("v31"):
            putback = float(bond_terms["v31"])
        auction.stock_price = fetch_stock_close(stock_code, today, stock_cache)

    auction.conversion_price = conversion
    if auction.stock_price and conversion:
        auction.parity = round(auction.stock_price / conversion * 100, 2)

    win_stats = None
    if raw_rows is not None:
        win_stats = blended_cb_win_stats(raw_rows, auction.auction_lots)
    low, high, fair, est_avg, est_clear, advice, notes = analyze_bid_range(
        floor, auction.parity, premium_stats, putback, win_stats=win_stats
    )
    auction.bid_low = low
    auction.bid_high = high
    auction.fair_value = fair
    auction.est_market_avg = est_avg
    auction.est_clear_price = est_clear
    auction.advice = advice
    auction.notes = notes
    if meta and meta.premium_pct is not None:
        auction.notes.append(f"公告轉換溢價率 {meta.premium_pct:.2f}%")
    if conversion:
        auction.notes.insert(0, f"轉換價 {conversion:.2f} 元")
    if auction.stock_price:
        auction.notes.insert(0, f"正股 {stock_code} 最近收盤 {auction.stock_price:.2f} 元")

    if shares_map is not None:
        auction.position = build_position_plan(auction, shares_map, purpose_cache)
    return auction


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"auctions": {}, "last_run": None}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def detect_alerts(
    current: list[AuctionRow], previous: dict[str, Any]
) -> tuple[list[AuctionRow], list[str]]:
    alerts: list[AuctionRow] = []
    reasons: list[str] = []
    prev_map = previous.get("auctions", {})
    for item in current:
        if item.status in ("cancelled", "completed", "past"):
            continue
        kind = "可轉債" if item.asset_kind == "cb" else "股票"
        prev = prev_map.get(item.bond_code)
        if prev is None:
            alerts.append(item)
            reasons.append(f"新{kind}標的：{item.name} ({item.bond_code})")
            continue
        if prev.get("status") != item.status and item.status in (
            "bidding",
            "upcoming",
            "awaiting_result",
        ):
            alerts.append(item)
            reasons.append(f"狀態變更：{item.name} {prev.get('status')} -> {item.status}")
        elif item.status == "bidding" and (
            prev.get("bid_low") != item.bid_low
            or prev.get("stock_price") != item.stock_price
            or prev.get("otc_price") != item.otc_price
        ):
            alerts.append(item)
            reasons.append(f"投標中更新：{item.name} 價格／估值已更新")
    return alerts, reasons


def format_tickets(plan: PositionPlan) -> list[str]:
    lines = [
        f"  部位建議：{plan.target_budget_twd / 1e4:.0f} 萬"
        f" | 預估保證金約 {plan.deposit_est_twd / 1e4:.0f} 萬",
        f"  配置理由：{plan.rationale}",
    ]
    if plan.tickets:
        lines.append("  建議標單（階梯）：")
        role_zh = {"cheap": "便宜倉", "core": "核心倉", "insure": "保險倉"}
        for i, t in enumerate(plan.tickets, 1):
            lines.append(
                f"    #{i} {role_zh.get(t.role, t.role)}：{t.price:.2f} 元 × {t.lots} 張"
                f"（約 {t.amount_twd / 1e4:.1f} 萬）"
            )
        avg = sum(t.price * t.lots for t in plan.tickets) / max(
            sum(t.lots for t in plan.tickets), 1
        )
        if plan.purpose_category == "stock_ipo":
            lines.append(
                f"  加權平均投標價約 {avg:.2f} 元；低價多張搶便宜、高價少張保命中。"
            )
        else:
            lines.append(
                f"  加權平均投標價約 {avg:.2f} 元；"
                "張數集中合理價附近以提高命中率，目標得標均價低於全場均價。"
            )
    return lines


def format_auction_lines(a: AuctionRow) -> list[str]:
    kind = "可轉債" if a.asset_kind == "cb" else "股票競拍"
    lines = [
        f"[{kind}] {a.name} ({a.bond_code}) / {a.bond_type} / 狀態 {a.status}",
        f"  投標期間：{a.bid_period} | 開標：{a.open_date} | 底標：{a.floor_price:.2f}",
    ]
    if a.asset_kind == "stock" and a.otc_price:
        disc = (
            f"（折價約 {a.discount_pct:.0f}%／約 {(100 - a.discount_pct) / 10:.1f} 折）"
            if a.discount_pct is not None
            else ""
        )
        lines.append(f"  興櫃／參考價：{a.otc_price:.2f} 元{disc}")
    if a.bid_low is not None and a.bid_high is not None and a.fair_value is not None:
        lines.append(
            f"  建議投標區間：{a.bid_low:.2f} ~ {a.bid_high:.2f} 元"
            f" | 合理價參考：{a.fair_value:.2f} 元"
        )
    if a.advice:
        lines.append(f"  建議：{a.advice}")
    lines.extend(f"  - {n}" for n in a.notes)
    if a.position:
        lines.extend(format_tickets(a.position))
    return lines


def render_text_report(
    alerts: list[AuctionRow], all_active: list[AuctionRow], reasons: list[str]
) -> str:
    lines = [
        "TWSE 競價拍賣監控報告（可轉債 + 股票）",
        "來源：https://www.twse.com.tw/zh/announcement/auction.html",
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


def _load_font(size: int):
    from PIL import ImageFont

    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_share_card(auction: AuctionRow, out_path: Path) -> Path:
    """產生可分享的競拍建議圖卡（手機轉傳友善）。"""
    from PIL import Image, ImageDraw

    plan = auction.position
    tickets = plan.tickets if plan else []
    # 1080 寬度方便手機；高度依標單筆數動態調整
    width = 1080
    row_h = 72
    header_h = 420
    footer_h = 110
    height = header_h + max(len(tickets), 1) * row_h + footer_h + 40

    # 深墨藍 + 琥珀強調（避免常見 AI 紫／奶油風）
    bg = (18, 28, 38)
    panel = (28, 42, 56)
    line = (48, 68, 86)
    text = (236, 240, 244)
    muted = (156, 172, 188)
    accent = (242, 169, 59)
    cheap_c = (72, 187, 156)
    core_c = (96, 165, 250)
    insure_c = (248, 113, 113)

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    font_title = _load_font(54)
    font_h2 = _load_font(36)
    font_body = _load_font(30)
    font_small = _load_font(24)
    font_tiny = _load_font(20)

    def text_w(s: str, font) -> int:
        box = draw.textbbox((0, 0), s, font=font)
        return box[2] - box[0]

    y = 36
    title_kind = "可轉債競拍建議" if auction.asset_kind == "cb" else "股票競拍建議"
    draw.text((48, y), title_kind, fill=accent, font=font_small)
    y += 42
    title = f"{auction.name}  {auction.bond_code}"
    draw.text((48, y), title, fill=text, font=font_title)
    y += 70
    if auction.asset_kind == "stock":
        sub = (
            f"{auction.bond_type}　投標 {auction.bid_period}　開標 {auction.open_date}"
        )
    else:
        sub = (
            f"正股 {auction.stock_code or '-'}　投標 {auction.bid_period}　開標 {auction.open_date}"
        )
    draw.text((48, y), sub, fill=muted, font=font_small)
    y += 48

    # 重點數據列
    if auction.asset_kind == "stock":
        purpose_line = (
            f"興櫃／參考 {auction.otc_price:.2f} 元"
            if auction.otc_price
            else "興櫃價未取得"
        )
        if auction.discount_pct is not None:
            purpose_line += (
                f"｜折價約 {auction.discount_pct:.0f}%"
                f"（{(100 - auction.discount_pct) / 10:.1f} 折）"
            )
        purpose_line += f"｜體質 {auction.quality_score}/情緒 {auction.sentiment_score}"
        metrics = [
            ("底標", f"{auction.floor_price:.2f}"),
            ("建議區間", f"{auction.bid_low:.2f}–{auction.bid_high:.2f}" if auction.bid_low else "-"),
            ("合理價", f"{auction.fair_value:.2f}" if auction.fair_value else "-"),
            ("部位", f"{(plan.target_budget_twd / 1e4):.0f} 萬" if plan else "-"),
        ]
    else:
        purpose_lbl = plan.purpose_label if plan else "用途未明"
        purpose_score = plan.purpose_score if plan else 50
        purpose_line = f"資金用途：{purpose_lbl}（{purpose_score}/100）"
        if plan and plan.purpose_text:
            purpose_line += f"｜{plan.purpose_text}"
        metrics = [
            ("底標", f"{auction.floor_price:.2f}"),
            ("建議區間", f"{auction.bid_low:.2f}–{auction.bid_high:.2f}" if auction.bid_low else "-"),
            ("合理價", f"{auction.fair_value:.2f}" if auction.fair_value else "-"),
            ("部位", f"{(plan.target_budget_twd / 1e4):.0f} 萬" if plan else "-"),
        ]
    box_w = (width - 48 * 2 - 18 * 3) // 4
    for i, (label, value) in enumerate(metrics):
        x = 48 + i * (box_w + 18)
        draw.rounded_rectangle((x, y, x + box_w, y + 110), radius=16, fill=panel)
        draw.text((x + 20, y + 18), label, fill=muted, font=font_tiny)
        # 縮小過長數值
        f = font_h2 if text_w(value, font_h2) < box_w - 28 else font_body
        draw.text((x + 20, y + 52), value, fill=text, font=f)
    y += 120
    draw.text((48, y), purpose_line[:42], fill=accent, font=font_small)
    y += 40

    draw.text((48, y), f"階梯標單（共 {len(tickets)} 筆）", fill=text, font=font_h2)
    y += 50

    role_zh = {"cheap": "便宜倉", "core": "核心倉", "insure": "保險倉"}
    role_color = {"cheap": cheap_c, "core": core_c, "insure": insure_c}
    # 表頭
    draw.rounded_rectangle((48, y, width - 48, y + 44), radius=10, fill=panel)
    headers = [(70, "#"), (150, "倉位"), (400, "投標價"), (620, "張數"), (820, "金額")]
    for x, h in headers:
        draw.text((x, y + 8), h, fill=muted, font=font_small)
    y += 52

    for i, t in enumerate(tickets, 1):
        if i % 2 == 1:
            draw.rounded_rectangle((48, y - 4, width - 48, y + row_h - 10), radius=10, fill=panel)
        rc = role_color.get(t.role, accent)
        draw.ellipse((70, y + 18, 86, y + 34), fill=rc)
        draw.text((100, y + 12), str(i), fill=text, font=font_body)
        draw.text((150, y + 12), role_zh.get(t.role, t.role), fill=rc, font=font_body)
        draw.text((400, y + 12), f"{t.price:.2f} 元", fill=text, font=font_body)
        draw.text((620, y + 12), f"{t.lots} 張", fill=text, font=font_body)
        draw.text((820, y + 12), f"{t.amount_twd / 1e4:.1f} 萬", fill=text, font=font_body)
        y += row_h

    y = height - footer_h
    draw.line((48, y, width - 48, y), fill=line, width=1)
    y += 16
    if plan:
        avg = (
            sum(t.price * t.lots for t in tickets) / max(sum(t.lots for t in tickets), 1)
            if tickets
            else 0
        )
        draw.text(
            (48, y),
            f"加權均價約 {avg:.2f} 元｜預估保證金約 {plan.deposit_est_twd / 1e4:.0f} 萬｜僅供研究分享",
            fill=muted,
            font=font_tiny,
        )
        y += 32
        # 截短理由避免溢出
        rationale = plan.rationale
        if len(rationale) > 48:
            rationale = rationale[:48] + "…"
        draw.text((48, y), rationale, fill=muted, font=font_tiny)
    else:
        draw.text((48, y), "僅供研究分享，不構成投資建議", fill=muted, font=font_tiny)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG", optimize=True)
    return out_path


def generate_share_cards(auctions: list[AuctionRow]) -> list[tuple[AuctionRow, Path]]:
    results: list[tuple[AuctionRow, Path]] = []
    stamp = datetime.now(TPE).strftime("%Y%m%d_%H%M")
    for a in auctions:
        if not a.position or not a.position.tickets:
            continue
        prefix = "cb" if a.asset_kind == "cb" else "stk"
        path = CARD_DIR / f"{prefix}_{a.bond_code}_{stamp}.png"
        results.append((a, render_share_card(a, path)))
    return results


def render_html_report(text: str, alerts: list[AuctionRow], card_cids: list[tuple[str, str]] | None = None) -> str:
    parts = [
        "<html><body style='font-family:sans-serif;line-height:1.5;color:#111;'>",
        "<h2>TWSE 競價拍賣監控（可轉債 + 股票）</h2>",
        f"<p>產生時間：{escape(datetime.now(TPE).strftime('%Y-%m-%d %H:%M'))} (Asia/Taipei)</p>",
        "<p>資料來源：<a href='https://www.twse.com.tw/zh/announcement/auction.html'>證交所競價拍賣公告</a></p>",
        "<p>下方附上可分享圖卡（也可直接轉傳附件 PNG）。</p>",
    ]
    if card_cids:
        parts.append("<h3>分享圖卡</h3>")
        for cid, label in card_cids:
            parts.append(f"<p><b>{escape(label)}</b><br>")
            parts.append(f"<img src='cid:{cid}' alt='{escape(label)}' style='max-width:100%;border-radius:12px;'/></p>")
    if alerts:
        parts.append("<h3>需關注標的</h3>")
        for a in alerts:
            parts.append(
                "<div style='border:1px solid #ddd;border-radius:8px;padding:12px;margin:12px 0;'>"
            )
            parts.append(
                f"<h4 style='margin:0 0 8px;'>{escape(a.name)} ({escape(a.bond_code)})</h4>"
            )
            parts.append("<ul style='margin:0;padding-left:18px;'>")
            for line in format_auction_lines(a)[1:]:
                parts.append(f"<li>{escape(line.strip())}</li>")
            parts.append("</ul></div>")
    parts.append(
        "<pre style='background:#f7f7f7;padding:12px;border-radius:8px;white-space:pre-wrap;'>"
    )
    parts.append(escape(text))
    parts.append(
        "</pre><p style='color:#666;font-size:12px;'>本報告僅供研究參考，不構成投資建議。</p>"
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

        # 再附一份 attachment，方便手機另存／轉傳
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
    raw = fetch_auctions()
    ipo_map = fetch_cb_ipo_table()
    cb_stats = historical_premium_stats(raw, asset_kind="cb")
    stock_stats = historical_premium_stats(raw, asset_kind="stock")
    shares_map = load_shares_outstanding()

    stock_cache: dict[str, float | None] = {}
    bond_cache: dict[tuple[str, str], dict[str, Any]] = {}
    purpose_cache: dict[str, PurposeInfo] = {}
    today_rows: list[AuctionRow] = []
    for row in raw:
        basic = normalize_auction(row, ipo_map, cb_stats, today, enrich=False)
        enrich = basic.status in ("bidding", "upcoming", "awaiting_result")
        today_rows.append(
            normalize_auction(
                row,
                ipo_map,
                cb_stats if basic.asset_kind == "cb" else stock_stats,
                today,
                enrich=enrich,
                stock_cache=stock_cache,
                bond_cache=bond_cache,
                shares_map=shares_map if enrich and basic.asset_kind == "cb" else None,
                purpose_cache=purpose_cache if enrich and basic.asset_kind == "cb" else None,
                stock_premium_stats=stock_stats,
                raw_rows=raw,
            )
        )

    active = [a for a in today_rows if a.status in ("bidding", "upcoming", "awaiting_result")]
    state = load_state()
    alerts, reasons = detect_alerts(today_rows, state)
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
        subject = "[TWSE競拍]"
        if alerts:
            subject += f" {alerts[0].name} 等 {len(alerts)} 檔需關注"
        else:
            subject += " 監控摘要"
        card_cids = [(p.stem, f"{a.name} ({a.bond_code})") for a, p in card_pairs]
        html = render_html_report(report, focus, card_cids=card_cids)
        send_email(subject, report, html, card_paths=card_paths)
        print(f"\n已寄送 Email（含圖卡）")

    if not args.dry_run:
        save_state(
            {
                "last_run": datetime.now(TPE).isoformat(),
                "auctions": {a.bond_code: asdict(a) for a in today_rows},
            }
        )
    return 0


def run_stock_subscription_monitor(args: argparse.Namespace) -> int:
    """Also check HiStock 公開申購 so existing Automation need not change prompt."""
    if os.getenv("SKIP_STOCK_SUB_MONITOR") == "1":
        return 0
    script = ROOT / "scripts" / "monitor_stock_subscription.py"
    if not script.exists():
        print(f"WARN: 找不到 {script}，略過股票申購監控", file=sys.stderr)
        return 0
    cmd = [sys.executable, str(script)]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.force_notify:
        cmd.append("--force-notify")
    elif args.notify:
        cmd.append("--notify")
    print("\n===== 接續執行股票公開申購監控 =====\n")
    completed = subprocess.run(cmd, check=False)
    return int(completed.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor Taiwan CB auctions")
    parser.add_argument("--dry-run", action="store_true", help="不寫入 state、不寄信")
    parser.add_argument("--notify", action="store_true", help="有異動才寄信")
    parser.add_argument("--force-notify", action="store_true", help="無論是否有異動都寄信")
    args = parser.parse_args()
    if args.force_notify:
        args.notify = True
    try:
        rc = run(args)
        sub_rc = run_stock_subscription_monitor(args)
        return rc or sub_rc
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
