"""Market-regime detection.

The agent is told the regime and picks its playbook from it:
  strong_bull → trend continuation, buy BOS retraces / breakout retests, deploy cash
  bull        → selective continuation, respect discount zones
  neutral     → only A+ setups, smaller size, hold more cash
  bear        → capital preservation; spot-only means mostly cash, no new longs
"""
from __future__ import annotations

from . import binance
from .indicators import ema, klines_to_df

_ANCHORS = ("BTCUSDT", "ETHUSDT")


def _coin_trend(symbol: str) -> dict | None:
    try:
        df = klines_to_df(binance.klines(symbol, "1d", 250))
    except binance.BinanceError:
        return None
    if len(df) < 210:
        return None
    close = df["close"]
    price = float(close.iloc[-1])
    e50, e200 = float(ema(close, 50).iloc[-1]), float(ema(close, 200).iloc[-1])
    hi200 = float(close.iloc[-200:].max())
    return {
        "symbol": symbol,
        "price": price,
        "above_ema200": price > e200,
        "ema50_gt_ema200": e50 > e200,
        "ret_30d": round(price / float(close.iloc[-31]) * 100 - 100, 1),
        "ret_90d": round(price / float(close.iloc[-91]) * 100 - 100, 1),
        "pct_from_200d_high": round(price / hi200 * 100 - 100, 1),
    }


def _breadth(candidates: list[dict] | None) -> dict:
    if not candidates:
        return {}
    feats = [c["features"] for c in candidates if c.get("features")]
    if not feats:
        return {}
    n = len(feats)
    up_stack = sum(1 for f in feats if f.get("ema50_gt_ema200"))
    up_30d = sum(1 for f in feats if (f.get("ret_30d") or 0) > 0)
    return {
        "coins_sampled": n,
        "pct_ema50_gt_ema200": round(up_stack / n * 100, 1),
        "pct_positive_30d": round(up_30d / n * 100, 1),
    }


def _bull_run(btc: dict | None, breadth: dict) -> dict:
    """Best-effort macro bull-run read + phase, from BTC weekly + shortlist breadth."""
    out = {"active": False, "phase": None, "score": 0, "signals": [], "derisk": False}
    try:
        wk = klines_to_df(binance.klines("BTCUSDT", "1w", 320))
    except binance.BinanceError:
        return out
    if len(wk) < 60 or not btc:
        return out
    close = wk["close"]
    price = float(close.iloc[-1])
    e10, e20, e40 = (float(ema(close, n).iloc[-1]) for n in (10, 20, 40))
    ret_13w = price / float(close.iloc[-14]) * 100 - 100
    ret_52w = price / float(close.iloc[-53]) * 100 - 100 if len(close) > 53 else 0.0
    ath = float(close.max())
    from_ath = price / ath * 100 - 100

    sig = out["signals"]
    if btc["above_ema200"]:
        sig.append("BTC above daily EMA200")
    if e10 > e20 > e40:
        sig.append("BTC weekly EMA stack bullish (10>20>40)")
    if ret_52w > 25:
        sig.append(f"BTC +{ret_52w:.0f}% / 52w")
    if from_ath > -15:
        sig.append("BTC within 15% of all-time high")
    if breadth.get("pct_ema50_gt_ema200", 0) >= 60:
        sig.append("market breadth: >60% of alts in bull stack")
    if breadth.get("pct_positive_30d", 0) >= 65:
        sig.append("market breadth: >65% of alts green over 30d")
    out["score"] = len(sig)
    out["active"] = out["score"] >= 4 and btc["above_ema200"]

    if out["active"]:
        weekly_ext = price / e40 * 100 - 100
        if ret_13w > 45 and breadth.get("pct_positive_30d", 0) >= 80:
            out["phase"] = "euphoria"
        elif weekly_ext > 55 or ret_52w > 140:
            out["phase"] = "late"
        elif ret_52w > 45:
            out["phase"] = "mid"
        else:
            out["phase"] = "early"

    # de-risk trigger: lost the daily EMA200 or weekly momentum rolled over hard
    if btc and (not btc["above_ema200"] or ret_13w < -20 or e10 < e20 < e40):
        out["derisk"] = True
    return out


def assess(cfg, candidates: list[dict] | None = None) -> dict:
    anchors = [t for t in (_coin_trend(s) for s in _ANCHORS) if t]
    breadth = _breadth(candidates)
    btc = next((a for a in anchors if a["symbol"] == "BTCUSDT"), None)

    label, budget = "neutral", 0.45
    if btc:
        strong_up = btc["above_ema200"] and btc["ema50_gt_ema200"]
        strong_dn = (not btc["above_ema200"]) and (not btc["ema50_gt_ema200"])
        b_ok = breadth.get("pct_ema50_gt_ema200", 50) >= 55
        if strong_up and btc["ret_30d"] > 12 and b_ok:
            label, budget = "strong_bull", 1.0
        elif strong_up and btc["ret_30d"] > -3:
            label, budget = "bull", 0.8
        elif strong_dn or btc["ret_30d"] < -18:
            label, budget = "bear", 0.15
        elif not btc["above_ema200"]:
            label, budget = "neutral", 0.35

    bull_run = _bull_run(btc, breadth)
    if bull_run["active"] and label in ("bull", "neutral"):
        label, budget = ("strong_bull", max(budget, 0.9)) if bull_run["phase"] in ("early", "mid") \
            else ("bull", max(budget, 0.7))
    if bull_run["derisk"] and label != "bear":
        budget = min(budget, 0.4)

    return {
        "label": label,
        "risk_budget": budget,          # multiplier the agent applies to sizing / deployment
        "bull_run": bull_run,
        "anchors": anchors,
        "breadth": breadth,
        "note": (
            "spot-only: in a bear regime prefer cash and only take A+ discount "
            "setups with a confirmed liquidity sweep + change of character."
        ),
    }
