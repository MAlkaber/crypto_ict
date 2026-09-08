"""Session / killzone / draw-on-liquidity analysis for the 15m entry frame.

ICT time-of-day concepts. Windows are in UTC and approximate New York
local killzones (ET); DST shifts them ~1h so treat them as guides, not
exact. Nothing here trades — it's entry context for the agent.
"""
from __future__ import annotations

import pandas as pd

# (label, start_hour_utc, end_hour_utc)
_KILLZONES = [
    ("asia_kz", 0, 5),
    ("london_kz", 7, 10),
    ("ny_am_kz", 12, 15),
    ("london_silver_bullet", 8, 9),
    ("ny_am_silver_bullet", 15, 16),
    ("ny_pm_silver_bullet", 19, 20),
]
_SESSIONS = [("asia", 23, 8), ("london", 7, 12), ("ny", 12, 20)]


def _in_window(hour: int, start: int, end: int) -> bool:
    return start <= hour < end if start <= end else (hour >= start or hour < end)


def _session_range(df: pd.DataFrame, start: int, end: int) -> dict | None:
    hrs = df.index.hour
    mask = [_in_window(h, start, end) for h in hrs]
    seg = df[mask]
    if seg.empty:
        return None
    # only the most recent day this session appears in
    last_day = seg.index[-1].date()
    seg = seg[seg.index.date == last_day]
    if seg.empty:
        return None
    return {"high": round(float(seg["high"].max()), 8),
            "low": round(float(seg["low"].min()), 8),
            "day": str(last_day)}


def session_levels(d15: pd.DataFrame) -> dict:
    """d15: 15m OHLC with a tz-naive UTC DatetimeIndex."""
    if d15 is None or len(d15) < 8:
        return {"note": "not enough 15m data"}
    price = float(d15["close"].iloc[-1])
    now = d15.index[-1]
    cur_hour = int(now.hour)

    kz = [name for name, s, e in _KILLZONES if _in_window(cur_hour, s, e)]

    sess = {}
    for name, s, e in _SESSIONS:
        r = _session_range(d15, s, e)
        if r:
            sess[name] = r

    # prior-day high/low from the 15m series
    days = sorted({d for d in d15.index.date})
    pdh = pdl = None
    if len(days) >= 2:
        prev = d15[d15.index.date == days[-2]]
        if not prev.empty:
            pdh, pdl = round(float(prev["high"].max()), 8), round(float(prev["low"].min()), 8)

    # recent sweeps (last 8 bars wick beyond a level, close back on the other side)
    recent = d15.iloc[-8:]
    sweeps = []
    checks = {}
    for name, r in sess.items():
        checks[f"{name}_high"] = r["high"]
        checks[f"{name}_low"] = r["low"]
    if pdh:
        checks["PDH"] = pdh
    if pdl:
        checks["PDL"] = pdl
    for label, lvl in checks.items():
        hi_break = (recent["high"] > lvl).any() and recent["close"].iloc[-1] < lvl
        lo_break = (recent["low"] < lvl).any() and recent["close"].iloc[-1] > lvl
        if label.endswith("low") or label == "PDL":
            if lo_break:
                sweeps.append({"level": label, "price": lvl, "side": "sell-side swept (bullish)"})
        elif hi_break:
            sweeps.append({"level": label, "price": lvl, "side": "buy-side swept (bearish)"})

    return {
        "now_utc": str(now)[:16],
        "current_killzones": kz or ["none"],
        "sessions": sess,
        "prior_day_high": pdh,
        "prior_day_low": pdl,
        "recent_sweeps": sweeps,
        "price": price,
        "note": "killzone windows are approximate UTC (ET DST shifts ~1h)",
    }
