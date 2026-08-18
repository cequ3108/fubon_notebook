#!/usr/bin/env python3
"""Fetch today's Taiwan market snapshot via Fubon Neo test account."""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

from fubon_neo.sdk import FubonSDK

TPE = timezone(timedelta(hours=8))
TEST_URL = os.getenv("NEOAPI_TEST_URL", "wss://neoapitest.fbs.com.tw/TASP/XCPXWS")
CERT_DIR = os.getenv("NEOAPI_CERT_DIR", "/workspace/.sdk/test_env/test_environment")

SYMBOLS = [
    "2330", "2317", "2454", "2303", "2881", "2882", "0050", "0056",
    "2002", "2603", "3231", "1519", "3037", "2382", "3711",
]


def pick_cert():
    cert_path = os.getenv("NEOAPI_TEST_CERT_PATH")
    if cert_path and os.path.isfile(cert_path):
        return cert_path
    for name in ("41610792.pfx", "58581758.pfx"):
        path = os.path.join(CERT_DIR, name)
        if os.path.isfile(path):
            return path
    raise FileNotFoundError(f"No test certificate found under {CERT_DIR}")


def login_sdk():
    cert_path = pick_cert()
    test_id = os.getenv("NEOAPI_TEST_ID", os.path.splitext(os.path.basename(cert_path))[0])
    password = os.getenv("NEOAPI_TEST_PASSWORD", "12345678")
    cert_pwd = os.getenv("NEOAPI_TEST_CERT_PASSWORD", "12345678")

    sdk = FubonSDK(30, 2, url=TEST_URL)
    result = sdk.login(test_id, password, cert_path, cert_pwd)
    if not result.is_success:
        raise RuntimeError(f"Login failed: {result.message}")
    acc = next(a for a in result.data if getattr(a, "account_type", "") in ("S", "Stock", "stock") or "證券" in str(getattr(a, "name", "")))
    if acc is None:
        acc = result.data[0]
    return sdk, acc, test_id


def safe_quote(rest, symbol):
    try:
        return rest.intraday.quote(symbol=symbol)
    except Exception as exc:
        return {"symbol": symbol, "error": str(exc)}


def safe_ticker(rest, symbol):
    try:
        return rest.intraday.ticker(symbol=symbol)
    except Exception as exc:
        return {"symbol": symbol, "error": str(exc)}


def main():
    sdk, acc, test_id = login_sdk()
    sdk.init_realtime()
    rest = sdk.marketdata.rest_client.stock

    now_tpe = datetime.now(TPE)
    rows = []
    for symbol in SYMBOLS:
        quote = safe_quote(rest, symbol)
        ticker = safe_ticker(rest, symbol)
        qsym = quote.get("symbol", symbol)
        rows.append({
            "symbol": qsym,
            "name": quote.get("name") or ticker.get("name"),
            "date": quote.get("date") or ticker.get("date"),
            "lastPrice": quote.get("lastPrice"),
            "change": quote.get("change"),
            "changePercent": quote.get("changePercent"),
            "openPrice": quote.get("openPrice"),
            "highPrice": quote.get("highPrice"),
            "lowPrice": quote.get("lowPrice"),
            "referencePrice": quote.get("referencePrice") or ticker.get("referencePrice"),
            "previousClose": quote.get("previousClose") or ticker.get("previousClose"),
            "avgPrice": quote.get("avgPrice"),
            "amplitude": quote.get("amplitude"),
            "tradeValue": (quote.get("total") or {}).get("tradeValue"),
            "tradeVolume": (quote.get("total") or {}).get("tradeVolume"),
            "transaction": (quote.get("total") or {}).get("transaction"),
            "isClose": quote.get("isClose"),
            "limitUpPrice": ticker.get("limitUpPrice"),
            "limitDownPrice": ticker.get("limitDownPrice"),
            "canDayTrade": ticker.get("canDayTrade"),
            "isAttention": ticker.get("isAttention"),
            "isDisposition": ticker.get("isDisposition"),
            "industry": ticker.get("industry"),
            "bids": quote.get("bids"),
            "asks": quote.get("asks"),
        })

    rows.sort(key=lambda r: (r.get("changePercent") is None, -(r.get("changePercent") or 0)))

    out = {
        "fetched_at_tpe": now_tpe.isoformat(),
        "test_account": test_id,
        "market_date": rows[0].get("date") if rows else None,
        "symbols": rows,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
