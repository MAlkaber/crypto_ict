"""Configuration loading (config.yaml + blocklist.yaml + .env)."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"


def _ns(obj):
    """Recursively turn dicts into attribute-access namespaces."""
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_ns(v) for v in obj]
    return obj


class Config:
    def __init__(self, config_path: Path | None = None, blocklist_path: Path | None = None):
        load_dotenv(ROOT / ".env")
        config_path = config_path or ROOT / "config.yaml"
        blocklist_path = blocklist_path or ROOT / "blocklist.yaml"

        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        with open(blocklist_path, "r", encoding="utf-8") as f:
            self.blocklist = yaml.safe_load(f)

        self._raw = raw
        for key, val in raw.items():
            setattr(self, key, _ns(val))

        DATA_DIR.mkdir(exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        # secrets
        self.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
        self.binance_api_key = os.getenv("BINANCE_API_KEY")
        self.binance_api_secret = os.getenv("BINANCE_API_SECRET")
        self.live_confirmed = os.getenv("I_UNDERSTAND_LIVE_TRADING", "no").strip().lower() == "yes"
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")

    @property
    def state_path(self) -> Path:
        return DATA_DIR / f"portfolio_{self.mode}.json"

    @property
    def report_path(self) -> Path:
        return DATA_DIR / "last_cycle.json"
