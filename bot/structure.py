"""Price-action / ICT-style structure features on DAILY candles.

Everything here is a best-effort daily-timeframe adaptation of common
"smart money" concepts (market structure, BOS/CHoCH, fair-value gaps,
order blocks, liquidity sweeps, premium/discount). It is decision
*input* for the agent — the agent decides which concepts matter for the
current market regime; nothing here places an order.

Pure pandas/numpy.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

SWING_K = 2          # a swing needs K higher/lower bars on each side
LOOKBACK = 120       # bars considered for structure
RANGE_BARS = 60      # bars used for the premium/discount range
EQ_TOL = 0.006       # 0.6% — "equal" highs/lows tolerance


def _swings(df: pd.DataFrame, k: int = SWING_K):
    """Return (highs, lows) as lists of (pos, price), oldest→newest."""
    high, low = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    highs, lows = [], []
    for i in range(k, n - k):
        seg_h = high[i - k:i + k + 1]
        seg_l = low[i - k:i + k + 1]
        if high[i] == seg_h.max() and (seg_h.argmax() == k):
            highs.append((i, float(high[i])))
        if low[i] == seg_l.min() and (seg_l.argmin() == k):
            lows.append((i, float(low[i])))
    return highs, lows


def _structure_dir(highs, lows) -> str:
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
        lh = highs[-1][1] < highs[-2][1]
        ll = lows[-1][1] < lows[-2][1]
        if hh and hl:
            return "up"
        if lh and ll:
            return "down"
    return "range"


def _bull_fvgs(df: pd.DataFrame):
    """3-candle bullish imbalance: low[i] > high[i-2]. Returns list of dicts."""
    high, low = df["high"].to_numpy(), df["low"].to_numpy()
    out = []
    for i in range(2, len(df)):
        if low[i] > high[i - 2]:
            out.append({"pos": i, "bottom": float(high[i - 2]), "top": float(low[i])})
    return out


def _bear_fvgs(df: pd.DataFrame):
    high, low = df["high"].to_numpy(), df["low"].to_numpy()
    out = []
    for i in range(2, len(df)):
        if high[i] < low[i - 2]:
            out.append({"pos": i, "top": float(low[i - 2]), "bottom": float(high[i])})
    return out


def compute_structure(df: pd.DataFrame, lookback: int = LOOKBACK) -> dict:
    """Compact, JSON-safe structure dict for one coin on whatever timeframe `df` is."""
    if len(df) < 30:
        return {"note": "not enough history"}

    d = df.iloc[-lookback:].reset_index(drop=True)
    price = float(d["close"].iloc[-1])
    last_pos = len(d) - 1
    highs, lows = _swings(d)

    struct = _structure_dir(highs, lows)

    # ── break of structure / change of character ─────────────
    last_bos = None
    choch_recent = False
    prior_high = highs[-2][1] if len(highs) >= 2 else (highs[-1][1] if highs else None)
    prior_low = lows[-2][1] if len(lows) >= 2 else (lows[-1][1] if lows else None)
    closes = d["close"].to_numpy()
    for j in range(last_pos, max(last_pos - 15, 0), -1):
        if prior_high and closes[j] > prior_high and closes[j - 1] <= prior_high:
            last_bos = {"dir": "bull", "age_bars": last_pos - j, "level": round(prior_high, 8)}
            choch_recent = struct != "up"
            break
        if prior_low and closes[j] < prior_low and closes[j - 1] >= prior_low:
            last_bos = {"dir": "bear", "age_bars": last_pos - j, "level": round(prior_low, 8)}
            choch_recent = struct != "down"
            break

    # ── nearest unfilled bullish FVG below price ─────────────
    bull_fvg_below = None
    for g in reversed(_bull_fvgs(d)):
        mid = (g["top"] + g["bottom"]) / 2
        if mid < price and float(d["low"].iloc[g["pos"]:].min()) >= g["bottom"] * 0.999:
            bull_fvg_below = {
                "top": round(g["top"], 8), "bottom": round(g["bottom"], 8),
                "dist_pct": round((mid / price - 1) * 100, 1),
                "age_bars": last_pos - g["pos"],
            }
            break

    # ── nearest bearish FVG above price (overhead supply) ────
    bear_fvg_above = None
    for g in reversed(_bear_fvgs(d)):
        mid = (g["top"] + g["bottom"]) / 2
        if mid > price:
            bear_fvg_above = {
                "top": round(g["top"], 8), "bottom": round(g["bottom"], 8),
                "dist_pct": round((mid / price - 1) * 100, 1),
            }
            break

    # ── bullish order block: last down candle before the up-impulse
    #    that produced the most recent higher high ─────────────
    bull_ob_below = None
    o, c = d["open"].to_numpy(), d["close"].to_numpy()
    hi_arr = d["high"].to_numpy()
    lo_arr = d["low"].to_numpy()
    if highs:
        hi_pos = highs[-1][0]
        for j in range(hi_pos, max(hi_pos - 12, 0), -1):
            if c[j] < o[j]:  # down candle
                top, bottom = float(o[j]), float(lo_arr[j])
                if (top + bottom) / 2 < price:
                    bull_ob_below = {
                        "top": round(top, 8), "bottom": round(bottom, 8),
                        "dist_pct": round(((top + bottom) / 2 / price - 1) * 100, 1),
                        "age_bars": last_pos - j,
                    }
                break

    # ── liquidity sweep: recent bar wicks below a prior swing low
    #    then closes back above it (stop-hunt + reclaim) ───────
    liquidity_sweep_recent = None
    if len(lows) >= 2:
        ref_low = min(p for _, p in lows[-4:-1]) if len(lows) >= 3 else lows[-2][1]
        for j in range(last_pos, max(last_pos - 6, 0), -1):
            if lo_arr[j] < ref_low and c[j] > ref_low:
                liquidity_sweep_recent = {
                    "age_bars": last_pos - j, "swept_level": round(ref_low, 8),
                }
                break

    # ── premium / discount ──────────────────────────────────
    rng = d.iloc[-RANGE_BARS:]
    r_lo, r_hi = float(rng["low"].min()), float(rng["high"].max())
    span = r_hi - r_lo
    range_pos = round((price - r_lo) / span * 100, 1) if span > 0 else 50.0
    zone = "discount" if range_pos < 45 else "premium" if range_pos > 55 else "equilibrium"

    # ── equal highs above / equal lows below (resting liquidity)
    equal_highs_above = None
    if len(highs) >= 2:
        for a in range(len(highs) - 1, 0, -1):
            h1, h2 = highs[a][1], highs[a - 1][1]
            if abs(h1 - h2) / h1 <= EQ_TOL and min(h1, h2) > price:
                equal_highs_above = {
                    "level": round((h1 + h2) / 2, 8),
                    "dist_pct": round(((h1 + h2) / 2 / price - 1) * 100, 1),
                }
                break
    equal_lows_below = None
    if len(lows) >= 2:
        for a in range(len(lows) - 1, 0, -1):
            l1, l2 = lows[a][1], lows[a - 1][1]
            if abs(l1 - l2) / l1 <= EQ_TOL and max(l1, l2) < price:
                equal_lows_below = {
                    "level": round((l1 + l2) / 2, 8),
                    "dist_pct": round(((l1 + l2) / 2 / price - 1) * 100, 1),
                }
                break

    return {
        "market_structure": struct,
        "last_break": last_bos,
        "change_of_character": choch_recent,
        "zone": zone,
        "range_pos_pct": range_pos,
        "bull_fvg_below": bull_fvg_below,
        "bear_fvg_above": bear_fvg_above,
        "bull_order_block_below": bull_ob_below,
        "liquidity_sweep_recent": liquidity_sweep_recent,
        "equal_highs_above": equal_highs_above,
        "equal_lows_below": equal_lows_below,
    }


def compute_ict(df: pd.DataFrame, lookback: int = LOOKBACK) -> dict:
    """Advanced ICT concepts on `df`'s timeframe: dealing range (external/internal),
    OTE, displacement, CRT, turtle soup, breaker, liquidity void, BPR, PD-array list.

    Best-effort from OHLC only — session/killzone/Silver-Bullet concepts need
    intraday timestamps and are handled in bot/sessions.py for the 15m frame."""
    if len(df) < 30:
        return {"note": "not enough history"}
    d = df.iloc[-lookback:].reset_index(drop=True)
    o, h, l, c = (d[x].to_numpy() for x in ("open", "high", "low", "close"))
    price = float(c[-1])
    highs, lows = _swings(d)

    # ── dealing range + external/internal ───────────────────
    rng = d.iloc[-RANGE_BARS:]
    r_lo, r_hi = float(rng["low"].min()), float(rng["high"].max())
    span = max(r_hi - r_lo, 1e-12)
    pos = (price - r_lo) / span
    eq = r_lo + span * 0.5
    dealing_range = {"low": round(r_lo, 8), "high": round(r_hi, 8),
                     "equilibrium": round(eq, 8), "pos_pct": round(pos * 100, 1),
                     "side": "premium" if pos > 0.5 else "discount"}
    near_ext = pos > 0.9 or pos < 0.1
    location = "external" if near_ext else "internal"

    # ── OTE (last bullish impulse leg: swing low -> swing high) ──
    ote = None
    if highs and lows and lows[-1][0] < highs[-1][0]:
        lo_p, hi_p = lows[-1][1], highs[-1][1]
        if hi_p > lo_p:
            leg = hi_p - lo_p
            z_hi, z_lo = hi_p - leg * 0.62, hi_p - leg * 0.79
            ote = {"zone_low": round(z_lo, 8), "zone_high": round(z_hi, 8),
                   "sweet_spot": round(hi_p - leg * 0.705, 8),
                   "price_in_zone": bool(z_lo <= price <= z_hi),
                   "dist_pct": round(((z_hi + z_lo) / 2 / price - 1) * 100, 1)}

    # ── displacement (momentum candle vs its own recent body avg) ──
    body = np.abs(c - o)
    avg_body = body[-15:].mean() or 1e-12
    displacement = None
    for j in range(len(d) - 1, max(len(d) - 8, 0), -1):
        if body[j] > 1.8 * avg_body and (h[j] - l[j]) > 1.5 * (h[-15:] - l[-15:]).mean():
            displacement = {"dir": "bull" if c[j] > o[j] else "bear",
                            "age_bars": len(d) - 1 - j,
                            "range_pct": round((h[j] - l[j]) / o[j] * 100, 1)}
            break

    # ── Candle Range Theory: sweep of prior candle's range, close back inside ──
    crt = None
    if len(d) >= 3:
        p_hi, p_lo = h[-2], l[-2]
        if l[-1] < p_lo and c[-1] > p_lo:
            crt = {"type": "bullish", "swept": round(float(p_lo), 8), "range_ref_bar": "prev"}
        elif h[-1] > p_hi and c[-1] < p_hi:
            crt = {"type": "bearish", "swept": round(float(p_hi), 8), "range_ref_bar": "prev"}

    # ── turtle soup (failed 20-bar breakout that reverses) ──
    turtle_soup = None
    if len(d) >= 25:
        prior_lo = float(np.min(l[-21:-1]))
        prior_hi = float(np.max(h[-21:-1]))
        if l[-1] < prior_lo and c[-1] > prior_lo:
            turtle_soup = {"type": "bullish", "level": round(prior_lo, 8)}
        elif h[-1] > prior_hi and c[-1] < prior_hi:
            turtle_soup = {"type": "bearish", "level": round(prior_hi, 8)}

    # ── liquidity void (widest unmitigated bullish FVG below price) ──
    liquidity_void = None
    for g in reversed(_bull_fvgs(d)):
        width = (g["top"] - g["bottom"]) / max(g["bottom"], 1e-12)
        mid = (g["top"] + g["bottom"]) / 2
        if width > 0.03 and mid < price and float(d["low"].iloc[g["pos"]:].min()) >= g["bottom"] * 0.999:
            liquidity_void = {"top": round(g["top"], 8), "bottom": round(g["bottom"], 8),
                              "width_pct": round(width * 100, 1)}
            break

    # ── BPR: overlapping bull + bear FVG near price ──
    bpr = None
    bulls, bears = _bull_fvgs(d), _bear_fvgs(d)
    for gb in reversed(bulls):
        for gs in reversed(bears):
            lo_ = max(gb["bottom"], gs["bottom"])
            hi_ = min(gb["top"], gs["top"])
            if hi_ > lo_ and abs((lo_ + hi_) / 2 / price - 1) < 0.06:
                bpr = {"bottom": round(lo_, 8), "top": round(hi_, 8),
                       "dist_pct": round((lo_ + hi_) / 2 / price * 100 - 100, 1)}
                break
        if bpr:
            break

    # ── PD-array matrix: ordered arrays below price (nearest first) ──
    arrays = []
    for g in reversed(bulls):
        mid = (g["top"] + g["bottom"]) / 2
        if mid < price:
            arrays.append({"kind": "bull_fvg", "top": round(g["top"], 8),
                           "bottom": round(g["bottom"], 8),
                           "dist_pct": round(mid / price * 100 - 100, 1)})
    if highs:
        hi_pos = highs[-1][0]
        for j in range(hi_pos, max(hi_pos - 12, 0), -1):
            if c[j] < o[j] and (o[j] + l[j]) / 2 < price:
                arrays.append({"kind": "bull_ob", "top": round(float(o[j]), 8),
                               "bottom": round(float(l[j]), 8),
                               "dist_pct": round((o[j] + l[j]) / 2 / price * 100 - 100, 1)})
                break
    arrays.sort(key=lambda a: -a["dist_pct"])   # closest below price first

    return {
        "dealing_range": dealing_range,
        "price_location": location,
        "ote": ote,
        "displacement": displacement,
        "crt": crt,
        "turtle_soup": turtle_soup,
        "liquidity_void": liquidity_void,
        "bpr": bpr,
        "pd_array_matrix_below": arrays[:4],
    }


def ict_summary(s: dict) -> str:
    if not s or s.get("note"):
        return (s or {}).get("note", "n/a")
    bits = [s["dealing_range"]["side"], s["price_location"]]
    if s.get("ote") and s["ote"]["price_in_zone"]:
        bits.append("in OTE")
    if s.get("displacement"):
        bits.append(f"{s['displacement']['dir']} displ")
    if s.get("crt"):
        bits.append(f"CRT {s['crt']['type'][:4]}")
    if s.get("turtle_soup"):
        bits.append(f"turtle {s['turtle_soup']['type'][:4]}")
    return ", ".join(bits)


def structure_summary(s: dict) -> str:
    """One-line human summary for logs."""
    if not s or s.get("note"):
        return s.get("note", "n/a") if s else "n/a"
    bits = [s["market_structure"], s["zone"]]
    if s.get("last_break"):
        b = s["last_break"]
        bits.append(f"{b['dir']}-BOS {b['age_bars']}d ago")
    if s.get("change_of_character"):
        bits.append("CHoCH")
    if s.get("liquidity_sweep_recent"):
        bits.append(f"sweep {s['liquidity_sweep_recent']['age_bars']}d ago")
    return ", ".join(bits)
