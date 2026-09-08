"""Two-way Telegram control — monitor + operate the bot from your phone.

Long-polling only (outbound HTTPS), so it runs anywhere with no inbound
ports / webhook / public URL. Commands are accepted ONLY from the chat id
in TELEGRAM_CHAT_ID — these commands move money.
"""
from __future__ import annotations

import contextlib
import io
import json
import threading
import time
import traceback

import requests

_API = "https://api.telegram.org/bot{token}/{method}"

HELP = """\
halal-crypto-bot — commands

monitor
  /status      equity, cash, day P&L, regime, next cycle, uptime
  /positions   open positions + unrealised P&L
  /report      last cycle summary (agent notes, trades, exits)
  /pnl         equity vs day-open and vs start
  /scan        current ranked shortlist (slow ~1m)
  /regime      market regime + bull-run read (slow)
  /review      quarterly concept × regime stats
  /settings    mode, model, interval, risk limits

operate
  /runcycle    run a full cycle now
  /pause       stop new buys (protective exits still run)
  /resume      allow new buys again
  /flatten YES sell everything to cash on the next cycle
"""


class TelegramBot:
    def __init__(self, cfg, controller):
        self.cfg = cfg
        self.ctl = controller
        self.token = cfg.telegram_bot_token
        self.chat_id = str(cfg.telegram_chat_id)
        self._offset = 0
        self._busy = threading.Lock()   # guards slow read commands

    # ── transport ───────────────────────────────────────────
    def _call(self, method: str, **params):
        try:
            r = requests.post(_API.format(token=self.token, method=method),
                              json=params, timeout=40)
            return r.json()
        except (requests.RequestException, ValueError):
            return {}

    def send(self, text: str, chat_id: str | None = None):
        for chunk in _split(text, 3800):
            self._call("sendMessage", chat_id=chat_id or self.chat_id, text=chunk,
                       disable_web_page_preview=True)

    # ── main loop ───────────────────────────────────────────
    def run(self):
        self.send("🟢 bot online. /help for commands.")
        while not self.ctl.stop.is_set():
            try:
                res = self._call("getUpdates", offset=self._offset, timeout=25,
                                 allowed_updates=["message"])
                for upd in res.get("result", []):
                    self._offset = upd["update_id"] + 1
                    self._handle(upd.get("message") or {})
            except Exception:  # never let the listener die
                traceback.print_exc()
                time.sleep(3)

    def _handle(self, msg: dict):
        chat = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text:
            return
        if chat != self.chat_id:
            self._call("sendMessage", chat_id=chat, text="unauthorized")
            return
        cmd, *rest = text.split()
        arg = " ".join(rest)
        fn = getattr(self, "cmd_" + cmd.lstrip("/").lower(), None)
        if fn is None:
            self.send("unknown command. /help")
            return
        try:
            fn(arg)
        except Exception as exc:
            self.send(f"⚠ {cmd} failed: {exc!r}")
            traceback.print_exc()

    # ── monitor commands ────────────────────────────────────
    def cmd_start(self, _):
        self.cmd_help(_)

    def cmd_help(self, _):
        self.send(HELP)

    def _last_report(self) -> dict:
        try:
            return json.loads(self.cfg.report_path.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def _portfolio(self) -> dict:
        try:
            return json.loads(self.cfg.state_path.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def cmd_status(self, _):
        rep = self._last_report()
        pf = self._portfolio()
        eta = self.ctl.next_cycle_eta()
        br = rep.get("bull_run", {}) or {}
        lines = [
            f"mode: {'LIVE' if self.ctl.live else self.cfg.mode}"
            f"{'  ⏸ PAUSED' if self.ctl.paused else ''}",
            f"equity: ${rep.get('equity', 0):,.2f}   cash: ${rep.get('cash', 0):,.2f}",
            f"drawdown: {rep.get('drawdown_pct', 0):+.1f}%   7d: {rep.get('rolling_7d_pct', 0):+.1f}%"
            f"{'   🛑 ' + rep.get('circuit_breaker_reason', 'circuit breaker') if rep.get('circuit_breaker') else ''}",
            f"regime: {rep.get('regime', '?')}"
            + (f"   🚀 bull-run {br.get('phase')}" if br.get('active') else "")
            + ("   ⚠ de-risk" if br.get('derisk') else ""),
            f"positions: {len(pf.get('positions', {}))}",
            f"cycles run: {self.ctl.cycle_count}   uptime: {_dur(self.ctl.uptime())}",
            f"next cycle: {_dur(eta) if eta is not None else 'pending'}",
        ]
        if self.ctl.last_error:
            lines.append(f"last error: {self.ctl.last_error}")
        self.send("\n".join(lines))

    def cmd_positions(self, _):
        rep = self._last_report()
        pos = rep.get("positions_after", [])
        if not pos:
            self.send("no open positions")
            return
        rows = [f"{p['symbol']:<12} ${p['value_usd']:>9,.0f}  {p['weight_pct']:>4}%  "
                f"{p['pnl_pct']:+.1f}%" for p in pos]
        self.send("positions (last cycle mark):\n" + "\n".join(rows))

    def cmd_report(self, _):
        rep = self._last_report()
        if not rep:
            self.send("no cycle has run yet")
            return
        lines = [f"cycle @ {rep.get('time', '')}  ({rep.get('elapsed_sec', 0)}s)",
                 f"regime {rep.get('regime')}  equity ${rep.get('equity', 0):,.2f}"]
        for f in rep.get("forced_exits", []):
            lines.append(f"🛑 {f['asset']}: {f['reason']}")
        for t in rep.get("trades", []):
            if t.get("ok", True):
                lines.append(f"{'🟢' if t['side'] == 'BUY' else '🔴'} {t['side']} "
                             f"{t['asset']} ${t.get('usd', 0):,.0f}")
        if rep.get("market_view"):
            lines.append(f"\nview: {rep['market_view']}")
        if rep.get("agent_summary"):
            lines.append(f"\n{rep['agent_summary']}")
        self.send("\n".join(lines))

    def cmd_pnl(self, _):
        pf = self._portfolio()
        curve = pf.get("equity_curve", [])
        rep = self._last_report()
        eq = rep.get("equity", curve[-1]["equity"] if curve else 0)
        start = self.cfg.risk.starting_paper_balance_usd
        peak = pf.get("peak_equity", start) or start
        self.send(f"equity: ${eq:,.2f}   peak: ${peak:,.2f}\n"
                  f"drawdown from peak: {(eq / peak - 1) * 100:+.2f}%  (${eq - peak:+,.0f})\n"
                  f"since start:        {(eq / start - 1) * 100:+.2f}%  (${eq - start:+,.0f})\n"
                  f"rolling 7d:         {rep.get('rolling_7d_pct', 0):+.2f}%")

    def cmd_settings(self, _):
        r = self.cfg.risk
        self.send(
            f"mode: {self.cfg.mode}   model: {self.cfg.model}   effort: {self.cfg.effort}\n"
            f"cycle: every {self.cfg.cycle.interval_minutes} min\n"
            f"risk/trade: {r.risk_per_trade_pct}% (R-based sizing to structural stop)\n"
            f"max position: {r.max_position_pct}%   max positions: {r.max_open_positions}\n"
            f"cash reserve: {r.cash_reserve_pct}%   min order: ${r.min_order_usd}\n"
            f"stop distance allowed: {r.min_stop_distance_pct}–{r.max_stop_distance_pct}%   "
            f"disaster stop: -{r.disaster_stop_pct}%\n"
            f"partial {float(r.partial_tp_fraction):.0%} at +{r.partial_tp_at_r}R, "
            f"trail after +{r.trail_after_r}R (-{r.trail_giveback_pct}% giveback)\n"
            f"circuit breaker: -{r.max_drawdown_pct}% from peak or -{r.max_rolling_7d_loss_pct}%/7d")

    def cmd_scan(self, _):
        if not self._busy.acquire(blocking=False):
            self.send("busy (a cycle or scan is running) — try again shortly")
            return
        try:
            self.send("scanning… (~1 min)")
            from .scanner import scan
            from .structure import structure_summary
            rows = scan(self.cfg)[:15]
            out = [f"{c['symbol']:<11} {c['features']['trend_score']:>4}  "
                   f"{structure_summary(c.get('mtf', {}).get('1w', {}))[:22]:<22} | "
                   f"{structure_summary(c.get('mtf', {}).get('4h', {}))[:22]}"
                   for c in rows]
            self.send("top 15 (score · weekly | 4H):\n" + "\n".join(out))
        finally:
            self._busy.release()

    def cmd_regime(self, _):
        if not self._busy.acquire(blocking=False):
            self.send("busy — try again shortly")
            return
        try:
            self.send("assessing regime…")
            from . import regime as rg
            from .scanner import scan
            r = rg.assess(self.cfg, scan(self.cfg))
            br = r["bull_run"]
            self.send(f"regime: {r['label']}   risk budget: {r['risk_budget']}\n"
                      f"bull-run: active={br['active']} phase={br['phase']} "
                      f"score={br['score']} derisk={br['derisk']}\n"
                      + "\n".join(f"· {s}" for s in br["signals"]))
        finally:
            self._busy.release()

    def cmd_review(self, arg):
        from .review import build_review, print_review
        months = float(arg) if arg.replace(".", "").isdigit() else None
        rv = build_review(self.cfg.state_path, self.cfg, months=months)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            print_review(rv)
        self.send(buf.getvalue() or "no closed trades yet")

    # ── operate commands ────────────────────────────────────
    def cmd_runcycle(self, _):
        self.ctl.trigger_cycle()
        self.send("▶ cycle queued — runs now")

    def cmd_pause(self, _):
        self.ctl.pause()
        self.send("⏸ paused — no new buys. Protective exits still run. /resume to undo.")

    def cmd_resume(self, _):
        self.ctl.resume()
        self.send("▶ resumed — new buys allowed from the next cycle.")

    def cmd_flatten(self, arg):
        if arg.strip().upper() != "YES":
            pf = self._portfolio()
            n = len(pf.get("positions", {}))
            self.send(f"this will SELL all {n} positions to cash on the next cycle.\n"
                      f"confirm with:  /flatten YES")
            return
        self.ctl.request_flatten()
        self.send("⚑ flatten queued — selling everything to cash now.")


def _split(text: str, limit: int):
    while text:
        yield text[:limit]
        text = text[limit:]


def _dur(sec: float | None) -> str:
    if sec is None:
        return "?"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h {sec % 3600 // 60}m"
    return f"{sec // 86400}d {sec % 86400 // 3600}h"
