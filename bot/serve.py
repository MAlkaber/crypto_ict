"""24/7 service: trading loop + Telegram control, one process.

    python -m bot.serve            # paper
    python -m bot.serve --live     # real orders (triple-gated)

Runs anywhere with outbound HTTPS — no inbound ports. Designed for a
managed host (Railway / Render / Fly). Mount a persistent volume at
./data so the portfolio and control state survive restarts.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import traceback

from .config import Config
from .control import Controller
from .cycle import run_cycle
from .notify import Notifier
from .telegram_bot import TelegramBot


def _trading_loop(cfg, ctl: Controller, notifier: Notifier):
    interval = cfg.cycle.interval_minutes * 60
    while not ctl.stop.is_set():
        flatten = ctl.consume_flatten()
        try:
            with ctl.lock:
                run_cycle(cfg, live=ctl.live, verbose=True,
                          allow_agent=not ctl.paused, flatten=flatten)
            ctl.cycle_count += 1
            ctl.last_cycle_at = time.time()
            ctl.last_error = None
        except Exception as exc:
            ctl.last_error = repr(exc)
            traceback.print_exc()
            try:
                notifier.message(f"⚠ cycle error: {exc!r}\nretrying next interval")
            except Exception:
                pass
        # wait for the interval, an early trigger, or shutdown
        if ctl.run_now.wait(timeout=interval):
            ctl.run_now.clear()


def main(argv=None):
    p = argparse.ArgumentParser(prog="bot.serve")
    p.add_argument("--config")
    p.add_argument("--live", action="store_true")
    args = p.parse_args(argv)

    cfg = Config(args.config)
    if not cfg.anthropic_api_key:
        sys.exit("error: ANTHROPIC_API_KEY not set")
    if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
        sys.exit("error: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set "
                 "(needed for bot.serve — use `python main.py loop` for no-Telegram)")

    live = args.live
    if live and not (cfg.mode == "live" and cfg.live_confirmed
                     and cfg.binance_api_key and cfg.binance_api_secret):
        sys.exit("error: --live needs mode: live + I_UNDERSTAND_LIVE_TRADING=yes + BINANCE keys")

    ctl = Controller(cfg, live=live)
    notifier = Notifier(cfg)
    tg = TelegramBot(cfg, ctl)

    loop = threading.Thread(target=_trading_loop, args=(cfg, ctl, notifier),
                            name="trading-loop", daemon=True)
    loop.start()
    print(f"serve: trading loop every {cfg.cycle.interval_minutes} min "
          f"({'LIVE' if live else cfg.mode}); Telegram control active")

    try:
        tg.run()                       # blocks on the long-poll loop
    except KeyboardInterrupt:
        pass
    finally:
        ctl.stop.set()
        ctl.run_now.set()
        try:
            tg.send("🔴 bot shutting down")
        except Exception:
            pass
        loop.join(timeout=10)


if __name__ == "__main__":
    main()
