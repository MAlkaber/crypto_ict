"""Risk engine — R-based sizing + structural, trade-managed exits.

Design (swing / ICT):
- Every buy carries a STRUCTURAL stop (the agent's invalidation level). Size is
  set so that hitting that stop loses `risk_per_trade_pct` of equity (= 1R).
- Exits are managed in R multiples: partial at +Nr (then stop to breakeven),
  trail the stop once past +Nr, plus a wide disaster stop as a data-error guard.
- The portfolio circuit breaker is measured from the equity HIGH-WATER MARK and
  over a rolling 7 days — not from the day's open (that's a day-trading notion).

All enforced in code, outside the agent.
"""
from __future__ import annotations

from dataclasses import dataclass

from .portfolio import Portfolio


@dataclass
class BuyDecision:
    ok: bool
    usd: float
    stop: float          # possibly-clamped structural stop to store on the position
    target: float
    r_pct: float          # stop distance as % of entry
    reason: str

    def as_dict(self):
        return {"ok": self.ok, "usd": round(self.usd, 2), "stop": self.stop,
                "target": self.target, "r_pct": round(self.r_pct, 2), "reason": self.reason}


class RiskEngine:
    def __init__(self, cfg):
        r = cfg.risk
        self.risk_per_trade_pct = float(r.risk_per_trade_pct)
        self.max_position_pct = float(r.max_position_pct)
        self.max_open_positions = int(r.max_open_positions)
        self.cash_reserve_pct = float(r.cash_reserve_pct)
        self.min_order_usd = float(r.min_order_usd)
        self.max_stop_distance_pct = float(r.max_stop_distance_pct)
        self.min_stop_distance_pct = float(r.min_stop_distance_pct)
        self.disaster_stop_pct = float(r.disaster_stop_pct)
        self.partial_tp_at_r = float(r.partial_tp_at_r)
        self.partial_tp_fraction = float(r.partial_tp_fraction)
        self.trail_after_r = float(r.trail_after_r)
        self.trail_giveback_pct = float(r.trail_giveback_pct)
        self.max_drawdown_pct = float(r.max_drawdown_pct)
        self.resume_drawdown_pct = float(r.resume_drawdown_pct)
        self.max_rolling_7d_loss_pct = float(r.max_rolling_7d_loss_pct)

    # ── portfolio circuit breaker (call once per cycle) ─────
    def update_halt(self, pf: Portfolio, equity_now: float) -> bool:
        dd = pf.drawdown_pct(equity_now)
        roll = pf.rolling_return_pct(equity_now, 7.0)
        if not pf.halted:
            if dd <= -self.max_drawdown_pct or roll <= -self.max_rolling_7d_loss_pct:
                pf.halted = True
                pf.halt_reason = (f"drawdown {dd:+.1f}% from peak" if dd <= -self.max_drawdown_pct
                                  else f"7d loss {roll:+.1f}%")
        elif dd >= -self.resume_drawdown_pct and roll > -self.max_rolling_7d_loss_pct:
            pf.halted = False
            pf.halt_reason = ""
        return pf.halted

    def halted(self, pf: Portfolio, equity_now: float) -> bool:
        return pf.halted

    # ── stop management: ratchet trailing / breakeven ───────
    def manage_stops(self, pf: Portfolio, prices: dict[str, float]) -> list[dict]:
        moves = []
        for asset, pos in pf.positions.items():
            px = prices.get(asset + "USDT")
            if not px or pos.avg_cost <= 0:
                continue
            r = pos.r_multiple(px)
            new_stop = pos.stop
            if pos.partial_done:                       # never give back a booked winner
                new_stop = max(new_stop, pos.avg_cost)
            if r is not None and r >= self.trail_after_r:
                trail = (pos.peak_price or px) * (1 - self.trail_giveback_pct / 100.0)
                new_stop = max(new_stop, trail)
            if new_stop > pos.stop * 1.0001:
                moves.append({"asset": asset, "old_stop": round(pos.stop, 8),
                              "new_stop": round(new_stop, 8)})
                pos.stop = new_stop
        return moves

    # ── protective / target exits ──────────────────────────
    def forced_exits(self, pf: Portfolio, prices: dict[str, float]) -> list[dict]:
        """Return orders: {asset, reason, fraction (None = all), r}."""
        orders = []
        for asset, pos in list(pf.positions.items()):
            px = prices.get(asset + "USDT")
            if not px or pos.avg_cost <= 0 or pos.qty <= 0:
                continue
            r = pos.r_multiple(px)
            disaster = pos.avg_cost * (1 - self.disaster_stop_pct / 100.0)

            if px <= disaster:
                orders.append({"asset": asset, "fraction": None, "r": _rr(r),
                               "reason": f"disaster stop (-{self.disaster_stop_pct:g}% from entry)"})
            elif pos.stop and px <= pos.stop:
                tag = "structural stop" if not pos.partial_done else "breakeven/trail stop"
                orders.append({"asset": asset, "fraction": None, "r": _rr(r),
                               "reason": f"{tag} hit @ {pos.stop:.6g}"})
            elif (not pos.partial_done and r is not None and r >= self.partial_tp_at_r):
                orders.append({"asset": asset, "fraction": self.partial_tp_fraction, "r": _rr(r),
                               "reason": f"partial +{r:.1f}R — book {self.partial_tp_fraction:.0%}, "
                                         f"stop to breakeven"})
        return orders

    # ── buy sizing / gating ────────────────────────────────
    def check_buy(self, pf: Portfolio, prices: dict[str, float], asset: str,
                  entry: float, stop: float, target: float) -> BuyDecision:
        equity = pf.equity(prices)
        if equity <= 0 or entry <= 0:
            return BuyDecision(False, 0.0, stop, target, 0.0, "no equity / price")
        if pf.halted:
            return BuyDecision(False, 0.0, stop, target, 0.0,
                               f"circuit breaker active ({pf.halt_reason})")

        held = asset in pf.positions and pf.positions[asset].qty > 0
        if not held and len(pf.positions) >= self.max_open_positions:
            return BuyDecision(False, 0.0, stop, target, 0.0,
                               f"max open positions ({self.max_open_positions})")

        # ── validate / normalise the structural stop ─────────
        if not stop or stop <= 0 or stop >= entry:
            return BuyDecision(False, 0.0, stop, target, 0.0,
                               "stop must be a price below entry (structural invalidation)")
        r_pct = (entry - stop) / entry * 100.0
        notes = []
        if r_pct > self.max_stop_distance_pct:
            return BuyDecision(False, 0.0, stop, target, r_pct,
                               f"stop {r_pct:.1f}% away > {self.max_stop_distance_pct:g}% "
                               f"— no clean invalidation, skip")
        if r_pct < self.min_stop_distance_pct:
            stop = entry * (1 - self.min_stop_distance_pct / 100.0)
            r_pct = self.min_stop_distance_pct
            notes.append(f"stop widened to {self.min_stop_distance_pct:g}% (was noise-tight)")

        # ── R-based size ────────────────────────────────────
        risk_usd = equity * self.risk_per_trade_pct / 100.0
        usd = risk_usd / (r_pct / 100.0)

        pos_cap = equity * self.max_position_pct / 100.0
        cur_val = 0.0
        if held:
            cur_val = pf.positions[asset].qty * (prices.get(asset + "USDT") or entry)
        room = pos_cap - cur_val
        if room <= 0:
            return BuyDecision(False, 0.0, stop, target, r_pct,
                               f"position already at the {self.max_position_pct:g}% cap")
        if usd > room:
            usd = room
            notes.append(f"capped at {self.max_position_pct:g}% position ceiling "
                         f"(risk < {self.risk_per_trade_pct:g}%)")

        min_cash = equity * self.cash_reserve_pct / 100.0
        spendable = pf.cash - min_cash
        if spendable <= 0:
            return BuyDecision(False, 0.0, stop, target, r_pct,
                               f"cash reserve floor ({self.cash_reserve_pct:g}%)")
        if usd > spendable:
            usd = spendable
            notes.append(f"capped at {self.cash_reserve_pct:g}% cash reserve")

        if usd < self.min_order_usd:
            return BuyDecision(False, 0.0, stop, target, r_pct,
                               f"sized ${usd:.2f} < min order ${self.min_order_usd:g}")

        return BuyDecision(True, usd, stop, target, r_pct, "; ".join(notes) or "ok")


def _rr(r):
    return round(r, 2) if r is not None else None
