"""Portfolio state: cash, positions, trade log, equity curve. JSON-persisted."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
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

    def cost_basis(self) -> float:
        return self.qty * self.avg_cost


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)
    day: str = field(default_factory=_utc_day)
    day_open_equity: float = 0.0
    created_at: float = field(default_factory=time.time)

    # ── persistence ──────────────────────────────────────────
    @classmethod
    def load_or_create(cls, path: Path, starting_balance: float) -> "Portfolio":
        if path.exists():
            raw = json.loads(path.read_text())
            raw["positions"] = {k: Position(**v) for k, v in raw.get("positions", {}).items()}
            return cls(**raw)
        p = cls(cash=starting_balance, day_open_equity=starting_balance)
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
