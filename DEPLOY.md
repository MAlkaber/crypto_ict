# Deploy — run the bot 24/7 and control it from Telegram

You don't need a server or an OS to manage. A managed host runs one process
(`python -m bot.serve`), restarts it if it crashes, and the bot talks to
Telegram over an outbound connection only (no inbound ports, no webhook, no
public URL).

---

## 1. Create the Telegram bot (2 min)

1. In Telegram, message **@BotFather** → `/newbot` → follow prompts.
   Copy the **bot token** (`123456:ABC-...`).
2. Send any message to your new bot (so it can DM you back).
3. Get your **chat id** (numeric, not `@username`): message **@userinfobot**, or
   run `python main.py tg-id --token <bot-token>` after DMing your bot once.

You now have `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Only that chat id can
command the bot.

> The `api_id` / `api_hash` from my.telegram.org are **not** used — those are for
> the MTProto client API (running a full user account). This bot uses the Bot
> API. Keep your `api_hash` private regardless.

---

## 2. Put the code on GitHub

Create a repo and push this folder. **Do not commit `.env`** (it's gitignored).

---

## 3a. Railway (easiest, ~$5/mo)

1. https://railway.app → **New Project → Deploy from GitHub repo** → pick the repo.
   Railway detects the `Dockerfile` and builds it.
2. **Variables** tab → add:
   - `ANTHROPIC_API_KEY`
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   - (live only) `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `I_UNDERSTAND_LIVE_TRADING=yes`
3. **Settings → Volumes** → add a volume mounted at `/app/data` (1 GB is plenty).
   This keeps your portfolio + trade history across restarts.
4. Deploy. Open the deploy logs — you should see
   `serve: trading loop every 240 min ...` and Telegram should DM you
   `🟢 bot online`.

Railway has no separate "worker" toggle — the `Dockerfile` `CMD` is the process.
There's no web port, which is fine.

---

## 3b. Fly.io (free allowance covers this)

```bash
# one-time
curl -L https://fly.io/install.sh | sh      # or: brew install flyctl
fly auth signup                              # or: fly auth login

# from the repo folder
fly launch --no-deploy                       # pick a unique app name; keep the Dockerfile
fly volumes create data --size 1 --region iad

fly secrets set ANTHROPIC_API_KEY=... \
                TELEGRAM_BOT_TOKEN=... \
                TELEGRAM_CHAT_ID=...
# live only:
# fly secrets set BINANCE_API_KEY=... BINANCE_API_SECRET=... I_UNDERSTAND_LIVE_TRADING=yes

fly deploy
fly logs                                      # watch it boot
```

`fly.toml` already declares one always-on VM and the `data` mount. Make sure
autostop is off: `fly scale count 1` and no `[http_service]` block (there isn't
one).

---

## 3c. Render (paid — $7/mo worker)

`render.yaml` is included (Blueprint). New → Blueprint → pick the repo → fill the
env vars it prompts for. Render's free tier only has *sleeping* web services, so
the always-on worker needs the Starter plan.

---

## 4. First run = paper

Leave `config.yaml` → `mode: paper` and deploy **without** `I_UNDERSTAND_LIVE_TRADING`.
The bot paper-trades a simulated $10k, DMs you every cycle, and responds to
commands. Watch it for a few weeks.

## 5. Going live

Only after you've reviewed `python main.py screen` and are satisfied:
1. Binance API key: **enable Spot trading only** — no withdrawals, no margin/futures. IP-allowlist it.
2. Host env vars: set `BINANCE_API_KEY`, `BINANCE_API_SECRET`,
   `I_UNDERSTAND_LIVE_TRADING=yes`.
3. `config.yaml` → `mode: live`, redeploy.
4. The container's start command must be `python -m bot.serve --live`
   (Railway: set a Custom Start Command; Fly: change the `CMD` or add
   `processes`). Without `--live` it stays paper even in live mode.

---

## Telegram commands

```
/status    /positions   /report   /pnl   /settings
/scan      /regime      /review
/runcycle  /pause       /resume    /flatten YES
```

`/pause` stops new buys but protective stops keep running. `/flatten YES` sells
everything to cash on the next cycle.

---

## Costs

| Item | Cost |
|---|---|
| Hosting | $0 (Fly) – $7/mo |
| Claude API | the real variable — ~6 agent cycles/day on the default 4h interval. Use `model: claude-sonnet-5` to cut it ~2.5×, or raise `cycle.interval_minutes`. |
| Binance API | free |

## Notes

- Only the trading loop and Telegram listener run — no database, no web server.
- State lives in `/app/data` (`portfolio_*.json`, `control.json`, `signals*.json`).
  Back it up if it matters: `fly ssh console -C "cat /app/data/portfolio_paper.json"`.
- Restarting the host is safe: pause state persists, the loop resumes on schedule.
- Logs go to stdout — view them in the host's dashboard.
