"""Quarterly (3–6 month) strategy review.

Pairs BUY→SELL fills FIFO into closed trades, then aggregates realised
results by ICT concept, by market regime and by bull-run phase — so you
(or the agent) can see which concepts actually worked in which market and
re-weight. Small samples mean little: treat anything under ~30 closed
trades per bucket as noise.
"""
from __future__ import annotations

import json
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations


def _closed_trades(trades: list[dict], fallback_r_pct: float) -> list[dict]:
    lots: dict[str, list[dict]] = defaultdict(list)
    closed = []
    for t in sorted(trades, key=lambda x: x.get("ts", 0)):
        asset, side = t.get("asset"), t.get("side")
        qty, price = float(t.get("qty", 0)), float(t.get("price", 0))
        if qty <= 0 or price <= 0:
            continue
        if side == "BUY":
            lots[asset].append({"qty": qty, "price": price, "ts": t.get("ts", 0),
                                "concepts": t.get("concepts") or [],
                                "regime": t.get("regime", "?"),
                                "bull_phase": t.get("bull_phase"),
                                "r_pct": t.get("r_pct") or fallback_r_pct})
        elif side == "SELL":
            remaining = qty
            while remaining > 1e-12 and lots[asset]:
                lot = lots[asset][0]
                take = min(remaining, lot["qty"])
                entry, exit_ = lot["price"], price
                ret = (exit_ / entry - 1) * 100
                r_denom = lot.get("r_pct") or fallback_r_pct
                hold_days = max((t.get("ts", 0) - lot["ts"]) / 86400, 0.0)
                closed.append({
                    "asset": asset,
                    "entry": round(entry, 8), "exit": round(exit_, 8),
                    "qty": round(take, 8),
                    "return_pct": round(ret, 2),
                    "R": round(ret / r_denom, 2) if r_denom else None,
                    "pnl_usd": round(take * (exit_ - entry), 2),
                    "hold_days": round(hold_days, 1),
                    "concepts": lot["concepts"],
                    "regime": lot["regime"],
                    "bull_phase": lot["bull_phase"],
                    "exit_reason": t.get("reason", ""),
                    "entry_ts": lot["ts"], "exit_ts": t.get("ts", 0),
                })
                lot["qty"] -= take
                remaining -= take
                if lot["qty"] <= 1e-12:
                    lots[asset].pop(0)
    return closed


def _stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    rets = [r["return_pct"] for r in rows]
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x <= 0]
    gross_w = sum(wins)
    gross_l = -sum(losses)
    return {
        "n": len(rows),
        "win_rate": round(len(wins) / len(rows) * 100, 1),
        "avg_return_pct": round(st.mean(rets), 2),
        "median_return_pct": round(st.median(rets), 2),
        "expectancy_R": round(st.mean([r["R"] for r in rows if r["R"] is not None]), 2)
        if any(r["R"] is not None for r in rows) else None,
        "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else None,
        "avg_hold_days": round(st.mean([r["hold_days"] for r in rows]), 1),
        "total_pnl_usd": round(sum(r["pnl_usd"] for r in rows), 2),
        "best_pct": round(max(rets), 2),
        "worst_pct": round(min(rets), 2),
    }


def _group(rows: list[dict], key) -> dict:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        k = key(r)
        for kk in (k if isinstance(k, (list, tuple, set)) else [k]):
            if kk:
                buckets[str(kk)].append(r)
    return {k: _stats(v) for k, v in sorted(buckets.items(), key=lambda kv: -len(kv[1]))}


def build_review(state_path, cfg, months: float | None = None) -> dict:
    raw = json.loads(state_path.read_text()) if state_path.exists() else {"trades": []}
    trades = raw.get("trades", [])
    if months:
        cutoff = datetime.now(timezone.utc).timestamp() - months * 30 * 86400
        trades = [t for t in trades if t.get("ts", 0) >= cutoff]

    closed = _closed_trades(trades, float(cfg.risk.disaster_stop_pct))
    concept_pairs = _group(
        closed,
        lambda r: ["+".join(sorted(p)) for p in combinations(sorted(set(r["concepts"])), 2)],
    )
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_months": months,
        "closed_trades": len(closed),
        "overall": _stats(closed),
        "by_regime": _group(closed, lambda r: r["regime"]),
        "by_bull_phase": _group(closed, lambda r: r["bull_phase"] or "not_bull_run"),
        "by_concept": _group(closed, lambda r: r["concepts"] or ["<untagged>"]),
        "by_concept_pair": {k: v for k, v in list(concept_pairs.items())[:20]},
        "by_asset": _group(closed, lambda r: r["asset"]),
        "trades": closed,
    }


def print_review(rv: dict):
    def line(name, s):
        if not s or s.get("n", 0) == 0:
            return
        print(f"  {name:<28} n={s['n']:<4} win={s['win_rate']:>5}%  "
              f"avgR={str(s.get('expectancy_R')):>6}  PF={str(s.get('profit_factor')):>6}  "
              f"avg={s['avg_return_pct']:>7}%  pnl=${s['total_pnl_usd']:>10,.0f}")

    print(f"\n═══ strategy review — {rv['closed_trades']} closed trades"
          + (f" (last {rv['window_months']:g}mo)" if rv["window_months"] else "") + " ═══")
    line("OVERALL", rv["overall"])
    for title, key in (("by regime", "by_regime"), ("by bull-run phase", "by_bull_phase"),
                       ("by concept", "by_concept"), ("by concept pair", "by_concept_pair")):
        block = rv[key]
        if block:
            print(f"\n{title}:")
            for name, s in block.items():
                line(name, s)
    if rv["closed_trades"] < 30:
        print("\n⚠  under 30 closed trades — not enough to draw conclusions yet.")


def agent_recommendation(rv: dict, cfg) -> str:
    """Optional: let Claude turn the aggregates into a re-weighting recommendation."""
    import anthropic
    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    payload = {k: rv[k] for k in ("overall", "by_regime", "by_bull_phase",
                                  "by_concept", "by_concept_pair", "closed_trades",
                                  "window_months")}
    msg = client.messages.create(
        model=cfg.model,
        max_tokens=4000,
        thinking={"type": "adaptive"},
        output_config={"effort": cfg.effort},
        system=(
            "You are reviewing a halal spot ICT swing bot's realised results. "
            "Given per-concept / per-regime / per-phase stats, say which ICT concepts "
            "and combinations to KEEP weighting, which to DOWN-weight or DROP, and which "
            "regimes each works in. Be blunt about sample size — flag buckets under ~30 "
            "trades as inconclusive. Output: 3 short sections (Keep / Adjust / Drop) plus "
            "one paragraph on regime-specific playbook tweaks. No code."
        ),
        messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
