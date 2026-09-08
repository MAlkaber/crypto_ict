"""Thin Binance spot REST client.

Public endpoints need no key. Signed endpoints (account, order) are only
used in live mode and require BINANCE_API_KEY / BINANCE_API_SECRET with
SPOT permission only (no withdrawals, no margin).
"""
from __future__ import annotations

import hashlib
import hmac
import math
import time
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode

import requests

BASE = "https://api.binance.com"
_LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")

_session = requests.Session()
_session.headers.update({"User-Agent": "halal-crypto-bot/1.0"})


class BinanceError(RuntimeError):
    pass


def _request(method: str, path: str, params=None, *, signed=False, api_key=None, api_secret=None):
    params = dict(params or {})
    headers = {}
    if signed:
        if not api_key or not api_secret:
            raise BinanceError("signed request requires API key + secret")
        params["timestamp"] = int(time.time() * 1000)
        params.setdefault("recvWindow", 5000)
        query = urlencode(params)
        params["signature"] = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        headers["X-MBX-APIKEY"] = api_key

    url = BASE + path
    for attempt in range(4):
        try:
            resp = _session.request(method, url, params=params, headers=headers, timeout=20)
        except requests.RequestException as exc:
            if attempt == 3:
                raise BinanceError(f"network error: {exc}") from exc
            time.sleep(2 ** attempt)
            continue
        if resp.status_code in (429, 418) or resp.status_code >= 500:
            time.sleep(2 ** attempt + 1)
            continue
        if not resp.ok:
            raise BinanceError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()
    raise BinanceError(f"retries exhausted: {method} {path}")


# ── public market data ───────────────────────────────────────

def exchange_info() -> dict:
    return _request("GET", "/api/v3/exchangeInfo")


def ticker_24hr() -> list[dict]:
    return _request("GET", "/api/v3/ticker/24hr")


def all_prices() -> dict[str, float]:
    return {row["symbol"]: float(row["price"]) for row in _request("GET", "/api/v3/ticker/price")}


def price(symbol: str) -> float:
    return float(_request("GET", "/api/v3/ticker/price", {"symbol": symbol})["price"])


def klines(symbol: str, interval: str = "1d", limit: int = 250) -> list[list]:
    return _request("GET", "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})


# ── helpers ──────────────────────────────────────────────────

def is_leveraged_token(base_asset: str) -> bool:
    return any(base_asset.endswith(s) for s in _LEVERAGED_SUFFIXES) and base_asset not in ("JUP",)


def symbol_filters(symbol_info: dict) -> dict:
    out = {"step_size": 0.0, "min_qty": 0.0, "tick_size": 0.0, "min_notional": 0.0}
    for f in symbol_info.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            out["step_size"] = float(f["stepSize"])
            out["min_qty"] = float(f["minQty"])
        elif f["filterType"] == "PRICE_FILTER":
            out["tick_size"] = float(f["tickSize"])
        elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
            out["min_notional"] = float(f.get("minNotional", f.get("notional", 0)))
    return out


def round_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return float(Decimal(str(qty)).quantize(Decimal(str(step)), rounding=ROUND_DOWN))


# ── signed endpoints (live only) ─────────────────────────────

def account(api_key: str, api_secret: str) -> dict:
    return _request("GET", "/api/v3/account", signed=True, api_key=api_key, api_secret=api_secret)


def create_market_order(symbol: str, side: str, *, quote_qty: float | None = None,
                        quantity: float | None = None, api_key: str, api_secret: str) -> dict:
    params = {"symbol": symbol, "side": side, "type": "MARKET"}
    if quote_qty is not None:
        params["quoteOrderQty"] = f"{quote_qty:.8f}".rstrip("0").rstrip(".")
    if quantity is not None:
        params["quantity"] = f"{quantity:.8f}".rstrip("0").rstrip(".")
    return _request("POST", "/api/v3/order", params, signed=True, api_key=api_key, api_secret=api_secret)
