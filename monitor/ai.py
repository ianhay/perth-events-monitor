"""Free AI extraction through GitHub Models for pages with no feed or structured data.

Inside GitHub Actions the built-in GITHUB_TOKEN works when the workflow grants
`models: read`. Outside Actions (or if your account blocks that), set MODELS_TOKEN
to a fine-grained personal access token with only the "Models: read" permission.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime

import requests

from .common import DATA, load_json, save_json
from .normalise import PERTH, Event, clean, parse_dt

log = logging.getLogger("monitor")
ENDPOINT = "https://models.github.ai/inference/chat/completions"
CACHE_PATH = DATA / "ai_cache.json"
DEBUG_PATH = DATA / "ai_debug.json"
PROMPT_VERSION = "2"  # bump to discard cached replies after prompt or parsing changes

SYSTEM = """You extract public events from local-government web pages in Perth and Peel, Western Australia.
Today is {today}. Return only JSON: {{"events": [ ... ]}}.
Include only events with a specific date from today onward. Never invent details; leave a field empty if the page does not say.
Each event object has:
  title, start (YYYY-MM-DDTHH:MM local time, or YYYY-MM-DD if no time is given), end (same format or ""),
  venue, address, price_text (as written, e.g. "Free", "$15", "Free, bookings required"),
  url (the event's own page or booking link, chosen from the LINKS list when one matches, else ""),
  audience (one of: kids, teens, adults, all_ages, seniors),
  category (one of: music, art_workshop, talk, market_festival, film, fitness, other),
  description (one sentence, max 200 characters).
For a recurring session, list each dated occurrence in the next 90 days, at most 20 occurrences.
If the page lists no dated events, return {{"events": []}}."""


def parse_reply(content: str) -> list[dict] | None:
    """Events from a model reply, tolerating code fences or text around the JSON. None if unreadable."""
    text = (content or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    for candidate in (text, text[text.find("{"): text.rfind("}") + 1] if "{" in text else "",
                      text[text.find("["): text.rfind("]") + 1] if "[" in text else ""):
        if not candidate:
            continue
        try:
            data = json.loads(candidate, strict=False)
        except ValueError:
            continue
        if isinstance(data, dict):
            data = data.get("events", next((v for v in data.values() if isinstance(v, list)), []))
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)]
    return None


class AIExtractor:
    def __init__(self, settings: dict):
        cfg = settings.get("ai", {})
        self.enabled = bool(cfg.get("enabled", True))
        self.model = cfg.get("model", "openai/gpt-4o-mini")
        self.max_calls = int(cfg.get("max_calls_per_run", 100))
        self.max_chars = int(cfg.get("max_page_chars", 7000))
        self.token = os.environ.get("MODELS_TOKEN") or os.environ.get("GITHUB_TOKEN")
        self.cache: dict = load_json(CACHE_PATH, {})
        self.calls = 0
        self.cache_hits = 0
        self.stopped_reason = ""
        self.debug: list[dict] = []  # what each call saw and returned, for troubleshooting
        if self.enabled and not self.token:
            self.enabled = False
            self.stopped_reason = "no token (set MODELS_TOKEN or run in GitHub Actions with models: read)"
            log.warning("AI extraction off: %s", self.stopped_reason)

    def available(self) -> bool:
        return self.enabled and not self.stopped_reason and self.calls < self.max_calls

    def extract(self, text: str, links: str, page_url: str) -> list[Event] | None:
        """Events found on the page, [] if none, or None if skipped (budget/limit) so it retries later."""
        text = text[: self.max_chars]
        key = hashlib.sha1(f"{PROMPT_VERSION}|{self.model}|{page_url}|{text}".encode()).hexdigest()
        if key in self.cache:
            self.cache_hits += 1
            self.cache[key]["used"] = datetime.now(PERTH).date().isoformat()
            return [self._to_event(e, page_url) for e in self.cache[key]["events"] if self._valid(e)]
        if not self.available():
            return None

        today = datetime.now(PERTH).strftime("%A %d %B %Y")
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM.format(today=today)},
                {"role": "user", "content": f"PAGE URL: {page_url}\n\nPAGE TEXT:\n{text}\n\nLINKS:\n{links[:2500]}"},
            ],
        }
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                   "Accept": "application/json"}
        for attempt in range(3):
            self.calls += 1
            try:
                r = requests.post(ENDPOINT, json=body, headers=headers, timeout=90)
            except requests.RequestException as exc:
                log.warning("AI request failed: %s", exc)
                time.sleep(5)
                continue
            if r.status_code == 429:
                wait = int(r.headers.get("retry-after", "0") or 0)
                if wait > 120 or attempt == 2:
                    self.stopped_reason = "free-tier rate limit reached; remaining pages wait for the next run"
                    log.warning("AI extraction paused: %s", self.stopped_reason)
                    return None
                time.sleep(max(wait, 20))
                continue
            if r.status_code in (401, 403):
                self.stopped_reason = (f"GitHub Models refused the token ({r.status_code}). Check the workflow has "
                                       "'models: read', or add a MODELS_TOKEN secret")
                log.warning("AI extraction off: %s", self.stopped_reason)
                return None
            if r.status_code >= 400:
                log.warning("AI request returned %s: %s", r.status_code, r.text[:200])
                return None
            try:
                content = r.json()["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, ValueError, TypeError) as exc:
                content = ""
                log.warning("AI reply had no content for %s: %s (%s)", page_url, exc, r.text[:200])
            events = parse_reply(content)
            self.debug.append({"url": page_url, "chars": len(text), "text_start": text[:400],
                               "reply_start": content[:600], "events": None if events is None else len(events)})
            time.sleep(4)  # stay under the per-minute limit
            if events is None:
                log.warning("AI reply not understood for %s: %r", page_url, content[:200])
                return None  # not cached, so it is retried next run
            self.cache[key] = {"url": page_url, "events": events,
                               "used": datetime.now(PERTH).date().isoformat()}
            log.info("AI: %d events from %s (%d chars)", len(events), page_url, len(text))
            return [self._to_event(e, page_url) for e in events if self._valid(e)]
        return None

    @staticmethod
    def _valid(e: dict) -> bool:
        return bool(e.get("title")) and parse_dt(e.get("start"))[0] is not None

    @staticmethod
    def _to_event(e: dict, page_url: str) -> Event:
        start, all_day = parse_dt(e.get("start"))
        end, _ = parse_dt(e.get("end"))
        audience = (e.get("audience") or "").lower()
        return Event(
            title=clean(e.get("title"), 200), start=start, end=end, all_day=all_day,
            venue=clean(e.get("venue")), address=clean(e.get("address")),
            price_text=clean(e.get("price_text"), 80), description=clean(e.get("description"), 300),
            url=(e.get("url") or page_url).strip(), method="ai",
            hint_category=(e.get("category") or "").lower(),
            hint_audience="kids" if audience in ("kids", "teens") else audience,
        )

    def save(self) -> None:
        # Drop cache entries not used for 60 days so the file stays small.
        cutoff = datetime.now(PERTH).date().toordinal() - 60
        self.cache = {k: v for k, v in self.cache.items()
                      if datetime.fromisoformat(v.get("used", "2000-01-01")).toordinal() >= cutoff}
        save_json(CACHE_PATH, self.cache, pretty=False)
        save_json(DEBUG_PATH, self.debug)
