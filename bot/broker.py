"""Execution layer. PaperBroker simulates fills; LiveBroker sends real
spot orders (triple-gated). Both expose the same buy/sell interface.

SPOT ONLY. `buy` spends USDT cash; `sell` reduces a holding you own.
There is no short, no borrow, no leverage anywhere in this file.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from . import binance
from .portfolio import Portfolio, Position

PAPER_FEE = 0.001       # 0.1% taker
PAPER_SLIPPAGE = 0.0007  # 0.07% adverse


@dataclass
class Fill:
    ok: bool
    side: str
    asset: str
    qty: float = 0.0
    price: float = 0.0
    usd: float = 0.0
    fee: float = 0.0
    message: str = ""

    def as_dict(self):
        return self.__dict__


class PaperBroker:
    def __init__(self, portfolio: Portfolio, prices: dict[str, float]):
        self.pf = portfolio
        self.prices = prices

    def _px(self, asset: str) -> float | None:
        return self.prices.get(asset + "USDT")

    def buy(self, asset: str, usd: float, reason: str = "") -> Fill:
        px = self._px(asset)
        if not px:
            return Fill(False, "BUY", asset, message="no price")
        if usd > self.pf.cash:
            usd = self.pf.cash
        if usd <= 0:
            return Fill(False, "BUY", asset, message="no cash")
        fill_px = px * (1 + PAPER_SLIPPAGE)
        fee = usd * PAPER_FEE
        qty = (usd - fee) / fill_px
        self.pf.cash -= usd

        pos = self.pf.positions.get(asset, Position())
        new_qty = pos.qty + qty
        pos.avg_cost = (pos.cost_basis() + (usd - fee)) / new_qty if new_qty else 0.0
        pos.qty = new_qty
        pos.peak_price = max(pos.peak_price, fill_px)
        if not pos.opened_at:
            pos.opened_at = time.time()
        self.pf.positions[asset] = pos

        self.pf.record_trade(mode="paper", side="BUY", asset=asset, qty=qty,
                             price=fill_px, usd=usd, fee=fee, reason=reason)
        return Fill(True, "BUY", asset, qty, fill_px, usd, fee, "filled (paper)")

    def sell(self, asset: str, usd: float | None, reason: str = "") -> Fill:
        pos = self.pf.positions.get(asset)
        if not pos or pos.qty <= 0:
            return Fill(False, "SELL", asset, message="no position")
        px = self._px(asset)
        if not px:
            return Fill(False, "SELL", asset, message="no price")
        fill_px = px * (1 - PAPER_SLIPPAGE)
        pos_value = pos.qty * fill_px
        sell_value = pos_value if usd is None else min(usd, pos_value)
        qty = pos.qty if usd is None else min(pos.qty, sell_value / fill_px)
        proceeds = qty * fill_px
        fee = proceeds * PAPER_FEE
        self.pf.cash += proceeds - fee
        pos.qty -= qty
        if pos.qty * fill_px < 1:      # dust -> close
            realised = None
            self.pf.positions.pop(asset, None)
        self.pf.record_trade(mode="paper", side="SELL", asset=asset, qty=qty,
                             price=fill_px, usd=proceeds - fee, fee=fee, reason=reason)
        return Fill(True, "SELL", asset, qty, fill_px, proceeds - fee, fee, "filled (paper)")


class LiveBroker:
    """Real Binance spot orders. Only constructed when: config mode == live,
    the --live flag is passed, AND I_UNDERSTAND_LIVE_TRADING=yes in .env."""

    def __init__(self, cfg, portfolio: Portfolio, prices: dict[str, float]):
        if not (cfg.mode == "live" and cfg.live_confirmed):
            raise RuntimeError("LiveBroker blocked: set mode: live and I_UNDERSTAND_LIVE_TRADING=yes")
        if not (cfg.binance_api_key and cfg.binance_api_secret):
            raise RuntimeError("LiveBroker needs BINANCE_API_KEY / BINANCE_API_SECRET")
        self.cfg = cfg
        self.pf = portfolio
        self.prices = prices
        self._filters = {
            s["symbol"]: binance.symbol_filters(s)
            for s in binance.exchange_info()["symbols"]
        }

    def _px(self, asset: str) -> float | None:
        return self.prices.get(asset + "USDT")

    def buy(self, asset: str, usd: float, reason: str = "") -> Fill:
        symbol = asset + "USDT"
        f = self._filters.get(symbol, {})
        if f.get("min_notional") and usd < f["min_notional"]:
            return Fill(False, "BUY", asset, message=f"below min notional {f['min_notional']}")
        try:
            res = binance.create_market_order(
                symbol, "BUY", quote_qty=round(usd, 2),
                api_key=self.cfg.binance_api_key, api_secret=self.cfg.binance_api_secret)
        except binance.BinanceError as e:
            return Fill(False, "BUY", asset, message=str(e))
        return self._apply_fill(asset, "BUY", res, reason)

    def sell(self, asset: str, usd: float | None, reason: str = "") -> Fill:
        symbol = asset + "USDT"
        pos = self.pf.positions.get(asset)
        if not pos or pos.qty <= 0:
            return Fill(False, "SELL", asset, message="no position")
        f = self._filters.get(symbol, {})
        px = self._px(asset) or pos.avg_cost
        qty = pos.qty if usd is None else min(pos.qty, usd / px)
        qty = binance.round_step(qty, f.get("step_size", 0.0))
        if qty <= 0:
            return Fill(False, "SELL", asset, message="qty rounds to zero")
        try:
            res = binance.create_market_order(
                symbol, "SELL", quantity=qty,
                api_key=self.cfg.binance_api_key, api_secret=self.cfg.binance_api_secret)
        except binance.BinanceError as e:
            return Fill(False, "SELL", asset, message=str(e))
        return self._apply_fill(asset, "SELL", res, reason)

    def _apply_fill(self, asset: str, side: str, res: dict, reason: str) -> Fill:
        fills = res.get("fills", [])
        qty = sum(float(x["qty"]) for x in fills) or float(res.get("executedQty", 0))
        quote = sum(float(x["qty"]) * float(x["price"]) for x in fills)
        fee = sum(float(x.get("commission", 0)) for x in fills
                  if x.get("commissionAsset") == "USDT")
        avg_px = quote / qty if qty else 0.0

        if side == "BUY":
            self.pf.cash -= quote + fee
            pos = self.pf.positions.get(asset, Position())
            new_qty = pos.qty + qty
            pos.avg_cost = (pos.cost_basis() + quote) / new_qty if new_qty else 0.0
            pos.qty = new_qty
            pos.peak_price = max(pos.peak_price, avg_px)
            pos.opened_at = pos.opened_at or time.time()
            self.pf.positions[asset] = pos
        else:
            self.pf.cash += quote - fee
            pos = self.pf.positions.get(asset)
            if pos:
                pos.qty -= qty
                if pos.qty * avg_px < 1:
                    self.pf.positions.pop(asset, None)

        self.pf.record_trade(mode="live", side=side, asset=asset, qty=qty,
                             price=avg_px, usd=quote, fee=fee, reason=reason)
        return Fill(True, side, asset, qty, avg_px, quote, fee, "filled (live)")


def make_broker(cfg, portfolio: Portfolio, prices: dict[str, float], live: bool):
    if live:
        return LiveBroker(cfg, portfolio, prices)
    return PaperBroker(portfolio, prices)
