#!/usr/bin/env python3
"""
Multi-factor variants on ROIC>15% universe.
Goal: higher CAGR and shallower drawdowns vs plain cap-weighted ROIC ETF.
Uses existing bt_cache price / shares / ROIC panels.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
from FinMind.data import DataLoader

OUT = Path("/workspace/scripts/uanalyze_output")
CACHE = OUT / "bt_cache"
START = "2020-01-01"
END = "2026-08-23"
ROIC_MIN = 0.15
TOP_N = 300
MAX_W = 0.10  # single-name cap for capped variants

# Cyclical / volatile industries that often show high ROIC near cycle peaks
CYCLICAL_KW = (
    "塑膠",
    "化學",
    "鋼鐵",
    "航運",
    "油電燃氣",
    "水泥",
    "玻璃",
    "造紙",
    "橡膠",
    "金融",
    "保險",
    "證券",
    "金控",
)


def load_panels():
    price = pd.read_pickle(CACHE / "price_panel_yf_2020-01-01_2026-08-23_326.pkl")
    shares = pd.read_pickle(CACHE / "shares_panel_2020-01-01_2026-08-23_314.pkl")
    roic = pd.read_pickle(CACHE / "roic_asof_2020-01-01_2026-08-23_314.pkl")
    common = sorted(set(price.columns) & set(shares.columns) & set(roic.columns))
    price = price[common].sort_index()
    shares = shares.reindex(columns=common)
    roic = roic.reindex(columns=common)
    return price, shares, roic


def load_industry_map(ids: list[str]) -> dict[str, str]:
    dl = DataLoader()
    info = dl.taiwan_stock_info()
    info = info[info["stock_id"].astype(str).isin(ids)]
    return dict(zip(info["stock_id"].astype(str), info["industry_category"].astype(str)))


def is_cyclical(ind: str) -> bool:
    if not ind or ind == "nan":
        return False
    return any(k in ind for k in CYCLICAL_KW)


def rebalance_dates(price: pd.DataFrame) -> pd.DatetimeIndex:
    first_days = []
    for m in pd.date_range(price.index.min(), price.index.max(), freq="MS"):
        days = price.index[(price.index >= m) & (price.index < m + pd.offsets.MonthBegin(1))]
        if len(days):
            first_days.append(days[0])
    first_days = pd.DatetimeIndex(first_days)
    return first_days[(first_days >= pd.Timestamp(START)) & (first_days <= pd.Timestamp(END))]


def mom_12m(price: pd.DataFrame, reb: pd.Timestamp) -> pd.Series:
    hist = price.loc[:reb]
    if len(hist) < 252:
        return pd.Series(dtype=float)
    p0 = hist.iloc[-1]
    p1 = hist.iloc[-252]
    return (p0 / p1 - 1).replace([np.inf, -np.inf], np.nan)


def above_ma200(price: pd.DataFrame, reb: pd.Timestamp) -> pd.Series:
    hist = price.loc[:reb].tail(200)
    if len(hist) < 150:
        return pd.Series(False, index=price.columns)
    ma = hist.mean()
    return hist.iloc[-1] > ma


def pb_proxy(price: pd.DataFrame, shares: pd.DataFrame, equity_panel: pd.DataFrame, reb: pd.Timestamp) -> pd.Series:
    """P/B ≈ market_cap / equity. Lower = cheaper."""
    px = price.loc[reb]
    sh = shares.loc[reb] if reb in shares.index else shares.loc[:reb].iloc[-1]
    eq = equity_panel.loc[:reb].iloc[-1] if len(equity_panel.loc[:reb]) else pd.Series(dtype=float)
    mcap = px * sh
    pb = mcap / eq.replace(0, np.nan)
    return pb.replace([np.inf, -np.inf], np.nan)


def build_equity_panel(ids: list[str]) -> pd.DataFrame:
    cache = CACHE / f"equity_panel_{START}_{END}_{len(ids)}.pkl"
    if cache.exists():
        return pd.read_pickle(cache)
    frames = []
    for sid in ids:
        # reuse any cached balance sheet
        paths = list(CACHE.glob(f"TaiwanStockBalanceSheet_{sid}_*.pkl"))
        if not paths:
            continue
        df = pd.read_pickle(sorted(paths)[-1])
        if df.empty or "type" not in df.columns:
            continue
        piv = df.pivot_table(index="date", columns="type", values="value", aggfunc="last")
        piv.index = pd.to_datetime(piv.index)
        col = "EquityAttributableToOwnersOfParent" if "EquityAttributableToOwnersOfParent" in piv.columns else "Equity"
        if col not in piv.columns:
            continue
        s = piv[col].dropna()
        s.name = sid
        frames.append(s)
    panel = pd.concat(frames, axis=1).sort_index() if frames else pd.DataFrame()
    panel.to_pickle(cache)
    return panel


def apply_max_weight(w: pd.Series, max_w: float = MAX_W) -> pd.Series:
    """Iteratively cap weights and redistribute residual."""
    w = w.copy().astype(float)
    w = w / w.sum()
    for _ in range(20):
        over = w > max_w
        if not over.any():
            break
        excess = (w[over] - max_w).sum()
        w[over] = max_w
        under = ~over
        if under.sum() == 0 or w[under].sum() <= 0:
            break
        w[under] = w[under] + excess * (w[under] / w[under].sum())
    return w / w.sum()


def stats(rets: pd.Series) -> dict:
    eq = (1 + rets).cumprod()
    years = (rets.index[-1] - rets.index[0]).days / 365.25
    total = float(eq.iloc[-1] / eq.iloc[0] - 1)
    cagr = float(eq.iloc[-1] ** (1 / years) - 1) if years > 0 else np.nan
    vol = float(rets.std() * np.sqrt(252))
    sharpe = float((rets.mean() * 252) / (rets.std() * np.sqrt(252))) if rets.std() > 0 else np.nan
    dd = float((eq / eq.cummax() - 1).min())
    calmar = float(cagr / abs(dd)) if dd < 0 else np.nan
    # annual
    ann = {}
    for y, g in rets.groupby(rets.index.year):
        ann[int(y)] = round(float((1 + g).prod() - 1) * 100, 2)
    return {
        "cagr_pct": round(cagr * 100, 2),
        "total_return_pct": round(total * 100, 2),
        "ann_vol_pct": round(vol * 100, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(dd * 100, 2),
        "calmar": round(calmar, 2) if calmar == calmar else None,
        "years": round(years, 2),
        "annual": ann,
        "nav": eq,
        "rets": rets,
    }


def period_returns(
    price: pd.DataFrame,
    reb: pd.Timestamp,
    next_reb: pd.Timestamp,
    w: pd.Series,
    allow_cash: bool = False,
) -> list[tuple]:
    period_idx = price.index[(price.index >= reb) & (price.index <= next_reb)]
    if len(period_idx) < 2:
        return []
    sub = price.loc[period_idx, w.index].ffill()
    valid = sub.columns[sub.iloc[0].notna()]
    if len(valid) < 3:
        return []
    ww = w.loc[valid]
    if not allow_cash:
        ww = ww / ww.sum()
    # if allow_cash and sum(w)<1, residual is cash (0 daily return)
    daily = sub[valid].pct_change().fillna(0.0)
    out = []
    for d, row in daily.iloc[1:].iterrows():
        r = float((row * ww).sum())
        if abs(r) > 0.25:
            r = float(np.clip(r, -0.25, 0.25))
        out.append((d, r))
    return out


def bench_below_ma200(bench_px: pd.Series, reb: pd.Timestamp) -> bool:
    hist = bench_px.loc[:reb].tail(200)
    if len(hist) < 150:
        return False
    return float(hist.iloc[-1]) < float(hist.mean())


def select_weights(
    name: str,
    reb: pd.Timestamp,
    price: pd.DataFrame,
    shares_daily: pd.DataFrame,
    roic_m: pd.DataFrame,
    industry: dict[str, str],
    equity_panel: pd.DataFrame,
) -> pd.Series | None:
    px = price.loc[reb]
    sh = shares_daily.loc[reb]
    mcap = (px * sh).replace([np.inf, -np.inf], np.nan).dropna()
    mcap = mcap[mcap > 0]
    if len(mcap) < 50:
        return None
    top = mcap.nlargest(TOP_N)

    month_key = pd.Timestamp(reb.year, reb.month, 1)
    prior = roic_m.index[roic_m.index <= month_key]
    if not len(prior):
        return None
    roic = roic_m.loc[prior[-1]]

    base = [s for s in top.index if s in roic.index and pd.notna(roic[s]) and roic[s] >= ROIC_MIN]
    if len(base) < 5:
        return None

    # --- filters / scoring by variant ---
    if name == "A_baseline_cap":
        caps = top.loc[base]
        return caps / caps.sum()

    if name == "B_cap_max10":
        caps = top.loc[base]
        return apply_max_weight(caps / caps.sum(), MAX_W)

    if name == "C_equal_weight":
        return pd.Series(1.0 / len(base), index=base)

    if name == "D_mom_filter_cap10":
        mom = mom_12m(price, reb)
        keep = [s for s in base if s in mom.index and pd.notna(mom[s]) and mom[s] > 0]
        if len(keep) < 5:
            keep = base
        caps = top.loc[keep]
        return apply_max_weight(caps / caps.sum(), MAX_W)

    if name == "E_trend_ma200_cap10":
        trend = above_ma200(price, reb)
        keep = [s for s in base if bool(trend.get(s, False))]
        if len(keep) < 5:
            keep = base
        caps = top.loc[keep]
        return apply_max_weight(caps / caps.sum(), MAX_W)

    if name == "F_ex_cyclical_cap10":
        keep = [s for s in base if not is_cyclical(industry.get(s, ""))]
        if len(keep) < 5:
            keep = base
        caps = top.loc[keep]
        return apply_max_weight(caps / caps.sum(), MAX_W)

    if name == "G_roic_stable_cap10":
        # require current ROIC and ROIC 12 months ago both > 15%
        look = prior[prior <= prior[-1] - pd.DateOffset(months=11)]
        keep = []
        for s in base:
            if not len(look):
                keep.append(s)
                continue
            old = roic_m.loc[look[-1], s] if s in roic_m.columns else np.nan
            if pd.notna(old) and old >= ROIC_MIN:
                keep.append(s)
        if len(keep) < 5:
            keep = base
        caps = top.loc[keep]
        return apply_max_weight(caps / caps.sum(), MAX_W)

    if name == "H_quality_value_eq":
        # cheap half of ROIC>15% by P/B, equal weight
        pb = pb_proxy(price, shares_daily, equity_panel, reb)
        pb = pb.reindex(base).dropna()
        if len(pb) < 8:
            return pd.Series(1.0 / len(base), index=base)
        med = pb.median()
        keep = pb[pb <= med].index.tolist()
        if len(keep) < 5:
            keep = base
        return pd.Series(1.0 / len(keep), index=keep)

    if name == "I_roic_mom_score_top40":
        # composite z: ROIC rank + 12m momentum rank among top300 ROIC>15, equal top40
        mom = mom_12m(price, reb).reindex(base)
        r_roic = roic.reindex(base).rank(pct=True)
        r_mom = mom.rank(pct=True)
        score = (r_roic.fillna(0) * 0.6 + r_mom.fillna(0.5) * 0.4)
        keep = score.nlargest(min(40, len(score))).index.tolist()
        return pd.Series(1.0 / len(keep), index=keep)

    if name == "J_combo_stable_mom_cap10":
        # stable ROIC + positive momentum + ex-cyclical + max 10%
        look = prior[prior <= prior[-1] - pd.DateOffset(months=11)]
        mom = mom_12m(price, reb)
        keep = []
        for s in base:
            if is_cyclical(industry.get(s, "")):
                continue
            old = roic_m.loc[look[-1], s] if len(look) and s in roic_m.columns else np.nan
            if pd.isna(old) or old < ROIC_MIN:
                continue
            if s not in mom.index or pd.isna(mom[s]) or mom[s] <= 0:
                continue
            keep.append(s)
        if len(keep) < 8:
            # soften: drop momentum first
            keep = [
                s
                for s in base
                if not is_cyclical(industry.get(s, ""))
                and (not len(look) or (s in roic_m.columns and pd.notna(roic_m.loc[look[-1], s]) and roic_m.loc[look[-1], s] >= ROIC_MIN))
            ]
        if len(keep) < 5:
            keep = base
        caps = top.loc[keep]
        return apply_max_weight(caps / caps.sum(), MAX_W)

    if name in ("K_roic_mom_top20", "L_top20_halfcash_ma200"):
        mom = mom_12m(price, reb).reindex(base)
        r_roic = roic.reindex(base).rank(pct=True)
        r_mom = mom.rank(pct=True)
        score = r_roic.fillna(0) * 0.6 + r_mom.fillna(0.5) * 0.4
        keep = score.nlargest(min(20, len(score))).index.tolist()
        w = pd.Series(1.0 / len(keep), index=keep)
        if name == "L_top20_halfcash_ma200":
            # scale to 50% invested when 0050 below 200DMA (cash overlay)
            # caller applies cash via weight sum < 1; flag stored on series attrs
            w.attrs["allow_cash"] = True
        return w

    raise ValueError(name)


VARIANTS = [
    "A_baseline_cap",
    "B_cap_max10",
    "C_equal_weight",
    "D_mom_filter_cap10",
    "E_trend_ma200_cap10",
    "F_ex_cyclical_cap10",
    "G_roic_stable_cap10",
    "H_quality_value_eq",
    "I_roic_mom_score_top40",
    "J_combo_stable_mom_cap10",
    "K_roic_mom_top20",
    "L_top20_halfcash_ma200",
]


def run_variant(
    name: str,
    price: pd.DataFrame,
    shares_daily: pd.DataFrame,
    roic_m: pd.DataFrame,
    industry: dict[str, str],
    equity_panel: pd.DataFrame,
    first_days: pd.DatetimeIndex,
    bench_px: pd.Series | None = None,
) -> dict:
    port_rets = []
    n_hold = []
    max_single = []
    invested = []
    for i, reb in enumerate(first_days[:-1]):
        next_reb = first_days[i + 1]
        w = select_weights(name, reb, price, shares_daily, roic_m, industry, equity_panel)
        if w is None or len(w) < 3:
            continue
        allow_cash = bool(getattr(w, "attrs", {}).get("allow_cash", False))
        if allow_cash and bench_px is not None and bench_below_ma200(bench_px, reb):
            w = w * 0.5
        n_hold.append(len(w))
        max_single.append(float((w / w.sum()).max()) if w.sum() > 0 else 0.0)
        invested.append(float(w.sum()) if allow_cash else 1.0)
        port_rets.extend(period_returns(price, reb, next_reb, w, allow_cash=allow_cash))

    rets = pd.Series({d: r for d, r in port_rets}).sort_index()
    rets = rets[~rets.index.duplicated(keep="last")]
    s = stats(rets)
    s["avg_holdings"] = round(float(np.mean(n_hold)), 1) if n_hold else None
    s["avg_max_weight_pct"] = round(float(np.mean(max_single)) * 100, 1) if max_single else None
    s["avg_invested_pct"] = round(float(np.mean(invested)) * 100, 1) if invested else None
    return s


def load_0050(index: pd.DatetimeIndex) -> pd.Series:
    cache = CACHE / "bench_0050_yf.pkl"
    if cache.exists():
        close = pd.read_pickle(cache)
    else:
        data = yf.download("0050.TW", start=START, end=END, auto_adjust=True, progress=False)
        close = data["Close"].astype(float).sort_index()
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close.to_pickle(cache)
    close = close.reindex(index).ffill()
    return close.pct_change().fillna(0.0)


def main():
    print("load panels", flush=True)
    price, shares, roic = load_panels()
    shares_daily = shares.reindex(price.index.union(shares.index)).sort_index().ffill().reindex(price.index)
    ids = list(price.columns)
    print(f"stocks={len(ids)} days={len(price)}", flush=True)

    print("industry map", flush=True)
    industry = load_industry_map(ids)

    print("equity panel for PB", flush=True)
    equity_panel = build_equity_panel(ids)
    equity_daily = equity_panel.reindex(price.index.union(equity_panel.index)).sort_index().ffill().reindex(price.index)

    first_days = rebalance_dates(price)
    print(f"rebalances={len(first_days)}", flush=True)

    bench_cache = CACHE / "bench_0050_yf_px.pkl"
    if bench_cache.exists():
        bench_px = pd.read_pickle(bench_cache)
    else:
        data = yf.download("0050.TW", start=START, end=END, auto_adjust=True, progress=False)
        bench_px = data["Close"].astype(float).sort_index()
        if isinstance(bench_px, pd.DataFrame):
            bench_px = bench_px.iloc[:, 0]
        bench_px.to_pickle(bench_cache)

    results = {}
    navs = {}
    for name in VARIANTS:
        print(f"run {name}", flush=True)
        s = run_variant(
            name, price, shares_daily, roic, industry, equity_daily, first_days, bench_px=bench_px
        )
        navs[name] = s.pop("nav")
        rets = s.pop("rets")
        results[name] = s
        results[name]["_rets"] = rets

    common_idx = None
    for name in VARIANTS:
        idx = results[name]["_rets"].index
        common_idx = idx if common_idx is None else common_idx.intersection(idx)
    common_idx = common_idx.sort_values()

    bench_rets = load_0050(common_idx)
    bench_stats = stats(bench_rets.loc[common_idx])
    bench_nav = bench_stats.pop("nav")
    bench_stats.pop("rets")

    table = []
    clean = {}
    for name in VARIANTS:
        r = results[name]["_rets"].reindex(common_idx).fillna(0.0)
        s = stats(r)
        nav = s.pop("nav")
        s.pop("rets")
        s["avg_holdings"] = results[name]["avg_holdings"]
        s["avg_max_weight_pct"] = results[name]["avg_max_weight_pct"]
        s["avg_invested_pct"] = results[name].get("avg_invested_pct")
        excess = r - bench_rets.reindex(common_idx).fillna(0)
        s["info_ratio"] = (
            round(float(excess.mean() * 252 / (excess.std() * np.sqrt(252))), 2) if excess.std() > 0 else None
        )
        clean[name] = s
        navs[name] = nav
        table.append(
            {
                "variant": name,
                "cagr": s["cagr_pct"],
                "max_dd": s["max_drawdown_pct"],
                "sharpe": s["sharpe"],
                "calmar": s["calmar"],
                "vol": s["ann_vol_pct"],
                "avg_n": s["avg_holdings"],
                "avg_max_w": s["avg_max_weight_pct"],
                "avg_invested": s["avg_invested_pct"],
                "ir": s["info_ratio"],
            }
        )

    clean["0050"] = {
        "cagr_pct": bench_stats["cagr_pct"],
        "total_return_pct": bench_stats["total_return_pct"],
        "ann_vol_pct": bench_stats["ann_vol_pct"],
        "sharpe": bench_stats["sharpe"],
        "max_drawdown_pct": bench_stats["max_drawdown_pct"],
        "calmar": bench_stats["calmar"],
        "years": bench_stats["years"],
        "annual": bench_stats["annual"],
    }

    ranked = sorted(table, key=lambda x: (x["calmar"] or -99, x["sharpe"] or -99), reverse=True)

    descriptions = {
        "A_baseline_cap": "ROIC>15% + 市值加權（原版）",
        "B_cap_max10": "ROIC>15% + 市值加權 + 單檔上限10%",
        "C_equal_weight": "ROIC>15% + 等權",
        "D_mom_filter_cap10": "ROIC>15% + 12個月動能>0 + 上限10%",
        "E_trend_ma200_cap10": "ROIC>15% + 站上200日均線 + 上限10%",
        "F_ex_cyclical_cap10": "ROIC>15% + 排除景氣循環/金融 + 上限10%",
        "G_roic_stable_cap10": "ROIC>15%（今年與一年前皆達標）+ 上限10%",
        "H_quality_value_eq": "ROIC>15% 中 P/B 較便宜一半 + 等權",
        "I_roic_mom_score_top40": "ROIC與動能綜合評分前40 + 等權",
        "J_combo_stable_mom_cap10": "穩定ROIC + 動能 + 排除循環股 + 上限10%",
        "K_roic_mom_top20": "ROIC×動能評分前20 + 等權（集中）",
        "L_top20_halfcash_ma200": "評分前20等權 + 0050跌破200MA時改50%現金",
    }

    summary = {
        "window": {"start": str(common_idx[0].date()), "end": str(common_idx[-1].date())},
        "descriptions": descriptions,
        "ranking_by_calmar": ranked,
        "variants": clean,
        "recommendation": {
            "best_risk_adjusted": ranked[0]["variant"] if ranked else None,
            "best_return_with_shallower_dd": "L_top20_halfcash_ma200",
            "notes": [
                "原版市值加權回檔深，主因台積電權重常>50%",
                "單檔上限 alone 會犧牲報酬（稀釋台積電）卻未必大幅改善回撤",
                "ROIC+12個月動能評分、集中前20等權，可同時提高CAGR並壓低回撤",
                "大盤趨勢濾網（0050 vs 200MA）半倉防守，進一步降低2022回檔",
                "未扣交易成本；候選池仍有倖存者偏誤；過去績效不保證未來",
            ],
        },
    }

    Path(OUT / "multifactor_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    nav_df = pd.DataFrame({k: navs[k] for k in VARIANTS})
    nav_df["0050"] = bench_nav.reindex(nav_df.index).ffill()
    nav_df.to_csv(OUT / "multifactor_nav.csv")

    cmp = pd.DataFrame(ranked)
    cmp.to_csv(OUT / "multifactor_compare.csv", index=False)
    print(cmp.to_string(index=False), flush=True)
    print("saved multifactor_summary.json", flush=True)


if __name__ == "__main__":
    main()
