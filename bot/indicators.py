"""Technical indicators + a compact feature vector per coin.

Pure pandas/numpy — no TA-Lib (keeps Windows install painless).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def klines_to_df(rows: list[list]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "tb_base", "tb_quote", "ignore",
    ])
    for col in ("open", "high", "low", "close", "volume", "quote_volume"):
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df.set_index("open_time")


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["close"].shift()
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def _ret(close: pd.Series, periods: int):
    if len(close) <= periods:
        return None
    return round(float(close.iloc[-1] / close.iloc[-1 - periods] * 100 - 100), 2)


def compute_features(df: pd.DataFrame) -> dict:
    """Compact, JSON-safe feature dict for one coin (expects daily candles)."""
    close = df["close"]
    price = float(close.iloc[-1])
    e20, e50, e200 = ema(close, 20), ema(close, 50), ema(close, 200)
    r = float(rsi(close).iloc[-1])
    atr_pct = float(atr(df).iloc[-1] / price * 100)

    window = close.iloc[-90:]
    hi90, lo90 = float(window.max()), float(window.min())
    vol_ratio = None
    if len(df) >= 20 and df["volume"].iloc[-20:].mean() > 0:
        vol_ratio = round(float(df["volume"].iloc[-1] / df["volume"].iloc[-20:].mean()), 2)

    feats = {
        "price": price,
        "rsi14": round(r, 1),
        "atr_pct": round(atr_pct, 2),
        "ret_7d": _ret(close, 7),
        "ret_30d": _ret(close, 30),
        "ret_90d": _ret(close, 90),
        "vol_ratio_20d": vol_ratio,
        "pct_from_90d_high": round(price / hi90 * 100 - 100, 1) if hi90 else None,
        "pct_above_90d_low": round(price / lo90 * 100 - 100, 1) if lo90 else None,
        "ema20_gt_ema50": bool(e20.iloc[-1] > e50.iloc[-1]),
        "ema50_gt_ema200": bool(e50.iloc[-1] > e200.iloc[-1]) if len(close) >= 200 else None,
    }

    # simple composite trend score used only for pre-ranking the shortlist
    score = 0.0
    if feats["ema50_gt_ema200"]:
        score += 1.0
    if feats["ema20_gt_ema50"]:
        score += 1.0
    if feats["ret_30d"] is not None:
        score += float(np.clip(feats["ret_30d"] / 20.0, -2.0, 2.0))
    if feats["ret_7d"] is not None:
        score += float(np.clip(feats["ret_7d"] / 15.0, -1.0, 1.0))
    if 45 <= r <= 72:
        score += 0.5
    if r > 82:
        score -= 1.0
    if vol_ratio and vol_ratio > 1.5:
        score += 0.5
    feats["trend_score"] = round(score, 2)
    return feats
