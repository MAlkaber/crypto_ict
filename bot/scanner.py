"""Build the tradeable universe, apply the ethical screen, rank by momentum."""
from __future__ import annotations

import time

from . import binance
from .indicators import compute_features, klines_to_df
from .screener import Screener
from .structure import compute_ict, compute_structure


def build_universe(cfg) -> tuple[list[dict], dict[str, str]]:
    """Return spot USDT pairs that are actually trading, plus base->name map."""
    info = binance.exchange_info()
    quote = cfg.base_currency
    out, names = [], {}
    for s in info["symbols"]:
        if s["quoteAsset"] != quote or s["status"] != "TRADING":
            continue
        if not s.get("isSpotTradingAllowed"):
            continue
        base = s["baseAsset"]
        if binance.is_leveraged_token(base):
            continue
        out.append({
            "symbol": s["symbol"],
            "base": base,
            "filters": binance.symbol_filters(s),
        })
        names[base] = base  # Binance gives no long name; symbol is the only signal
    return out, names


def screen_universe(cfg, screener: Screener | None = None):
    """Yield {base, symbol, verdict} for every pair — for `python main.py screen`."""
    universe, names = build_universe(cfg)
    screener = screener or Screener(cfg)
    screener.set_names(names)
    results = []
    for u in universe:
        v = screener.check(u["base"])
        results.append({"base": u["base"], "symbol": u["symbol"], "verdict": v})
    return results


def scan(cfg, screener: Screener | None = None, verbose: bool = False) -> list[dict]:
    """Full market scan: screen -> liquidity filter -> indicators -> ranked shortlist."""
    universe, names = build_universe(cfg)
    screener = screener or Screener(cfg)
    screener.set_names(names)

    tickers = {t["symbol"]: t for t in binance.ticker_24hr()}
    min_vol = cfg.universe.min_24h_quote_volume_usd

    candidates = []
    for u in universe:
        t = tickers.get(u["symbol"])
        if not t:
            continue
        qv = float(t["quoteVolume"])
        if qv < min_vol:
            continue
        verdict = screener.check(u["base"])
        if not verdict.allowed:
            continue
        candidates.append({
            **u,
            "quote_volume_24h": round(qv),
            "change_24h_pct": round(float(t["priceChangePercent"]), 2),
            "screen": verdict.reasons,
            "screen_verified": verdict.verified,
        })

    candidates.sort(key=lambda c: c["quote_volume_24h"], reverse=True)
    # pass 1 — rank the volume pool by daily momentum
    pool = candidates[: cfg.universe.max_candidates_for_agent * 2]
    ranked = []
    for c in pool:
        try:
            rows = binance.klines(c["symbol"], cfg.universe.kline_interval, cfg.universe.kline_lookback)
        except binance.BinanceError:
            continue
        df = klines_to_df(rows)
        if len(df) < 50:
            continue
        c["features"] = compute_features(df)
        c["structure"] = compute_structure(df)
        c["ict_1d"] = compute_ict(df)
        c["_frames"] = {"1d": df}
        ranked.append(c)
        if verbose:
            print(f"  ranked {c['symbol']:<14} trend={c['features']['trend_score']:>5}")
        time.sleep(0.04)

    ranked.sort(key=lambda c: c["features"]["trend_score"], reverse=True)
    shortlist = ranked[: cfg.universe.max_candidates_for_agent]

    # pass 2 — multi-timeframe (htf bias + setup POI) for the final shortlist only
    tf = getattr(cfg, "timeframes", None)
    htf = getattr(tf, "htf", "1w") if tf else "1w"
    setup = getattr(tf, "setup", "4h") if tf else "4h"
    htf_lb = getattr(tf, "htf_lookback", 160) if tf else 160
    setup_lb = getattr(tf, "setup_lookback", 180) if tf else 180
    for c in shortlist:
        mtf = {"1d": c["structure"]}
        ict = {"1d": c["ict_1d"]}
        for tf_name, limit, lb in ((htf, 300, htf_lb), (setup, 500, setup_lb)):
            try:
                d = klines_to_df(binance.klines(c["symbol"], tf_name, limit))
            except binance.BinanceError:
                continue
            if len(d) < 30:
                continue
            c["_frames"][tf_name] = d
            mtf[tf_name] = compute_structure(d, lb)
            ict[tf_name] = compute_ict(d, lb)
            time.sleep(0.04)
        c["mtf"] = mtf
        c["ict"] = ict
        c["mtf_order"] = [htf, setup, "1d"]
        if verbose:
            print(f"  mtf    {c['symbol']:<14} {mtf.get(htf, {}).get('market_structure', '?')}/"
                  f"{mtf.get(setup, {}).get('market_structure', '?')}")

    return shortlist
