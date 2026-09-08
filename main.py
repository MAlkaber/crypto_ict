"""halal-crypto-bot — CLI entry point.

    python main.py screen        # show the halal screen verdicts
    python main.py scan          # ranked shortlist the agent would see
    python main.py regime        # current market regime
    python main.py status        # portfolio state
    python main.py once          # run one trading cycle (paper)
    python main.py loop          # run a cycle every cycle.interval_minutes
    python main.py serve         # 24/7: trading loop + Telegram remote control
    python main.py once --live   # real orders (needs mode: live + .env confirm)
    python main.py reset --yes   # wipe portfolio state

Spot only. Long only. No leverage anywhere.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from bot.config import Config


def _die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def cmd_screen(cfg, args):
    from bot.scanner import screen_universe
    results = screen_universe(cfg)
    results.sort(key=lambda r: (r["verdict"].allowed, r["base"]))
    allowed = [r for r in results if r["verdict"].allowed]
    blocked = [r for r in results if not r["verdict"].allowed]
    print(f"\n{len(blocked)} blocked / {len(results)} pairs\n" + "─" * 60)
    for r in blocked:
        print(f"  ✗ {r['base']:<10} {r['verdict'].reasons[0]}")
    print(f"\n{len(allowed)} allowed (verified={sum(r['verdict'].verified for r in allowed)})")
    if args.verbose:
        for r in allowed:
            tag = "" if r["verdict"].verified else "  (unverified)"
            print(f"  ✓ {r['base']:<10}{tag}")


def cmd_scan(cfg, args):
    from bot.scanner import scan
    from bot.structure import structure_summary
    rows = scan(cfg, verbose=args.verbose)
    print(f"\ntop {len(rows)} by trend score\n" + "─" * 90)
    print(f"{'symbol':<13}{'score':>7}{'ret30d':>9}{'rsi':>6}{'vol×':>6}  weekly / 4H / daily structure")
    for c in rows:
        f = c["features"]
        mtf = c.get("mtf", {})
        order = c.get("mtf_order", ["1w", "4h", "1d"])
        tfs = "  |  ".join(f"{tf}: {structure_summary(mtf.get(tf, c.get('structure', {})))}"
                           for tf in order)
        print(f"{c['symbol']:<13}{f['trend_score']:>7}{str(f['ret_30d']):>9}"
              f"{str(f['rsi14']):>6}{str(f['vol_ratio_20d']):>6}  {tfs}")


def cmd_regime(cfg, args):
    from bot import regime
    from bot.scanner import scan
    cands = scan(cfg) if args.deep else None
    r = regime.assess(cfg, cands)
    print(json.dumps(r, indent=2, default=str))


def cmd_status(cfg, args):
    path = cfg.state_path
    if not path.exists():
        print(f"no portfolio yet at {path} (run `once`)")
        return
    raw = json.loads(path.read_text())
    print(f"\nmode: {cfg.mode}   cash: ${raw['cash']:,.2f}   "
          f"day_open_equity: ${raw.get('day_open_equity', 0):,.2f}")
    pos = raw.get("positions", {})
    if pos:
        print("─" * 50)
        for asset, p in pos.items():
            print(f"  {asset:<10} qty={p['qty']:.6g}  avg=${p['avg_cost']:.6g}  "
                  f"peak=${p['peak_price']:.6g}")
    trades = raw.get("trades", [])
    print(f"\n{len(trades)} trades logged", end="")
    if trades:
        last = trades[-1]
        print(f"  · last: {last['side']} {last['asset']} @ {last.get('iso', '')}")
    else:
        print()
    curve = raw.get("equity_curve", [])
    if curve:
        print(f"equity: ${curve[-1]['equity']:,.2f}")


def _check_live(cfg, want_live: bool) -> bool:
    if not want_live:
        return False
    if cfg.mode != "live" or not cfg.live_confirmed:
        _die("--live requires `mode: live` in config.yaml AND "
             "I_UNDERSTAND_LIVE_TRADING=yes in .env")
    if not (cfg.binance_api_key and cfg.binance_api_secret):
        _die("--live requires BINANCE_API_KEY / BINANCE_API_SECRET in .env")
    return True


def cmd_once(cfg, args):
    from bot.cycle import run_cycle
    live = _check_live(cfg, args.live)
    if live:
        print("⚠  LIVE MODE — real orders will be placed. Ctrl-C in 5s to abort.")
        time.sleep(5)
    report = run_cycle(cfg, live=live, verbose=not args.quiet)
    print(f"\ncycle done in {report['elapsed_sec']}s — equity ${report['equity']:,.2f}")


def cmd_loop(cfg, args):
    from bot.cycle import run_cycle
    live = _check_live(cfg, args.live)
    interval = cfg.cycle.interval_minutes * 60
    print(f"looping every {cfg.cycle.interval_minutes} min "
          f"({'LIVE' if live else cfg.mode}). Ctrl-C to stop.")
    if live:
        time.sleep(5)
    while True:
        try:
            report = run_cycle(cfg, live=live, verbose=not args.quiet)
            print(f"next cycle in {cfg.cycle.interval_minutes} min "
                  f"— equity ${report['equity']:,.2f}")
        except KeyboardInterrupt:
            print("\nstopped.")
            return
        except Exception as exc:  # keep the loop alive across transient failures
            print(f"cycle error: {exc!r} — retrying next interval", file=sys.stderr)
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nstopped.")
            return


def cmd_review(cfg, args):
    from bot.review import agent_recommendation, build_review, print_review
    rv = build_review(cfg.state_path, cfg, months=args.months)
    print_review(rv)
    out = cfg.report_path.parent / "review.json"
    out.write_text(json.dumps(rv, indent=2, default=float))
    print(f"\nwrote {out}")
    if args.agent:
        if not cfg.anthropic_api_key:
            _die("--agent needs ANTHROPIC_API_KEY")
        print("\n─── agent re-weighting recommendation ───")
        print(agent_recommendation(rv, cfg))


def cmd_tg_id(cfg, args):
    """Print chat ids of anyone who has recently messaged the bot."""
    import requests
    token = args.token or cfg.telegram_bot_token
    if not token:
        _die("pass --token or set TELEGRAM_BOT_TOKEN in .env")
    print("send your bot a message in Telegram, then run this.\n")
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30).json()
    if not r.get("ok"):
        _die(f"Telegram: {r.get('description', r)}")
    seen = {}
    for u in r.get("result", []):
        m = u.get("message") or u.get("channel_post") or {}
        ch = m.get("chat", {})
        if ch.get("id"):
            seen[ch["id"]] = f"{ch.get('type')}  {ch.get('title') or ch.get('username') or ch.get('first_name', '')}"
    if not seen:
        print("no messages yet — DM the bot and retry")
        return
    for cid, desc in seen.items():
        print(f"  TELEGRAM_CHAT_ID={cid}   ({desc})")


def cmd_serve(cfg, args):
    from bot.serve import main as serve_main
    argv = []
    if args.config:
        argv += ["--config", args.config]
    if args.live:
        argv.append("--live")
    serve_main(argv)


def cmd_reset(cfg, args):
    path = cfg.state_path
    if not path.exists():
        print("nothing to reset")
        return
    if not args.yes:
        _die(f"this deletes {path}. re-run with --yes to confirm")
    path.unlink()
    print(f"deleted {path}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="halal-crypto-bot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="path to config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("screen", help="halal screen verdicts")
    s.add_argument("-v", "--verbose", action="store_true", help="also list allowed coins")
    s.set_defaults(func=cmd_screen)

    s = sub.add_parser("scan", help="ranked shortlist")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("regime", help="market regime")
    s.add_argument("--deep", action="store_true", help="also compute shortlist breadth")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_regime)

    s = sub.add_parser("status", help="portfolio state")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("once", help="run one cycle")
    s.add_argument("--live", action="store_true")
    s.add_argument("-q", "--quiet", action="store_true")
    s.set_defaults(func=cmd_once)

    s = sub.add_parser("loop", help="run a cycle every interval")
    s.add_argument("--live", action="store_true")
    s.add_argument("-q", "--quiet", action="store_true")
    s.set_defaults(func=cmd_loop)

    s = sub.add_parser("review", help="quarterly strategy review (concepts × regime)")
    s.add_argument("--months", type=float, default=None, help="only trades from the last N months")
    s.add_argument("--agent", action="store_true", help="ask Claude for a re-weighting recommendation")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("serve", help="24/7 service: trading loop + Telegram control")
    s.add_argument("--live", action="store_true")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("tg-id", help="find your TELEGRAM_CHAT_ID")
    s.add_argument("--token", help="bot token (else uses TELEGRAM_BOT_TOKEN)")
    s.set_defaults(func=cmd_tg_id)

    s = sub.add_parser("reset", help="wipe portfolio state")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_reset)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    from pathlib import Path
    cfg = Config(Path(args.config) if args.config else None)
    if cfg.anthropic_api_key is None and args.cmd in ("once", "loop"):
        _die("ANTHROPIC_API_KEY not set (.env)")
    args.func(cfg, args)


if __name__ == "__main__":
    main()
