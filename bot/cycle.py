"""One full trading cycle: scan → mark → protective exits → agent → persist → report."""
from __future__ import annotations

import json
import time

from . import binance, regime as regime_mod
from .agent import TradingAgent
from .broker import make_broker
from .indicators import compute_features, klines_to_df
from .notify import Notifier, utc_now_iso
from .portfolio import Portfolio
from .risk import RiskEngine
from .scanner import scan
from .structure import compute_ict, compute_structure


def _add_held_to_shortlist(cfg, pf, candidates, frames, verbose):
    """Append minimal candidate entries for coins we hold but that fell off the
    ranked shortlist, so the agent can still review and manage them."""
    have = {c["symbol"] for c in candidates}
    tf = getattr(cfg, "timeframes", None)
    htf = getattr(tf, "htf", "1w") if tf else "1w"
    setup = getattr(tf, "setup", "4h") if tf else "4h"
    for asset in pf.positions:
        sym = asset + "USDT"
        if sym in have:
            continue
        try:
            d1 = klines_to_df(binance.klines(sym, cfg.universe.kline_interval,
                                             cfg.universe.kline_lookback))
        except binance.BinanceError:
            continue
        if len(d1) < 50:
            continue
        c = {"symbol": sym, "base": asset, "held_only": True,
             "features": compute_features(d1), "structure": compute_structure(d1),
             "ict_1d": compute_ict(d1), "mtf": {"1d": compute_structure(d1)},
             "ict": {"1d": compute_ict(d1)}, "mtf_order": [htf, setup, "1d"],
             "screen": ["held position"], "quote_volume_24h": None, "change_24h_pct": None}
        fr = {"1d": d1}
        for name in (htf, setup):
            try:
                d = klines_to_df(binance.klines(sym, name, 400))
                if len(d) >= 30:
                    fr[name] = d
                    c["mtf"][name] = compute_structure(d, 180)
                    c["ict"][name] = compute_ict(d, 180)
            except binance.BinanceError:
                pass
        candidates.append(c)
        frames[sym] = fr
        if verbose:
            print(f"  + held {sym} added for review")


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

    # make sure the agent can also inspect coins it holds that fell off the shortlist
    _add_held_to_shortlist(cfg, pf, candidates, frames, verbose)

    if verbose:
        print("· assessing market regime …")
    regime = regime_mod.assess(cfg, candidates)

    broker = make_broker(cfg, pf, prices, live)

    # ── stop management + protective / target exits ────────
    stop_moves = risk.manage_stops(pf, prices)
    if verbose:
        for m in stop_moves:
            print(f"  ↑ {m['asset']} stop {m['old_stop']:.6g} → {m['new_stop']:.6g}")

    forced_done = []
    for order in risk.forced_exits(pf, prices):
        asset, frac = order["asset"], order.get("fraction")
        pos = pf.positions.get(asset)
        usd = None if frac is None else (pos.qty * (prices.get(asset + "USDT") or pos.avg_cost) * frac)
        fill = broker.sell(asset, usd, reason=order["reason"])
        if fill.ok:
            if frac is not None and asset in pf.positions:      # partial: lock the winner
                pf.positions[asset].partial_done = True
                pf.positions[asset].stop = max(pf.positions[asset].stop, pf.positions[asset].avg_cost)
            forced_done.append({**order, "usd": round(fill.usd, 2), "price": fill.price})
            if verbose:
                print(f"  🛑 {'trim' if frac else 'exit'} {asset}: {order['reason']}")

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
    halted = risk.update_halt(pf, pf.equity(prices))
    skip_agent = halted or not allow_agent or flatten
    agent_out = {"iterations": 0, "trades": [], "agent_summary": "", "market_view": regime["label"],
                 "stopped": "skipped"}
    if skip_agent:
        agent_out["agent_summary"] = (
            f"Circuit breaker active ({pf.halt_reason}) — no new buys." if halted
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
        "drawdown_pct": round(pf.drawdown_pct(equity_after), 2),
        "rolling_7d_pct": round(pf.rolling_return_pct(equity_after, 7.0), 2),
        "circuit_breaker": halted,
        "circuit_breaker_reason": pf.halt_reason,
        "paused": not allow_agent,
        "stop_moves": stop_moves,
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
