"""Shared control state between the trading loop and the Telegram bot.

Persisted bits (pause flag) live in data/control.json so a restart on a
managed host (Railway / Render / Fly) resumes in the same state.
"""
from __future__ import annotations

import json
import threading
import time

from .config import DATA_DIR

_PATH = DATA_DIR / "control.json"


class Controller:
    def __init__(self, cfg, live: bool = False):
        self.cfg = cfg
        self.live = live
        self.lock = threading.Lock()          # held for the duration of a cycle
        self.run_now = threading.Event()      # set -> loop runs a cycle immediately
        self.stop = threading.Event()
        self.started_at = time.time()
        self.cycle_count = 0
        self.last_cycle_at: float | None = None
        self.last_error: str | None = None
        self._flatten = False
        self.paused = False
        self._load()

    # ── persistence ─────────────────────────────────────────
    def _load(self):
        try:
            d = json.loads(_PATH.read_text())
            self.paused = bool(d.get("paused", False))
        except (FileNotFoundError, ValueError):
            pass

    def _save(self):
        try:
            _PATH.write_text(json.dumps({"paused": self.paused, "ts": time.time()}, indent=2))
        except OSError:
            pass

    # ── operations ──────────────────────────────────────────
    def pause(self):
        self.paused = True
        self._save()

    def resume(self):
        self.paused = False
        self._save()

    def request_flatten(self):
        self._flatten = True
        self.run_now.set()

    def consume_flatten(self) -> bool:
        v, self._flatten = self._flatten, False
        return v

    def trigger_cycle(self):
        self.run_now.set()

    def next_cycle_eta(self) -> float | None:
        if self.last_cycle_at is None:
            return None
        due = self.last_cycle_at + self.cfg.cycle.interval_minutes * 60
        return max(due - time.time(), 0.0)

    def uptime(self) -> float:
        return time.time() - self.started_at
