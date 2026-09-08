# halal-crypto-bot

A **spot-only, long-only** crypto trading bot for Binance. A mechanical halal
screen picks the tradeable universe; a Claude agent then trades it the ICT /
smart-money way — top-down across **weekly → 4H → 15m** — with regime awareness
and a bull-run protocol. All risk limits and protective exits are enforced in
code, outside the agent.

> ⚠️ **Not financial advice and not a fatwa.** The halal screen is best-effort
> (see `blocklist.yaml`). The ICT structure detection is a best-effort daily/
> intraday approximation. Paper-trade first. Review `python main.py screen`
> before ever going live.

## What it does

```
universe ──► halal screen ──► liquidity + momentum rank ──► shortlist (~40)
                                                                │
                    weekly + 4H structure & ICT arrays  ◄───────┤
                                                                ▼
   market regime  ──►   Claude agent (ICT top-down)   ──►  buy / sell (spot)
   + bull-run read       · weekly bias & draw on liquidity      │
                         · 4H point of interest (OB/FVG/breaker) │
                         · 15m entry: sweep + CISD/CHoCH + FVG   │
                                                                ▼
        RiskEngine clamps every buy · protective exits run first
                                                                ▼
              portfolio.json  +  signal feed (data/signals.json)
```

- **No leverage, no margin, no shorting, no futures — anywhere.** A bearish read
  only ever means "stay out" or "trim".
- The halal screen (lending/interest, yield, gambling, adult, stablecoins, …) is
  enforced in code — a blocked coin can never reach the agent.

## Setup

```bash
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt
copy .env.example .env                               # add ANTHROPIC_API_KEY
```

## Commands

```bash
python main.py screen [-v]     # halal screen verdicts
python main.py scan   [-v]     # ranked shortlist + weekly/4H/daily structure
python main.py regime [--deep] # market regime + bull-run read
python main.py status          # portfolio state
python main.py once            # run one trading cycle (paper)
python main.py loop            # a cycle every cycle.interval_minutes
python main.py serve           # 24/7: trading loop + two-way Telegram control
python main.py review [--months 6] [--agent]   # quarterly concept × regime review
python main.py once --live     # REAL orders — see below
python main.py reset --yes     # wipe portfolio state
```

### Run it 24/7 + control from Telegram

`python main.py serve` runs the trading loop **and** a two-way Telegram bot in one
process — no inbound ports, no webhook. Commands (accepted only from your
`TELEGRAM_CHAT_ID`):

```
/status /positions /report /pnl /settings      monitor
/scan /regime /review                          analysis
/runcycle /pause /resume /flatten YES           operate
```

`/pause` stops new buys but keeps protective stops running. Host it on Railway or
Fly (no server to manage) — see **[DEPLOY.md](DEPLOY.md)**.

## Configuration

- **`config.yaml`** — mode, model/effort, cycle interval, universe filters,
  `timeframes` (htf/setup/entry), and the `risk` block (R-based sizing, structural
  stop bounds, partial/trail rules, drawdown circuit breaker).
- **`blocklist.yaml`** — the halal screen: explicit blocks, keyword patterns,
  optional CoinGecko category checks.

## How the agent decides

1. **Regime first.** `regime.py` reads BTC/ETH daily + weekly and shortlist
   breadth → `strong_bull / bull / neutral / bear`, a `risk_budget` multiplier,
   and a `bull_run` block (`active`, `phase` = early/mid/late/euphoria, `derisk`).
2. **Top-down ICT.** Weekly bias + draw on liquidity → 4H point of interest in
   the discount half of the dealing range → 15m confirmation (liquidity sweep +
   change in state of delivery + FVG). Machine-computed inputs per timeframe:
   market structure, BOS/CHoCH, premium/discount, FVG, order block, liquidity
   sweep, equal highs/lows, dealing range, OTE, displacement, CRT, turtle soup,
   liquidity void, BPR, PD-array matrix. 15m also gets session highs/lows,
   killzones and recent sweeps.
3. The agent **chooses which concepts matter for the regime**, tags every order
   with the concepts it used, and gives each `buy` a **structural `stop` and
   `target` price** plus a full thesis (bias, POI, trigger, invalidation, target).
4. **RiskEngine** (`risk.py`) — sizing is **R-based**: the position is sized so a
   hit to the agent's structural stop loses `risk_per_trade_pct` of equity (=1R),
   then capped by `max_position_pct` / cash reserve / max positions. A stop more
   than `max_stop_distance_pct` away is rejected (not an A+ setup). Managed exits
   run *before* the agent each cycle: the structural stop (trailed up as price
   runs), a partial booked at `+partial_tp_at_r`R with stop → breakeven, and a
   `disaster_stop_pct` backstop. New buys halt when equity is `max_drawdown_pct`
   below its **high-water mark** or down `max_rolling_7d_loss_pct` over 7 days.

## Quarterly review (which concepts work in which market)

`python main.py review` pairs BUY→SELL fills into closed trades and aggregates
realised results **by ICT concept, by regime, and by bull-run phase**. Run it
after 3–6 months (≥ ~30 closed trades per bucket to mean anything). `--agent`
asks Claude to turn the numbers into a Keep / Adjust / Drop re-weighting.

## Signal feed (copy trading)

Every cycle writes `data/signals.json` (latest) and appends
`data/signals_log.jsonl` (history) with each action, its thesis, concepts,
regime, and risk levels. Followers read the feed to mirror trades manually or
with their own copy of the bot. **Native Binance Copy Trading is futures-only
and cannot be driven from here.** Running a service others' funds follow may be
regulated investment advice in your jurisdiction — that's on you.

## Live trading (triple-gated)

All three required:
1. `config.yaml` → `mode: live`
2. `.env` → `I_UNDERSTAND_LIVE_TRADING=yes`
3. `python main.py once --live` (or `loop --live`)

Binance API key: **spot trading only** — no withdrawals, no margin/futures, IP-restricted.

## Layout

| file | role |
|---|---|
| `bot/config.py` | config + blocklist + `.env` loading |
| `bot/binance.py` | thin spot REST client (public + signed) |
| `bot/screener.py` | halal screen (blocklist + keywords + optional CoinGecko) |
| `bot/indicators.py` | EMA/RSI/ATR + momentum feature vector |
| `bot/structure.py` | market structure + ICT arrays (any timeframe) |
| `bot/sessions.py` | 15m session / killzone / draw-on-liquidity |
| `bot/regime.py` | market regime + bull-run detection |
| `bot/scanner.py` | universe → screen → rank → multi-timeframe enrich |
| `bot/risk.py` | position sizing limits + protective exits |
| `bot/portfolio.py` | cash / positions / trade log / equity curve (JSON) |
| `bot/broker.py` | `PaperBroker` (simulated) / `LiveBroker` (real spot) |
| `bot/agent.py` | the Claude ICT trading agent (tool-use loop) |
| `bot/review.py` | quarterly concept × regime performance review |
| `bot/notify.py` | console / Telegram push / signal feed |
| `bot/cycle.py` | one full cycle end to end |
| `bot/control.py` | shared loop/bot state (pause, triggers) |
| `bot/telegram_bot.py` | two-way Telegram control (long-polling) |
| `bot/serve.py` | 24/7 service = loop + Telegram, one process |
| `main.py` | CLI |
