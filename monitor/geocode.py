"""Venue geocoding with OpenStreetMap Nominatim (cached, 1 request per second)."""
from __future__ import annotations

import logging
import re
import time

from .common import DATA, Http, load_json, save_json
from .normalise import Event

log = logging.getLogger("monitor")
CACHE_PATH = DATA / "geocache.json"
NOMINATIM = "https://nominatim.openstreetmap.org/search"


class Geocoder:
    def __init__(self, settings: dict, http: Http):
        cfg = settings.get("geocode", {})
        self.enabled = cfg.get("enabled", True)
        self.max_new = int(cfg.get("max_new_per_run", 80))
        self.viewbox = ",".join(str(v) for v in cfg.get("viewbox", [115.40, -31.40, 116.45, -33.10]))
        self.http = http
        self.cache: dict = load_json(CACHE_PATH, {})
        self.new = 0

    @staticmethod
    def _queries(ev: Event) -> list[str]:
        council = re.sub(r"^(City|Town|Shire) of ", "", ev.council or "")
        qs = []
        if ev.address:
            qs.append(f"{ev.address}, Western Australia")
        if ev.venue:
            qs.append(f"{ev.venue}, {council}, Western Australia" if council else f"{ev.venue}, Western Australia")
            qs.append(f"{ev.venue}, Western Australia")
        return list(dict.fromkeys(q for q in qs if len(q) > 22))

    def locate(self, ev: Event) -> None:
        if not self.enabled or ev.lat is not None:
            return
        for q in self._queries(ev):
            key = q.lower()
            if key in self.cache:
                hit = self.cache[key]
                if hit:
                    ev.lat, ev.lon = hit
                    return
                continue
            if self.new >= self.max_new:
                return
            self.new += 1
            time.sleep(1.1)
            r = self.http.get(NOMINATIM, check_robots=False, params={
                "q": q, "format": "jsonv2", "limit": 1, "countrycodes": "au",
                "viewbox": self.viewbox, "bounded": 1})
            hit = None
            if r is not None:
                try:
                    data = r.json()
                    if data:
                        hit = [float(data[0]["lat"]), float(data[0]["lon"])]
                except (ValueError, KeyError, IndexError):
                    hit = None
            self.cache[key] = hit
            if hit:
                ev.lat, ev.lon = hit
                return

    def save(self) -> None:
        save_json(CACHE_PATH, self.cache)
