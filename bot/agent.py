"""The Claude-driven trading agent.

The agent is handed: the current portfolio, the market regime, and a
pre-screened + pre-ranked shortlist (halal screen already enforced in
code — it is impossible for a blocked coin to reach here). It reasons
with the full price-action / ICT toolkit, chooses a playbook that fits
the regime, and calls `buy` / `sell`. Every buy is still clamped by the
RiskEngine; every sell is spot-only (reduces a holding you own).

Manual tool-use loop against `client.messages.create` — see the
claude-api skill. No leverage, no shorting, no margin anywhere.
"""
from __future__ import annotations

import json

import anthropic

from . import binance
from .indicators import klines_to_df
from .risk import RiskEngine
from .sessions import session_levels
from .structure import compute_ict, compute_structure, ict_summary, structure_summary

SYSTEM = """\
You are the portfolio manager for a HALAL, SPOT-ONLY swing-trading bot on Binance.
You trade the ICT / smart-money way, top-down across timeframes.

Hard rules (enforced in code — do not fight them):
- Long only. You BUY with USDT cash and SELL coins you already hold. No shorting,
  no margin, no leverage, no derivatives, ever. (Bearish ICT reads only ever mean
  "stay out" or "trim" — never "short".)
- The universe is already halal-screened (no interest/lending, yield, gambling,
  adult, stablecoins). You cannot trade anything else.
- POSITION SIZE IS NOT YOUR CHOICE — it is R-based. Every `buy` needs a `stop`
  (your structural invalidation price) and a `target`. The RiskEngine sizes the
  order so a hit to that stop loses ~{risk_per_trade_pct}% of equity (= 1R), then
  caps it at {max_position_pct}% of equity / {cash_reserve_pct}% cash reserve /
  {max_open_positions} positions. So: a TIGHT structural stop => a BIGGER position;
  a loose one => smaller. If invalidation is more than {max_stop_distance_pct}%
  from entry, the trade is rejected — that's not an A+ setup, pick a better POI.
- Managed exits already run BEFORE you each cycle: structural stop (your level,
  trailed up as price runs), partial {partial_tp_fraction} booked at
  +{partial_tp_at_r}R with stop to breakeven, and a {disaster_stop_pct}% disaster
  backstop. You MAY still exit early when the thesis/structure breaks.

TOP-DOWN METHOD (weekly -> 4H -> 15m), the timeframes are connected:
1. WEEKLY (htf): establish directional bias and the weekly draw on liquidity
   (which side — buy-side above old highs / sell-side below old lows — price is
   likely reaching for). Only look for longs when weekly bias is up or a weekly
   discount reversal is in play. If weekly is bearish and mid-range: no trade.
2. 4H (setup): find the point of interest in the direction of weekly bias —
   an unmitigated bullish order block, a 4H FVG, or a breaker — sitting in the
   DISCOUNT half of the dealing range. That POI is your entry zone.
3. 15m (entry): require confirmation at/inside the 4H POI — a liquidity sweep of
   a local low, then a 15m change of character (CHoCH) and ideally a 15m FVG to
   enter against. No 15m confirmation -> either skip or (bull regime only) take a
   smaller "POI-touch" starter and add on confirmation next cycle.
Invalidation = a decisive move/close beyond the POI (below the order block / the
sweep low on the relevant timeframe). Target = the next opposing liquidity pool
(old high, equal highs, HTF FVG).

REGIME overlay — you get strong_bull / bull / neutral / bear and a risk_budget
multiplier; scale deployment by it:
  strong_bull: press continuation, POI-touch starters OK, deploy most cash.
  bull:        A/A+ setups with 15m confirmation; discount only; don't chase.
  neutral:     A+ only (weekly discount + sweep + CHoCH), small size, lots of cash.
  bear:        capital preservation, default NO new buys; at most one tiny A+
               weekly-discount reversal; trim weak/extended holdings into strength.
Never chase price that is extended in premium with no pullback and no sweep.

ICT TOOLKIT — you know and reason in ALL of these; per coin/timeframe you get
the machine-computed ones, and you infer the rest from the candles in get_coin:
- Structure: external vs internal structure, structure points, BOS, MSS/CHoCH,
  LRLR / HRLR (low/high resistance liquidity runs), multi-timeframe alignment.
- PD arrays: FVG (BISI/SIBI), IFVG, BPR, NWOG/NDOG, liquidity void, order block,
  breaker, mitigation block, propulsion block, rejection block, OTE / SD, the
  PD-array matrix (which array price is drawing to next).
- Liquidity: buy-side / sell-side, major vs minor, engineered liquidity, equal
  highs/lows, liquidity pools, liquidity sweep / stop raid, turtle soup.
- Time & price: trading sessions, killzones, Silver Bullet, CBDR, macros,
  Candle Range Theory (CRT), Quarterly Theory, Power of 3 (accumulation-
  manipulation-distribution), MMXM (market-maker buy/sell model).
- Delivery: IPDA price delivery C-E-R-R (consolidation → expansion → retracement
  → reversal), Change in State of Delivery (CISD), displacement, daily bias.
You DECIDE which of these matter in the current regime and say which you used.
A quarterly review re-weights concepts by realised results — so tag every order
with the concepts it relied on (the `concepts` field).

You get per timeframe: market_structure, last_break (BOS/CHoCH), premium/discount
zone + range_pos_pct, bullish FVG / order block below, liquidity sweep, equal
highs/lows, plus an ICT block (dealing range, OTE, displacement, CRT, turtle
soup, liquidity void, BPR, PD-array matrix). get_coin adds a fresh 15m read with
session highs/lows, current killzone and recent sweeps.

BULL-RUN PROTOCOL — regime.bull_run tells you active/phase/derisk:
  early:    accumulate aggressively, buy discount POIs & BOS retraces, rotate
            into leaders, let winners run to HTF liquidity.
  mid:      stay invested, add only on clean 4H POI retests with 15m CISD.
  late:     take partial profits into buy-side liquidity, tighten theses, favour
            majors, no chasing, raise effective cash.
  euphoria: scale OUT into parabolic strength / equal highs, smallest new risk,
            watch BTC weekly for MMXM distribution / CHoCH.
  derisk=true: treat regime as neutral at best — trim, hold cash, no new adds
            until BTC reclaims its weekly structure.

Process:
1. get_portfolio. Review each open position against its ORIGINAL timeframe thesis
   — HTF bias still valid? structure still intact? trim/exit what broke or hit a
   liquidity target.
2. Scan the shortlist's weekly+4H reads. get_coin on the best few (<= 8) — this
   also pulls fresh 15m for the entry check.
3. buy / sell. Every `buy` needs `stop` and `target` PRICES (not %). `stop` = the
   real structural invalidation (below the 4H order block / the 15m sweep low) —
   put it where the idea is wrong, not at a round number. `target` = the next
   opposing liquidity pool. The thesis MUST name: weekly bias + draw, the 4H POI
   zone, the 15m trigger (or why you took a starter), and restate stop + target.
   Fill `concepts` with the ICT concepts you actually used (e.g.
   ["weekly_discount","4h_order_block","15m_CISD","turtle_soup","OTE"]).
   The result tells you the sized amount and R distance — if it says the stop is
   too far, you picked a poor POI; find a tighter structural level or skip.
4. finish with a concise summary and your market view.

Keep total tool calls under the cap. Be decisive. Cash is a position."""


def _compact_candidate(c: dict) -> dict:
    f = c.get("features", {})
    mtf = c.get("mtf", {})
    order = c.get("mtf_order", ["1w", "4h", "1d"])
    return {
        "symbol": c["symbol"],
        "trend_score": f.get("trend_score"),
        "price": f.get("price"),
        "ret_7d": f.get("ret_7d"),
        "ret_30d": f.get("ret_30d"),
        "rsi14": f.get("rsi14"),
        "vol_ratio_20d": f.get("vol_ratio_20d"),
        "pct_from_90d_high": f.get("pct_from_90d_high"),
        "ema50_gt_ema200": f.get("ema50_gt_ema200"),
        "structure_by_tf": {tf: structure_summary(mtf.get(tf, {})) for tf in order},
        "ict_by_tf": {tf: ict_summary(c.get("ict", {}).get(tf, {})) for tf in order},
    }


TOOLS = [
    {
        "name": "get_portfolio",
        "description": "Cash, equity, drawdown from the equity high-water mark, whether "
                       "the circuit breaker is active, and every open position with its "
                       "stop / target / current R multiple / partial-taken flag.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_coin",
        "description": "Full detail for one shortlisted symbol: indicators, 24h stats, "
                       "the full structure breakdown on weekly / 4H / daily, a FRESH 15m "
                       "structure read + recent 15m candles for the entry check, and the "
                       "last candles of each higher timeframe.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "e.g. SOLUSDT"}},
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
    {
        "name": "buy",
        "description": "Open or add to a long. You do NOT set the size — provide `stop` "
                       "and `target` prices and the RiskEngine sizes it R-based (tighter "
                       "stop = bigger position) and may reject it. The result gives the "
                       "actual amount, stored stop, and R distance.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "stop": {"type": "number",
                         "description": "structural invalidation PRICE, below entry"},
                "target": {"type": "number",
                           "description": "first target PRICE — the next opposing liquidity pool"},
                "thesis": {"type": "string",
                           "description": "weekly bias+draw, 4H POI zone, 15m trigger, stop, target"},
                "concepts": {"type": "array", "items": {"type": "string"},
                             "description": "ICT concepts relied on, for the quarterly review"},
            },
            "required": ["symbol", "stop", "target", "thesis", "concepts"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sell",
        "description": "Sell a coin you hold. `amount` is 'all', a percent like '50%', "
                       "or a USDT figure. Spot only — you can only sell what you own.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "amount": {"type": "string", "description": "'all' | '50%' | '250'"},
                "reason": {"type": "string"},
                "concepts": {"type": "array", "items": {"type": "string"},
                             "description": "ICT concepts behind the exit (optional)"},
            },
            "required": ["symbol", "amount", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "finish",
        "description": "End the cycle. Provide a summary of actions and your market view.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "market_view": {"type": "string"},
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
    },
]


class TradingAgent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        self.risk = RiskEngine(cfg)

    # ── tool handlers ───────────────────────────────────────
    def _portfolio_view(self, pf, prices) -> dict:
        equity = pf.equity(prices)
        return {
            "cash": round(pf.cash, 2),
            "equity": round(equity, 2),
            "invested_pct": round((1 - pf.cash / equity) * 100, 1) if equity else 0.0,
            "open_positions": self._positions_view(pf, prices),
            "drawdown_from_peak_pct": round(pf.drawdown_pct(equity), 2),
            "rolling_7d_pct": round(pf.rolling_return_pct(equity, 7.0), 2),
            "circuit_breaker": {"active": pf.halted, "reason": pf.halt_reason},
            "max_open_positions": self.risk.max_open_positions,
            "risk_per_trade_pct": self.risk.risk_per_trade_pct,
        }

    @staticmethod
    def _positions_view(pf, prices) -> list[dict]:
        out = []
        for asset, pos in pf.positions.items():
            px = prices.get(asset + "USDT")
            if not px:
                continue
            r = pos.r_multiple(px)
            out.append({
                "symbol": asset + "USDT",
                "qty": pos.qty,
                "avg_cost": round(pos.avg_cost, 6),
                "price": px,
                "value_usd": round(pos.qty * px, 2),
                "pnl_pct": round((px / pos.avg_cost - 1) * 100, 2) if pos.avg_cost else 0.0,
                "stop": round(pos.stop, 6) if pos.stop else None,
                "target": round(pos.target, 6) if pos.target else None,
                "r_multiple": round(r, 2) if r is not None else None,
                "partial_taken": pos.partial_done,
                "concepts": pos.concepts,
            })
        return out

    @staticmethod
    def _ohlc(df, n: int) -> list:
        out = []
        for ts, row in df.iloc[-n:].iterrows():
            stamp = str(ts.date()) if getattr(ts, "hour", 0) == 0 else str(ts)[:16]
            out.append([stamp, round(float(row["open"]), 8), round(float(row["high"]), 8),
                        round(float(row["low"]), 8), round(float(row["close"]), 8)])
        return out

    def _coin_detail(self, symbol: str) -> dict:
        symbol = symbol.upper()
        c = self.by_symbol.get(symbol)
        if not c:
            return {"error": f"{symbol} is not in the screened shortlist"}
        tf_frames = self.frames.get(symbol, {})

        # fresh 15m (entry timeframe) — pulled live, only for coins the agent studies
        entry = {}
        try:
            d15 = klines_to_df(binance.klines(symbol, self.entry_tf, self.entry_lb))
            lb = min(self.entry_lb, 192)
            entry = {
                "timeframe": self.entry_tf,
                "structure": compute_structure(d15, lb),
                "ict": compute_ict(d15, lb),
                "session_liquidity": session_levels(d15),
                "recent_candles_ohlc": self._ohlc(d15, 32),
            }
        except binance.BinanceError as exc:
            entry = {"timeframe": self.entry_tf, "error": str(exc)}

        candles_by_tf = {}
        for tf, df in tf_frames.items():
            candles_by_tf[tf] = self._ohlc(df, 24 if tf != "1d" else 40)

        return {
            "symbol": symbol,
            "screen_reasons": c.get("screen"),
            "quote_volume_24h": c.get("quote_volume_24h"),
            "change_24h_pct": c.get("change_24h_pct"),
            "features": c.get("features"),
            "structure_by_tf": c.get("mtf", {"1d": c.get("structure")}),
            "ict_by_tf": c.get("ict", {"1d": c.get("ict_1d")}),
            "entry_timeframe": entry,
            "candles_ohlc": candles_by_tf,
        }

    def _do_buy(self, pf, broker, prices, symbol, stop, target, thesis, concepts) -> dict:
        symbol = symbol.upper()
        asset = symbol[:-4] if symbol.endswith("USDT") else symbol
        if symbol not in self.by_symbol:
            return {"ok": False, "reason": f"{symbol} not in screened shortlist"}
        entry = prices.get(symbol)
        if not entry:
            return {"ok": False, "reason": "no live price"}
        decision = self.risk.check_buy(pf, prices, asset, entry, float(stop), float(target))
        if not decision.ok:
            return {"ok": False, "reason": decision.reason}
        fill = broker.buy(asset, decision.usd, reason=thesis)
        rec = fill.as_dict()
        if fill.ok:
            pos = pf.positions.get(asset)
            if pos:
                pos.target = float(target)
                if not pos.init_stop:            # first entry defines 1R
                    pos.init_stop = decision.stop
                pos.stop = max(pos.stop, decision.stop) if pos.stop else decision.stop
                pos.concepts = list(dict.fromkeys((pos.concepts or []) + (concepts or [])))
            self.trades.append({**rec, "thesis": thesis, "concepts": concepts or [],
                                "stop": decision.stop, "target": float(target),
                                "r_pct": round(decision.r_pct, 2),
                                "regime": self.regime_label, "bull_phase": self.bull_phase})
        rec["risk_note"] = decision.reason
        rec["stop"] = decision.stop
        rec["r_pct"] = round(decision.r_pct, 2)
        return rec

    def _do_sell(self, pf, broker, prices, symbol, amount, reason, concepts=None) -> dict:
        symbol = symbol.upper()
        asset = symbol[:-4] if symbol.endswith("USDT") else symbol
        pos = pf.positions.get(asset)
        if not pos or pos.qty <= 0:
            return {"ok": False, "reason": f"no {asset} position"}
        amount = str(amount).strip().lower()
        px = prices.get(asset + "USDT") or pos.avg_cost
        if amount in ("all", "100%", "", "none"):
            usd = None
        elif amount.endswith("%"):
            usd = pos.qty * px * float(amount[:-1]) / 100.0
        else:
            usd = float(amount)
        fill = broker.sell(asset, usd, reason=reason)
        rec = fill.as_dict()
        if fill.ok:
            self.trades.append({**rec, "thesis": reason, "concepts": concepts or [],
                                "regime": self.regime_label, "bull_phase": self.bull_phase})
        return rec

    # ── main loop ───────────────────────────────────────────
    def run(self, pf, broker, prices, candidates: list[dict], regime: dict,
            frames: dict, verbose: bool = False) -> dict:
        self.by_symbol = {c["symbol"]: c for c in candidates}
        self.frames = frames
        self.trades: list[dict] = []
        tf = getattr(self.cfg, "timeframes", None)
        self.entry_tf = getattr(tf, "entry", "15m") if tf else "15m"
        self.entry_lb = getattr(tf, "entry_lookback", 192) if tf else 192
        self.regime_label = regime.get("label", "")
        self.bull_phase = (regime.get("bull_run") or {}).get("phase")

        r = self.cfg.risk
        system = SYSTEM.format(
            risk_per_trade_pct=r.risk_per_trade_pct, max_position_pct=r.max_position_pct,
            max_open_positions=r.max_open_positions, cash_reserve_pct=r.cash_reserve_pct,
            max_stop_distance_pct=r.max_stop_distance_pct,
            partial_tp_fraction=f"{float(r.partial_tp_fraction):.0%}",
            partial_tp_at_r=r.partial_tp_at_r, disaster_stop_pct=r.disaster_stop_pct,
        )

        htf = candidates[0].get("mtf_order", ["1w", "4h", "1d"]) if candidates else ["1w", "4h", "1d"]
        intro = {
            "timeframes": {"htf_bias": htf[0], "setup_poi": htf[1], "context": "1d",
                           "entry_confirmation": self.entry_tf + " (via get_coin)"},
            "regime": regime,
            "portfolio": self._portfolio_view(pf, prices),
            "shortlist": [_compact_candidate(c) for c in candidates],
            "base_currency": self.cfg.base_currency,
        }
        messages = [{
            "role": "user",
            "content": "Run this cycle. Data below.\n\n" + json.dumps(intro, default=str),
        }]

        tools = list(TOOLS)
        if getattr(self.cfg.news, "enabled", False):
            tools = tools + [{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}]

        max_iter = int(self.cfg.cycle.max_agent_iterations)
        summary, market_view, stopped = "", regime.get("label", ""), "iteration cap"

        for i in range(max_iter):
            resp = self.client.messages.create(
                model=self.cfg.model,
                max_tokens=16000,
                system=system,
                thinking={"type": "adaptive"},
                output_config={"effort": self.cfg.effort},
                tools=tools,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "pause_turn":
                continue
            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                for b in resp.content:
                    if getattr(b, "type", None) == "text" and b.text.strip():
                        summary = b.text.strip()
                if resp.stop_reason == "max_tokens":
                    stopped = "hit max_tokens"
                    messages.append({"role": "user", "content":
                                     "You were cut off. Place any final orders, then call finish."})
                    continue
                stopped = "agent ended turn"
                break

            results, done = [], False
            for tu in tool_uses:
                out = self._dispatch(tu, pf, broker, prices)
                if verbose:
                    print(f"  · {tu.name}({json.dumps(tu.input, default=str)[:90]}) -> "
                          f"{json.dumps(out, default=str)[:120]}")
                results.append({
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": json.dumps(out, default=str),
                })
                if tu.name == "finish":
                    done = True
                    summary = tu.input.get("summary", "")
                    market_view = tu.input.get("market_view", market_view)
                    stopped = "agent called finish"
            messages.append({"role": "user", "content": results})
            if done:
                break

        return {
            "iterations": i + 1,
            "stopped": stopped,
            "trades": self.trades,
            "agent_summary": summary,
            "market_view": market_view,
        }

    def _dispatch(self, tu, pf, broker, prices):
        name, inp = tu.name, tu.input
        if name == "get_portfolio":
            return self._portfolio_view(pf, prices)
        if name == "get_coin":
            return self._coin_detail(inp["symbol"])
        if name == "buy":
            return self._do_buy(pf, broker, prices, inp["symbol"], inp["stop"],
                                inp["target"], inp["thesis"], inp.get("concepts", []))
        if name == "sell":
            return self._do_sell(pf, broker, prices, inp["symbol"], inp["amount"],
                                 inp["reason"], inp.get("concepts", []))
        if name == "finish":
            return {"ok": True}
        return {"error": f"unknown tool {name}"}
