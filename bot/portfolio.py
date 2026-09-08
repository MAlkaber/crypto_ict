"""Portfolio state: cash, positions, trade log, equity curve. JSON-persisted."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, fields, asdict
from datetime import datetime, timezone
from pathlib import Path


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Position:
    qty: float = 0.0
    avg_cost: float = 0.0        # USDT per unit
    peak_price: float = 0.0      # for trailing stop
    opened_at: float = 0.0
    stop: float = 0.0            # current protective stop price (moves with the trade)
    init_stop: float = 0.0       # original structural stop — defines 1R
    target: float = 0.0          # first target (next liquidity pool)
    partial_done: bool = False   # first partial already taken
    concepts: list = field(default_factory=list)

    def cost_basis(self) -> float:
        return self.qty * self.avg_cost

    def r_distance(self) -> float:
        """1R as a price distance (entry - original stop)."""
        return max(self.avg_cost - self.init_stop, 0.0) if self.init_stop else 0.0

    def r_multiple(self, price: float) -> float | None:
        d = self.r_distance()
        return (price - self.avg_cost) / d if d > 0 else None


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)
    day: str = field(default_factory=_utc_day)
    day_open_equity: float = 0.0
    peak_equity: float = 0.0        # high-water mark, for the drawdown circuit breaker
    halted: bool = False            # portfolio circuit breaker latched on
    halt_reason: str = ""
    created_at: float = field(default_factory=time.time)

    # ── persistence ──────────────────────────────────────────
    @classmethod
    def load_or_create(cls, path: Path, starting_balance: float) -> "Portfolio":
        if path.exists():
            raw = json.loads(path.read_text())
            known = {f.name for f in fields(cls)}
            raw = {k: v for k, v in raw.items() if k in known}          # tolerate old/extra keys
            pos_known = {f.name for f in fields(Position)}
            raw["positions"] = {k: Position(**{kk: vv for kk, vv in v.items() if kk in pos_known})
                                for k, v in raw.get("positions", {}).items()}
            return cls(**raw)
        p = cls(cash=starting_balance, day_open_equity=starting_balance,
                peak_equity=starting_balance)
        return p

    def save(self, path: Path):
        raw = asdict(self)
        path.write_text(json.dumps(raw, indent=2, default=float))

    # ── valuation ────────────────────────────────────────────
    def holdings_value(self, prices: dict[str, float]) -> float:
        total = 0.0
        for asset, pos in self.positions.items():
            px = prices.get(asset + "USDT")
            if px:
                total += pos.qty * px
        return total

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.holdings_value(prices)

    def roll_day(self, equity_now: float):
        today = _utc_day()
        if today != self.day:
            self.day = today
            self.day_open_equity = equity_now
        if self.day_open_equity <= 0:
            self.day_open_equity = equity_now

    def mark(self, prices: dict[str, float]):
        eq = self.equity(prices)
        self.roll_day(eq)
        self.peak_equity = max(self.peak_equity or eq, eq)
        for asset, pos in self.positions.items():
            px = prices.get(asset + "USDT")
            if px:
                pos.peak_price = max(pos.peak_price or px, px)
        self.equity_curve.append({
            "ts": time.time(),
            "equity": round(eq, 2),
            "cash": round(self.cash, 2),
        })
        # keep the curve from growing unbounded
        if len(self.equity_curve) > 5000:
            self.equity_curve = self.equity_curve[-5000:]

    def drawdown_pct(self, equity_now: float) -> float:
        """Current % below the equity high-water mark (0 or negative)."""
        peak = max(self.peak_equity or equity_now, equity_now)
        return (equity_now / peak - 1.0) * 100.0 if peak > 0 else 0.0

    def rolling_return_pct(self, equity_now: float, days: float = 7.0) -> float:
        """% change vs the earliest equity-curve point within the last `days`."""
        cutoff = time.time() - days * 86400
        past = [p["equity"] for p in self.equity_curve if p["ts"] >= cutoff]
        base = past[0] if past else (self.equity_curve[0]["equity"] if self.equity_curve else equity_now)
        return (equity_now / base - 1.0) * 100.0 if base > 0 else 0.0

    def record_trade(self, **kw):
        kw.setdefault("ts", time.time())
        kw.setdefault("iso", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.trades.append(kw)

    def unrealised(self, prices: dict[str, float]) -> dict[str, dict]:
        out = {}
        for asset, pos in self.positions.items():
            px = prices.get(asset + "USDT")
            if not px:
                continue
            out[asset] = {
                "qty": pos.qty,
                "avg_cost": round(pos.avg_cost, 6),
                "price": px,
                "value": round(pos.qty * px, 2),
                "pnl_pct": round((px / pos.avg_cost - 1) * 100, 2) if pos.avg_cost else 0.0,
            }
        return out
