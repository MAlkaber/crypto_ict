"""One full trading cycle: scan → mark → protective exits → agent → persist → report."""
from __future__ import annotations

import json
import time

from . import binance, regime as regime_mod
from .agent import TradingAgent
from .broker import make_broker
from .notify import Notifier, utc_now_iso
from .portfolio import Portfolio
from .risk import RiskEngine
from .scanner import scan


def run_cycle(cfg, *, live: bool = False, verbose: bool = False,
              allow_agent: bool = True, flatten: bool = False) -> dict:
    t0 = time.time()
    pf = Portfolio.load_or_create(cfg.state_path, cfg.risk.starting_paper_balance_usd)
    risk = RiskEngine(cfg)
    notifier = Notifier(cfg)

    if verbose:
        print("· scanning universe …")
    candidates = scan(cfg, verbose=verbose)
    frames = {c["symbol"]: c.pop("_frames") for c in candidates if "_frames" in c}

    prices = binance.all_prices()
    pf.mark(prices)
    equity_before = pf.equity(prices)

    if verbose:
        print("· assessing market regime …")
    regime = regime_mod.assess(cfg, candidates)

    broker = make_broker(cfg, pf, prices, live)

    # ── protective exits (before the agent) ─────────────────
    forced = risk.forced_exits(pf, prices)
    forced_done = []
    for order in forced:
        fill = broker.sell(order["asset"], None, reason=order["reason"])
        if fill.ok:
            forced_done.append({**order, "usd": round(fill.usd, 2), "price": fill.price})
            if verbose:
                print(f"  🛑 exit {order['asset']}: {order['reason']}")

    # ── manual flatten (from Telegram /flatten) ─────────────
    flat_done = []
    if flatten:
        for asset in list(pf.positions):
            fill = broker.sell(asset, None, reason="manual flatten")
            if fill.ok:
                flat_done.append({"asset": asset, "reason": "manual flatten",
                                  "usd": round(fill.usd, 2), "price": fill.price})
        if verbose and flat_done:
            print(f"  ⚑ flattened {len(flat_done)} positions to cash")

    # ── the agent ───────────────────────────────────────────
    halted = risk.halted(pf, pf.equity(prices))
    skip_agent = halted or not allow_agent or flatten
    agent_out = {"iterations": 0, "trades": [], "agent_summary": "", "market_view": regime["label"],
                 "stopped": "skipped"}
    if skip_agent:
        agent_out["agent_summary"] = (
            "Daily-loss circuit breaker active — no new buys." if halted
            else "Flattened to cash on request." if flatten
            else "Bot paused — protective exits only, no new buys.")
        if verbose:
            print(f"  ⚠ agent skipped ({agent_out['agent_summary']})")
    else:
        agent = TradingAgent(cfg)
        agent_out = agent.run(pf, broker, prices, candidates, regime, frames, verbose=verbose)

    # ── persist + report ────────────────────────────────────
    prices = binance.all_prices()
    pf.mark(prices)
    pf.save(cfg.state_path)
    equity_after = pf.equity(prices)

    weights = {}
    positions_after = []
    for asset, pos in pf.positions.items():
        px = prices.get(asset + "USDT")
        if not px:
            continue
        val = pos.qty * px
        weights[asset] = round(val / equity_after * 100, 1) if equity_after else 0.0
        positions_after.append({
            "symbol": asset + "USDT", "value_usd": round(val, 2),
            "weight_pct": weights[asset],
            "pnl_pct": round((px / pos.avg_cost - 1) * 100, 2) if pos.avg_cost else 0.0,
        })

    report = {
        "time": utc_now_iso(),
        "mode": "live" if live else cfg.mode,
        "regime": regime["label"],
        "regime_detail": regime,
        "risk_budget": regime["risk_budget"],
        "bull_run": regime.get("bull_run", {}),
        "equity": round(equity_after, 2),
        "equity_before": round(equity_before, 2),
        "cash": round(pf.cash, 2),
        "day_pnl_pct": round(risk.daily_loss_pct(pf, equity_after), 2),
        "daily_loss_halt": halted,
        "paused": not allow_agent,
        "forced_exits": forced_done + flat_done,
        "trades": agent_out["trades"],
        "weights": weights,
        "positions_after": positions_after,
        "agent_summary": agent_out["agent_summary"],
        "market_view": agent_out.get("market_view", ""),
        "agent_iterations": agent_out["iterations"],
        "agent_stopped": agent_out["stopped"],
        "elapsed_sec": round(time.time() - t0, 1),
    }

    cfg.report_path.write_text(json.dumps(report, indent=2, default=float))
    notifier.publish_signals(report)
    notifier.cycle(report)
    return report
