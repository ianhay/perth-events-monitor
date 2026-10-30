"""Shared paths, config loading, JSON storage and a polite HTTP client."""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.robotparser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yaml

from . import APP_NAME, __version__

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DATA = ROOT / "data"            # working state (caches, first-seen dates)
SITE_DATA = ROOT / "docs" / "data"  # what the website reads
REPORTS = ROOT / "reports"

log = logging.getLogger("monitor")


def setup_logging() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    log.info("%s v%s", APP_NAME, __version__)


def load_yaml(name: str) -> dict:
    with open(CONFIG / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_json(path: Path, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, obj: Any, pretty: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        if pretty:
            json.dump(obj, fh, ensure_ascii=False, indent=1, sort_keys=False)
        else:
            json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)


def host_of(url: str) -> str:
    h = urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


class Http:
    """requests wrapper with per-host delay, robots.txt checks and retries."""

    def __init__(self, delay: float = 1.5, timeout: float = 25):
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        contact = f"https://github.com/{repo}" if repo else "personal use"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": f"Mozilla/5.0 (compatible; PerthEventsMonitor/{__version__}; +{contact})",
            "Accept-Language": "en-AU,en;q=0.8",
        })
        self.delay = delay
        self.timeout = timeout
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.requests_made = 0

    def _wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            gap = time.monotonic() - last
            if gap < self.delay:
                time.sleep(self.delay - gap)
        self._last[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                self._wait(p.netloc)
                r = self.session.get(base + "/robots.txt", timeout=self.timeout)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
                self._robots[base] = rp
            except requests.RequestException:
                self._robots[base] = None  # unreachable robots.txt: assume allowed
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.session.headers["User-Agent"], url)

    def get(self, url: str, *, check_robots: bool = True, **kw) -> requests.Response | None:
        if check_robots and not self.allowed(url):
            log.info("robots.txt disallows %s", url)
            return None
        host = urlparse(url).netloc
        for attempt in range(3):
            self._wait(host)
            try:
                r = self.session.get(url, timeout=self.timeout, allow_redirects=True, **kw)
                self.requests_made += 1
            except requests.RequestException as exc:
                log.debug("GET %s failed (%s), attempt %d", url, exc, attempt + 1)
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code in (429, 502, 503, 504):
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                log.debug("GET %s -> %s", url, r.status_code)
                return None
            return r
        return None
