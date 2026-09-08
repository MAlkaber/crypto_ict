"""Ethical / halal screen — blocklist + keyword rules, with optional
CoinGecko category enrichment.

BEST EFFORT. See blocklist.yaml header. Enforced in code so the trading
agent can never trade a coin that fails here.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .config import CACHE_DIR

STABLE_HINTS = re.compile(r"(USD|EUR|GBP|DAI|BUSD)$")
_CG_BASE = "https://api.coingecko.com/api/v3"


@dataclass
class Verdict:
    allowed: bool
    verified: bool
    reasons: list[str] = field(default_factory=list)

    def as_dict(self):
        return {"allowed": self.allowed, "verified": self.verified, "reasons": self.reasons}


class _CoinGecko:
    """Minimal cached CoinGecko client. Free tier is heavily rate-limited,
    so everything is cached to disk and new lookups are budgeted per run."""

    def __init__(self, lookups_per_run: int = 8):
        self.dir = CACHE_DIR / "coingecko"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.budget = lookups_per_run
        self._list = None

    def _get(self, path, params=None):
        r = requests.get(_CG_BASE + path, params=params, timeout=20,
                         headers={"User-Agent": "halal-crypto-bot/1.0"})
        if r.status_code == 429:
            raise RuntimeError("coingecko rate limited")
        r.raise_for_status()
        return r.json()

    def _symbol_index(self) -> dict[str, list[str]]:
        if self._list is not None:
            return self._list
        cache = self.dir / "coins_list.json"
        if cache.exists() and time.time() - cache.stat().st_mtime < 7 * 86400:
            data = json.loads(cache.read_text())
        else:
            try:
                data = self._get("/coins/list")
                cache.write_text(json.dumps(data))
            except Exception:
                data = json.loads(cache.read_text()) if cache.exists() else []
        idx: dict[str, list[str]] = {}
        for row in data:
            idx.setdefault(row["symbol"].upper(), []).append(row["id"])
        self._list = idx
        return idx

    def categories_for(self, base: str) -> list[str] | None:
        ids = self._symbol_index().get(base.upper(), [])
        if len(ids) != 1:            # ambiguous or unknown ticker -> don't guess
            return None
        coin_id = ids[0]
        cache = self.dir / f"{coin_id}.json"
        if cache.exists() and time.time() - cache.stat().st_mtime < 30 * 86400:
            return json.loads(cache.read_text())
        if self.budget <= 0:
            return None
        self.budget -= 1
        try:
            data = self._get(f"/coins/{coin_id}", params={
                "localization": "false", "tickers": "false", "market_data": "false",
                "community_data": "false", "developer_data": "false",
            })
            cats = [c for c in (data.get("categories") or []) if c]
            cache.write_text(json.dumps(cats))
            time.sleep(2.5)          # be gentle with the free tier
            return cats
        except Exception:
            return None


class Screener:
    def __init__(self, cfg):
        self.cfg = cfg
        bl = cfg.blocklist
        self.explicit = {k.upper(): v for k, v in (bl.get("explicit_block") or {}).items()}
        self.exchange_tokens = {s.upper() for s in (bl.get("exchange_tokens") or [])}
        self.patterns = [re.compile(p, re.IGNORECASE) for p in (bl.get("keywords") or [])]
        self.bad_categories = {c.lower() for c in (bl.get("bad_categories") or [])}
        self.names: dict[str, str] = {}
        self.cg = _CoinGecko() if getattr(cfg.screening, "use_coingecko", False) else None

    def set_names(self, mapping: dict[str, str]):
        """Optionally provide base_asset -> human project name for keyword matching."""
        self.names = {k.upper(): v for k, v in mapping.items()}

    def check(self, base: str) -> Verdict:
        b = base.upper()
        s = self.cfg.screening

        if b in self.explicit:
            return Verdict(False, True, [self.explicit[b]])
        if getattr(s, "exclude_stablecoins", True) and STABLE_HINTS.search(b):
            return Verdict(False, True, ["looks like a stablecoin"])
        if getattr(s, "exclude_exchange_tokens", False) and b in self.exchange_tokens:
            return Verdict(False, True, ["exchange token (venue offers margin/futures)"])

        haystack = f"{b} {self.names.get(b, '')}".strip()
        for pat in self.patterns:
            if pat.search(haystack):
                return Verdict(False, True, [f"name/symbol matches {pat.pattern!r}"])

        if self.cg is not None:
            cats = self.cg.categories_for(b)
            if cats is None:
                return Verdict(not getattr(s, "exclude_unverified", False), False,
                               ["category could not be verified"])
            low = [c.lower() for c in cats]
            hit = [c for c in low if c in self.bad_categories]
            if hit:
                return Verdict(False, True, [f"CoinGecko category: {', '.join(hit)}"])
            if getattr(s, "exclude_memecoins", True) and any("meme" in c for c in low):
                return Verdict(False, True, ["CoinGecko category: meme"])
            return Verdict(True, True, ["categories reviewed: " + ", ".join(cats[:4])])

        return Verdict(True, False, ["passed blocklist + keyword screen (categories not checked)"])
