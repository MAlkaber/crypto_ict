"""Notifications + the public signal feed (the copy-trading foundation).

- console: human-readable cycle summary
- telegram: same summary pushed to a chat (optional)
- signal feed: data/signals.json (latest cycle) + data/signals_log.jsonl
  (append-only history). Followers read these to mirror trades manually or
  with their own copy of the bot. Spot-only, no leverage — nothing here
  sends an order.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import requests

from .config import DATA_DIR

_TG = "https://api.telegram.org/bot{token}/sendMessage"


class Notifier:
    def __init__(self, cfg):
        self.cfg = cfg
        n = cfg.notifications
        self.console = getattr(n, "console", True)
        self.telegram = getattr(n, "telegram", False) and cfg.telegram_bot_token and cfg.telegram_chat_id

    # ── channels ────────────────────────────────────────────
    def _tg_send(self, text: str):
        if not self.telegram:
            return
        try:
            requests.post(_TG.format(token=self.cfg.telegram_bot_token),
                          json={"chat_id": self.cfg.telegram_chat_id, "text": text,
                                "parse_mode": "Markdown", "disable_web_page_preview": True},
                          timeout=15)
        except requests.RequestException:
            pass

    def message(self, text: str):
        if self.console:
            print(text)
        self._tg_send(text)

    # ── cycle summary ───────────────────────────────────────
    def cycle(self, report: dict):
        mode = report.get("mode", "paper").upper()
        eq = report.get("equity")
        br = report.get("bull_run", {}) or {}
        br_txt = ""
        if br.get("active"):
            br_txt = f"  |  🚀 bull-run: {br.get('phase', '?')}"
        elif br.get("derisk"):
            br_txt = "  |  ⚠ de-risk"
        dd = report.get("drawdown_pct", 0)
        cb = "  |  🛑 circuit breaker" if report.get("circuit_breaker") else ""
        lines = [
            f"*halal-crypto-bot* — {mode} cycle @ {report.get('time', '')}",
            f"regime: *{report.get('regime', '?')}*{br_txt}{cb}  |  equity: ${eq:,.2f}  "
            f"|  cash: ${report.get('cash', 0):,.2f}  |  dd: {dd:+.1f}%",
        ]
        for m in report.get("stop_moves", []):
            lines.append(f"↑ {m['asset']} stop → {m['new_stop']:.6g}")
        forced = report.get("forced_exits", [])
        for f in forced:
            verb = "trim" if f.get("fraction") else "exit"
            lines.append(f"🛑 {verb} {f['asset']} — {f['reason']}")
        for t in report.get("trades", []):
            if not t.get("ok", True):
                continue
            arrow = "🟢 BUY " if t["side"] == "BUY" else "🔴 SELL"
            extra = f"  (stop {t['stop']:.6g}, {t.get('r_pct', 0):.1f}%)" if t.get("stop") and t["side"] == "BUY" else ""
            lines.append(f"{arrow} {t['asset']} ${t.get('usd', 0):,.0f} @ {t.get('price', 0):.6g}{extra}")
        if report.get("agent_summary"):
            lines.append("")
            lines.append(report["agent_summary"])
        if not forced and not report.get("trades"):
            lines.append("no trades — holding")
        self.message("\n".join(lines))

    # ── public signal feed ──────────────────────────────────
    def publish_signals(self, report: dict):
        actions = []
        for f in report.get("forced_exits", []):
            actions.append({"action": "TRIM" if f.get("fraction") else "EXIT",
                            "symbol": f["asset"] + "USDT", "reason": f["reason"], "auto": True})
        for t in report.get("trades", []):
            if not t.get("ok", True):
                continue
            actions.append({
                "action": t["side"], "symbol": t["asset"] + "USDT",
                "price": t.get("price"), "usd": round(t.get("usd", 0), 2),
                "stop": t.get("stop"), "target": t.get("target"), "r_pct": t.get("r_pct"),
                "weight_pct": report.get("weights", {}).get(t["asset"]),
                "thesis": t.get("thesis", ""),
                "concepts": t.get("concepts", []),
            })

        signal = {
            "ts": time.time(),
            "time": report.get("time"),
            "mode": report.get("mode"),
            "regime": report.get("regime"),
            "bull_run": report.get("bull_run", {}),
            "market_view": report.get("market_view", ""),
            "equity": report.get("equity"),
            "actions": actions,
            "positions": report.get("positions_after", []),
            "disclaimer": "Best-effort halal spot signals. Not financial advice. "
                          "Do your own screening and risk management.",
        }
        (DATA_DIR / "signals.json").write_text(json.dumps(signal, indent=2, default=float))
        with open(DATA_DIR / "signals_log.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(signal, default=float) + "\n")
        return signal


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
