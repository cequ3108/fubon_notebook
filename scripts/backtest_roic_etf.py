#!/usr/bin/env python3
"""
Backtest: Top-300 market-cap × ROIC>15% × market-cap weighted × monthly rebalance.
Past ~5 years. Compare vs 0050.
"""

from __future__ import annotations

import json
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from FinMind.data import DataLoader

warnings.filterwarnings("ignore")

OUT = Path("/workspace/scripts/uanalyze_output")
OUT.mkdir(parents=True, exist_ok=True)
API = "https://api.finmindtrade.com/api/v4/data"
CACHE = OUT / "bt_cache"
CACHE.mkdir(exist_ok=True)

START = "2020-01-01"
END = datetime.now().strftime("%Y-%m-%d")
ROIC_MIN = 0.15
TOP_N = 300
REPORT_LAG_DAYS = 45  # avoid look-ahead: only use FS ended >=45d before rebalance


def api_df(dataset: str, stock_id: str, start: str, end: str) -> pd.DataFrame:
    path = CACHE / f"{dataset}_{stock_id}_{start}_{end}.pkl"
    if path.exists():
        return pd.read_pickle(path)
    for attempt in range(3):
        try:
            r = requests.get(
                API,
                params={"dataset": dataset, "data_id": stock_id, "start_date": start, "end_date": end},
                timeout=45,
            )
            data = r.json().get("data", [])
            df = pd.DataFrame(data)
            df.to_pickle(path)
            time.sleep(0.08)
            return df
        except Exception:
            time.sleep(0.5)
    return pd.DataFrame()


def pivot_fs(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.pivot_table(index="date", columns="type", values="value", aggfunc="last")
    out.index = pd.to_datetime(out.index)
    return out.sort_index()


def load_universe() -> pd.DataFrame:
    """Candidate pool ≈ current top300 (covers most historical large caps)."""
    top = pd.read_csv(OUT / "top300_market_cap_v3.csv")
    dl = DataLoader()
    info = dl.taiwan_stock_info()
    info = info[info["type"].isin(["twse", "tpex"]) & info["stock_id"].str.match(r"^\d{4}$")]
    ids = set(top["stock_id"].astype(str))
    mega = {
        "2330", "2317", "2454", "2308", "2382", "2881", "2882", "2886", "2891", "2884",
        "2885", "2892", "2880", "2883", "2887", "2890", "3711", "2303", "2412", "3045",
        "4904", "6669", "3008", "2357", "3231", "2345", "3037", "2379", "3034", "2395",
        "2408", "6505", "1303", "1301", "1326", "2912", "2207", "5871", "2801", "2002",
        "2301", "2354", "2474", "4938", "2383", "3017", "2327", "3443", "3661", "6415",
        "1590", "2049", "3533", "3653", "5269", "6488", "8299", "5347", "3035", "2376",
        "2377", "2356", "3714", "6770", "6781", "8046", "2615", "2603", "2609", "2888",
        "1216", "1101", "1102", "1402", "1476", "1504", "1605", "1717", "1722", "1802",
        "1904", "1907", "2002", "2105", "2201", "2227", "2231", "2324", "2353", "2409",
        "2449", "2451", "2471", "2492", "2498", "2610", "2618", "2633", "2809", "2812",
        "2820", "2834", "2867", "2889", "2897", "2915", "3005", "3023", "3036", "3044",
        "3189", "3406", "3450", "3481", "3529", "3596", "3702", "3715", "4919", "4958",
        "5269", "5285", "5434", "5471", "5522", "5876", "5904", "6176", "6239", "6271",
        "6409", "6414", "6456", "6531", "6550", "6579", "6592", "6643", "6691", "6706",
        "6756", "6805", "6861", "8016", "8021", "8046", "8069", "8112", "8210", "8454",
    }
    ids |= mega
    meta = info[info["stock_id"].isin(ids)][["stock_id", "stock_name", "industry_category", "type"]].drop_duplicates("stock_id")
    # if some mega missing from info, still keep from top csv
    missing = ids - set(meta["stock_id"])
    extra = top[top["stock_id"].astype(str).isin(missing)][["stock_id", "name", "industry"]].rename(
        columns={"name": "stock_name", "industry": "industry_category"}
    )
    if len(extra):
        extra["type"] = "twse"
        meta = pd.concat([meta, extra], ignore_index=True).drop_duplicates("stock_id")
    return meta.reset_index(drop=True)


def get_price_panel(stock_ids: list[str], start: str, end: str) -> pd.DataFrame:
    """Wide close price panel via yfinance (fast batch)."""
    import yfinance as yf

    cache_path = CACHE / f"price_panel_yf_{start}_{end}_{len(stock_ids)}.pkl"
    if cache_path.exists():
        return pd.read_pickle(cache_path)

    # map to yahoo tickers using FinMind info type
    dl = DataLoader()
    info = dl.taiwan_stock_info()
    info = info[info["stock_id"].isin(stock_ids)]
    type_map = dict(zip(info["stock_id"].astype(str), info["type"]))

    tickers = []
    rev = {}
    for sid in stock_ids:
        t = type_map.get(sid, "twse")
        y = f"{sid}.TW" if t == "twse" else f"{sid}.TWO"
        tickers.append(y)
        rev[y] = sid

    frames = []
    chunk = 80
    for i in range(0, len(tickers), chunk):
        part = tickers[i : i + chunk]
        try:
            data = yf.download(
                part,
                start=start,
                end=(pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                group_by="ticker",
                auto_adjust=True,
                threads=True,
                progress=False,
            )
            if len(part) == 1:
                close = data["Close"].rename(rev[part[0]])
                frames.append(close.to_frame())
            else:
                # MultiIndex columns
                for y in part:
                    try:
                        if isinstance(data.columns, pd.MultiIndex):
                            s = data[(y, "Close")] if (y, "Close") in data.columns else data[y]["Close"]
                        else:
                            continue
                        s.name = rev[y]
                        frames.append(s)
                    except Exception:
                        continue
        except Exception as exc:
            print("yf chunk err", exc, flush=True)
        print(f"  prices {min(i+chunk,len(tickers))}/{len(tickers)}", flush=True)
        time.sleep(0.3)

    if not frames:
        raise RuntimeError("No price data")
    panel = pd.concat(frames, axis=1).sort_index()
    panel = panel.loc[:, ~panel.columns.duplicated()]
    panel.to_pickle(cache_path)
    return panel


def get_shares_history(stock_ids: list[str], start: str, end: str) -> pd.DataFrame:
    """OrdinaryShare / 10 ≈ shares outstanding (par 10). Forward-filled daily."""
    cache_path = CACHE / f"shares_panel_{start}_{end}_{len(stock_ids)}.pkl"
    if cache_path.exists():
        return pd.read_pickle(cache_path)

    # fetch a bit earlier for ffill
    start_fs = (pd.Timestamp(start) - pd.DateOffset(years=2)).strftime("%Y-%m-%d")
    frames = []
    for i, sid in enumerate(stock_ids):
        bal = pivot_fs(api_df("TaiwanStockBalanceSheet", sid, start_fs, end))
        if bal.empty or "OrdinaryShare" not in bal.columns:
            continue
        shares = (bal["OrdinaryShare"] / 10.0).dropna()
        shares.name = sid
        frames.append(shares)
        if i % 50 == 0:
            print(f"  shares {i}/{len(stock_ids)}", flush=True)
    if not frames:
        raise RuntimeError("No shares data")
    # asof onto daily calendar later
    panel = pd.concat(frames, axis=1).sort_index()
    panel.to_pickle(cache_path)
    return panel


def get_roic_asof(stock_ids: list[str], start: str, end: str) -> pd.DataFrame:
    """
    Build monthly ROIC series available at each month-start (with report lag).
    Returns DataFrame indexed by rebalance date, columns=stock_id.
    """
    cache_path = CACHE / f"roic_asof_{start}_{end}_{len(stock_ids)}.pkl"
    if cache_path.exists():
        return pd.read_pickle(cache_path)

    start_fs = (pd.Timestamp(start) - pd.DateOffset(years=6)).strftime("%Y-%m-%d")
    # For each stock, compute quarterly point-in-time ROIC (TTM)
    stock_roic_curves = {}
    for i, sid in enumerate(stock_ids):
        inc = pivot_fs(api_df("TaiwanStockFinancialStatements", sid, start_fs, end))
        bal = pivot_fs(api_df("TaiwanStockBalanceSheet", sid, start_fs, end))
        if inc.empty or bal.empty or "IncomeAfterTaxes" not in inc.columns:
            continue
        eq = bal.get("EquityAttributableToOwnersOfParent")
        if eq is None:
            eq = bal.get("Equity")
        if eq is None:
            continue
        ni = inc["IncomeAfterTaxes"].dropna()
        eq = eq.dropna()
        ltd = bal.get("LongtermBorrowings", pd.Series(0, index=bal.index)).fillna(0) + bal.get(
            "BondsPayable", pd.Series(0, index=bal.index)
        ).fillna(0)

        # quarterly TTM ROIC on each quarter-end, available_date = q_end + lag
        q_dates = sorted(set(ni.index) & set(eq.index))
        rows = []
        for qd in q_dates:
            # TTM = last 4 quarters NI ending at qd
            ni_hist = ni.loc[:qd].tail(4)
            if len(ni_hist) < 4:
                continue
            ttm_ni = float(ni_hist.sum())
            # invested capital average: current and 4Q ago
            eq_hist = eq.loc[:qd].tail(5)
            if len(eq_hist) < 2:
                continue
            ic_now = float(eq.loc[qd]) + float(ltd.loc[qd] if qd in ltd.index else 0)
            qd_prev = eq_hist.index[0]
            ic_prev = float(eq.loc[qd_prev]) + float(ltd.loc[qd_prev] if qd_prev in ltd.index else 0)
            avg_ic = (ic_now + ic_prev) / 2.0
            if avg_ic <= 0:
                continue
            avail = qd + pd.Timedelta(days=REPORT_LAG_DAYS)
            rows.append((avail, ttm_ni / avg_ic))
        if rows:
            s = pd.Series({a: v for a, v in rows}).sort_index()
            # keep last observation per day
            s = s[~s.index.duplicated(keep="last")]
            stock_roic_curves[sid] = s
        if i % 40 == 0:
            print(f"  roic {i}/{len(stock_ids)} ok={len(stock_roic_curves)}", flush=True)

    # month starts
    months = pd.date_range(start, end, freq="MS")
    data = {}
    for sid, curve in stock_roic_curves.items():
        vals = []
        for m in months:
            hist = curve.loc[:m]
            vals.append(float(hist.iloc[-1]) if len(hist) else np.nan)
        data[sid] = vals
    roic_df = pd.DataFrame(data, index=months)
    roic_df.to_pickle(cache_path)
    return roic_df


def month_ends_in_range(price: pd.DataFrame) -> pd.DatetimeIndex:
    # last trading day of each month in price index
    g = price.groupby([price.index.year, price.index.month]).tail(1).index
    return pd.DatetimeIndex(g)


def run_backtest(price: pd.DataFrame, shares: pd.DataFrame, roic_m: pd.DataFrame) -> dict:
    # align shares to daily via ffill
    shares_daily = shares.reindex(price.index.union(shares.index)).sort_index().ffill().reindex(price.index)

    # rebalance dates = first trading day of each month
    first_days = []
    for m in pd.date_range(price.index.min(), price.index.max(), freq="MS"):
        days = price.index[(price.index >= m) & (price.index < m + pd.offsets.MonthBegin(1))]
        if len(days):
            first_days.append(days[0])
    first_days = pd.DatetimeIndex(first_days)
    # need full month ahead → drop last incomplete if needed
    first_days = first_days[(first_days >= pd.Timestamp(START)) & (first_days <= pd.Timestamp(END))]

    port_rets = []
    holdings_log = []
    weights_prev = None
    turnover_list = []

    for i, reb in enumerate(first_days[:-1]):
        next_reb = first_days[i + 1]
        # market cap at prior close (reb itself open uses previous close ≈ reb close of day before; use reb prices)
        px = price.loc[reb]
        sh = shares_daily.loc[reb]
        mcap = px * sh
        mcap = mcap.replace([np.inf, -np.inf], np.nan).dropna()
        mcap = mcap[mcap > 0]
        if len(mcap) < 50:
            continue

        top = mcap.nlargest(TOP_N)
        # ROIC as of this month-start
        month_key = pd.Timestamp(reb.year, reb.month, 1)
        if month_key not in roic_m.index:
            # nearest prior
            prior = roic_m.index[roic_m.index <= month_key]
            if not len(prior):
                continue
            month_key = prior[-1]
        roic = roic_m.loc[month_key]
        eligible = [s for s in top.index if s in roic.index and pd.notna(roic[s]) and roic[s] >= ROIC_MIN]
        if len(eligible) < 5:
            # fallback: hold cash (0 return) or keep previous — use cash
            holdings_log.append({"date": str(reb.date()), "n": 0, "names": []})
            # zero return for period
            period_idx = price.index[(price.index >= reb) & (price.index < next_reb)]
            for d in period_idx[1:]:
                port_rets.append((d, 0.0))
            continue

        caps = top.loc[eligible]
        w = caps / caps.sum()

        if weights_prev is not None:
            # turnover = 0.5 * sum |w_new - w_old|
            all_ids = set(w.index) | set(weights_prev.index)
            turn = 0.5 * sum(abs(w.get(s, 0) - weights_prev.get(s, 0)) for s in all_ids)
            turnover_list.append(turn)
        weights_prev = w.copy()

        holdings_log.append(
            {
                "date": str(reb.date()),
                "n": int(len(w)),
                "top5": [
                    {"id": sid, "w": round(float(w[sid]), 4), "roic": round(float(roic[sid]) * 100, 1)}
                    for sid in w.nlargest(5).index
                ],
            }
        )

        # daily returns within period using fixed weights from reb close to next_reb
        period_idx = price.index[(price.index >= reb) & (price.index <= next_reb)]
        if len(period_idx) < 2:
            continue
        sub = price.loc[period_idx, w.index].ffill()
        # if stock missing, drop weight and renormalize
        valid = sub.columns[sub.iloc[0].notna()]
        ww = w.loc[valid]
        ww = ww / ww.sum()
        sub = sub[valid]
        daily = sub.pct_change().fillna(0.0)
        for d, row in daily.iloc[1:].iterrows():
            r = float((row * ww).sum())
            # clip extreme bad ticks
            if abs(r) > 0.25:
                r = float(np.clip(r, -0.25, 0.25))
            port_rets.append((d, r))

    rets = pd.Series({d: r for d, r in port_rets}).sort_index()
    rets = rets[~rets.index.duplicated(keep="last")]
    equity = (1 + rets).cumprod()

    # benchmark 0050
    b = api_df("TaiwanStockPrice", "0050", START, END)
    b["date"] = pd.to_datetime(b["date"])
    bench = b.set_index("date")["close"].astype(float).sort_index()
    bench = bench.reindex(rets.index).ffill()
    bench_rets = bench.pct_change().fillna(0.0)
    bench_eq = (1 + bench_rets).cumprod()

    def stats(r: pd.Series, eq: pd.Series) -> dict:
        if len(r) < 10:
            return {}
        years = (r.index[-1] - r.index[0]).days / 365.25
        total = float(eq.iloc[-1] / eq.iloc[0] - 1) if eq.iloc[0] else np.nan
        cagr = float(eq.iloc[-1] ** (1 / years) - 1) if years > 0 else np.nan
        vol = float(r.std() * np.sqrt(252))
        sharpe = float((r.mean() * 252) / (r.std() * np.sqrt(252))) if r.std() > 0 else np.nan
        dd = float((eq / eq.cummax() - 1).min())
        calmar = float(cagr / abs(dd)) if dd < 0 else np.nan
        return {
            "start": str(r.index[0].date()),
            "end": str(r.index[-1].date()),
            "years": round(years, 2),
            "total_return": round(total * 100, 2),
            "cagr": round(cagr * 100, 2),
            "ann_vol": round(vol * 100, 2),
            "sharpe": round(sharpe, 2),
            "max_drawdown": round(dd * 100, 2),
            "calmar": round(calmar, 2) if calmar == calmar else None,
        }

    # annual returns
    ann = []
    for y, g in rets.groupby(rets.index.year):
        eq_y = (1 + g).prod() - 1
        b_y = (1 + bench_rets.reindex(g.index).fillna(0)).prod() - 1
        ann.append({"year": int(y), "strategy": round(float(eq_y) * 100, 2), "0050": round(float(b_y) * 100, 2)})

    # excess
    excess = rets - bench_rets.reindex(rets.index).fillna(0)
    te = float(excess.std() * np.sqrt(252))
    ir = float(excess.mean() * 252 / (excess.std() * np.sqrt(252))) if excess.std() > 0 else np.nan

    result = {
        "strategy": stats(rets, equity),
        "benchmark_0050": stats(bench_rets.reindex(rets.index).fillna(0), bench_eq.reindex(rets.index).ffill()),
        "avg_holdings": round(float(np.mean([h["n"] for h in holdings_log if h["n"] > 0])), 1),
        "avg_monthly_turnover": round(float(np.mean(turnover_list)) * 100, 2) if turnover_list else None,
        "info_ratio": round(ir, 2) if ir == ir else None,
        "tracking_error": round(te * 100, 2) if te == te else None,
        "annual_returns": ann,
        "holdings_sample": holdings_log[::6][:12],  # every ~6 months
        "equity_curve": {
            "dates": [str(d.date()) for d in equity.index[::5]],
            "strategy": [round(float(x), 4) for x in equity.iloc[::5]],
            "0050": [round(float(x), 4) for x in bench_eq.reindex(equity.index).ffill().iloc[::5]],
        },
    }

    # save series
    out_df = pd.DataFrame(
        {
            "strategy_ret": rets,
            "strategy_nav": equity,
            "bench_ret": bench_rets.reindex(rets.index).fillna(0),
            "bench_nav": bench_eq.reindex(rets.index).ffill(),
        }
    )
    out_df.to_csv(OUT / "backtest_daily.csv")
    pd.DataFrame(holdings_log).to_csv(OUT / "backtest_holdings_log.csv", index=False)
    return result


def main():
    print("1) universe", flush=True)
    meta = load_universe()
    ids = meta["stock_id"].astype(str).tolist()
    print(f"   candidates {len(ids)}", flush=True)

    print("2) prices", flush=True)
    price = get_price_panel(ids, START, END)
    # keep stocks with enough history
    price = price.dropna(axis=1, thresh=int(len(price) * 0.4))
    ids = [c for c in price.columns]
    print(f"   price cols {len(ids)} days {len(price)}", flush=True)

    print("3) shares", flush=True)
    shares = get_shares_history(ids, START, END)
    shares = shares.reindex(columns=[c for c in ids if c in shares.columns])
    print(f"   shares cols {shares.shape}", flush=True)

    print("4) ROIC as-of", flush=True)
    common = [c for c in ids if c in shares.columns]
    roic_m = get_roic_asof(common, START, END)
    print(f"   roic shape {roic_m.shape}", flush=True)

    print("5) backtest", flush=True)
    price = price[common]
    shares = shares[common]
    result = run_backtest(price, shares, roic_m)
    Path(OUT / "backtest_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: result[k] for k in result if k != "equity_curve"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
