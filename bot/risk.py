"""Risk engine — position sizing limits + automatic protective exits.

Everything here is enforced in code, *outside* the agent. The Claude agent
can ask to buy or sell, but every buy is clamped by `check_buy` and the
protective exits in `forced_exits` fire before the agent ever runs.
"""
from __future__ import annotations

from dataclasses import dataclass

from .portfolio import Portfolio


@dataclass
class BuyDecision:
    ok: bool
    usd: float          # clamped spend (0 when ok is False)
    reason: str

    def as_dict(self):
        return {"ok": self.ok, "usd": round(self.usd, 2), "reason": self.reason}


class RiskEngine:
    def __init__(self, cfg):
        r = cfg.risk
        self.max_position_pct = float(r.max_position_pct)
        self.max_open_positions = int(r.max_open_positions)
        self.cash_reserve_pct = float(r.cash_reserve_pct)
        self.min_order_usd = float(r.min_order_usd)
        self.stop_loss_pct = float(r.stop_loss_pct)
        self.take_profit_pct = float(r.take_profit_pct)
        self.trailing_stop_pct = float(r.trailing_stop_pct)
        self.max_daily_loss_pct = float(r.max_daily_loss_pct)

    # ── daily circuit breaker ────────────────────────────────
    def daily_loss_pct(self, pf: Portfolio, equity_now: float) -> float:
        base = pf.day_open_equity or equity_now
        if base <= 0:
            return 0.0
        return (equity_now / base - 1.0) * 100.0

    def halted(self, pf: Portfolio, equity_now: float) -> bool:
        """True once equity has dropped >= max_daily_loss_pct from the day's open."""
        return self.daily_loss_pct(pf, equity_now) <= -self.max_daily_loss_pct

    # ── protective exits (run before the agent) ──────────────
    def forced_exits(self, pf: Portfolio, prices: dict[str, float]) -> list[dict]:
        """Return a list of {asset, reason, pnl_pct} sell-everything orders."""
        orders = []
        tp = self.take_profit_pct / 100.0
        sl = self.stop_loss_pct / 100.0
        trail = self.trailing_stop_pct / 100.0
        trail_arm = tp / 2.0

        for asset, pos in list(pf.positions.items()):
            px = prices.get(asset + "USDT")
            if not px or pos.avg_cost <= 0 or pos.qty <= 0:
                continue
            pnl = px / pos.avg_cost - 1.0
            peak = max(pos.peak_price or px, px)

            if pnl <= -sl:
                reason = f"stop-loss ({pnl * 100:+.1f}% <= -{self.stop_loss_pct:g}%)"
            elif pnl >= tp:
                reason = f"take-profit ({pnl * 100:+.1f}% >= {self.take_profit_pct:g}%)"
            elif pnl >= trail_arm and px <= peak * (1.0 - trail):
                drop = px / peak - 1.0
                reason = f"trailing-stop ({drop * 100:+.1f}% from peak {peak:.6g})"
            else:
                continue

            orders.append({"asset": asset, "reason": reason, "pnl_pct": round(pnl * 100, 2)})
        return orders

    # ── buy sizing / gating ─────────────────────────────────
    def check_buy(self, pf: Portfolio, prices: dict[str, float], asset: str,
                  requested_usd: float) -> BuyDecision:
        equity = pf.equity(prices)
        if equity <= 0:
            return BuyDecision(False, 0.0, "no equity")

        if self.halted(pf, equity):
            return BuyDecision(False, 0.0,
                               f"daily loss halt ({self.daily_loss_pct(pf, equity):+.1f}%)")

        held = asset in pf.positions and pf.positions[asset].qty > 0
        if not held and len(pf.positions) >= self.max_open_positions:
            return BuyDecision(False, 0.0,
                               f"max open positions reached ({self.max_open_positions})")

        usd = float(requested_usd)
        notes = []

        # per-position cap
        pos_cap = equity * self.max_position_pct / 100.0
        cur_val = 0.0
        if held:
            px = prices.get(asset + "USDT")
            cur_val = pf.positions[asset].qty * px if px else pf.positions[asset].cost_basis()
        room = pos_cap - cur_val
        if room <= 0:
            return BuyDecision(False, 0.0,
                               f"position already at/above {self.max_position_pct:g}% cap")
        if usd > room:
            usd = room
            notes.append(f"clamped to {self.max_position_pct:g}% position cap")

        # cash reserve
        min_cash = equity * self.cash_reserve_pct / 100.0
        spendable = pf.cash - min_cash
        if spendable <= 0:
            return BuyDecision(False, 0.0,
                               f"cash reserve floor hit ({self.cash_reserve_pct:g}%)")
        if usd > spendable:
            usd = spendable
            notes.append(f"clamped to {self.cash_reserve_pct:g}% cash reserve")

        if usd < self.min_order_usd:
            return BuyDecision(False, 0.0,
                               f"below min order ${self.min_order_usd:g} (had ${usd:.2f})")

        return BuyDecision(True, usd, "; ".join(notes) or "ok")
