"""Daily collection run.

    python -m monitor.collect

Reads config/sources.json (run monitor.discover first), collects events from every
council source and followed Eventbrite organiser, merges duplicates, and writes
docs/data/events.json and docs/data/health.json for the website and digest.
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta

from . import __version__
from .ai import AIExtractor
from .common import DATA, SITE_DATA, Http, load_json, load_yaml, save_json, setup_logging
from .discover import SOURCES_PATH
from .eventbrite import collect as collect_eventbrite
from .extractors import detail_links, next_page, page_links_for_ai, page_text, parse_ical, parse_jsonld, soupify
from .geocode import Geocoder
from .normalise import PERTH, Event, apply_price, categorise, dedupe

log = logging.getLogger("monitor")
PAGE_CACHE = DATA / "page_cache.json"
STATE = DATA / "state.json"


class Collector:
    def __init__(self, settings: dict):
        self.s = settings
        self.http = Http(delay=float(settings.get("request_delay_seconds", 1.5)))
        self.ai = AIExtractor(settings)
        self.page_cache: dict = load_json(PAGE_CACHE, {})
        self.today = datetime.now(PERTH)

    # ---- one council source ----------------------------------------------------------------
    def source(self, council: str, src: dict) -> tuple[list[Event], dict]:
        health = {"source": council, "url": src["url"], "method": src.get("method", "html"),
                  "status": "ok", "events": 0, "detail": ""}
        if src.get("method") == "ical" and src.get("ical_url"):
            r = self.http.get(src["ical_url"])
            events = parse_ical(r.content, self.s["horizon_days"]) if r is not None else []
            if r is None:
                health.update(status="error", detail="iCal feed could not be fetched")
        else:
            events = self._html_source(src["url"], health)
        for ev in events:
            ev.council, ev.source, ev.source_url = council, "council", src["url"]
        health["events"] = len(events)
        if health["status"] == "ok" and not events:
            health["status"] = "empty"
        return events, health

    def _html_source(self, url: str, health: dict) -> list[Event]:
        listing_events: list[Event] = []
        listing_pages: list[tuple[str, str]] = []  # (url, html)
        links: list[str] = []
        page_url: str | None = url
        for _ in range(int(self.s.get("max_listing_pages", 5))):
            if not page_url:
                break
            r = self.http.get(page_url)
            if r is None:
                if not listing_pages:
                    health.update(status="error", detail="listing page could not be fetched")
                break
            soup = soupify(r.text)
            listing_pages.append((r.url, r.text))
            listing_events += parse_jsonld(soup, r.url)
            links += detail_links(soup, r.url)
            nxt = next_page(soup, r.url)
            page_url = nxt if nxt and nxt not in [u for u, _ in listing_pages] else None

        if listing_events:
            return listing_events

        # Event detail pages often carry schema.org data even when the listing doesn't.
        events = self._detail_pages(list(dict.fromkeys(links)))
        if events:
            return events

        # Last resort: one free AI call per listing page.
        events = []
        for purl, html in listing_pages:
            found = self.ai.extract(page_text(html, int(self.s["ai"]["max_page_chars"])),
                                    page_links_for_ai(soupify(html), purl), purl)
            if found is None:
                health["detail"] = f"AI extraction waiting: {self.ai.stopped_reason or 'run budget used'}"
                prev = self.page_cache.get(purl, {}).get("events", [])
                events += [Event.from_cache(e) for e in prev]  # keep yesterday's results meanwhile
                continue
            self.page_cache[purl] = {"fetched": self.today.isoformat(), "events": [e.to_cache() for e in found]}
            events += found
        return events

    def _detail_pages(self, links: list[str]) -> list[Event]:
        events, fetched = [], 0
        recheck = timedelta(days=int(self.s.get("detail_recheck_days", 6)))
        cap = int(self.s.get("max_detail_pages_per_run", 40))
        for link in links:
            cached = self.page_cache.get(link)
            fresh = cached and self.today - datetime.fromisoformat(cached["fetched"]) < recheck
            if fresh or (cached and fetched >= cap):
                cached["seen"] = self.today.isoformat()
                events += [Event.from_cache(e) for e in cached["events"]]
                continue
            if fetched >= cap:
                continue
            fetched += 1
            r = self.http.get(link)
            if r is None:
                continue
            found = parse_jsonld(soupify(r.text), r.url)
            for ev in found:
                ev.url = ev.url or r.url
            self.page_cache[link] = {"fetched": self.today.isoformat(), "seen": self.today.isoformat(),
                                     "events": [e.to_cache() for e in found]}
            events += found
            # Stop early if the first few detail pages have no structured data: the AI route
            # on the listing page is then cheaper than visiting every event page.
            if fetched == 4 and not events:
                break
        return events

    # ---- whole run ----------------------------------------------------------------------------
    def run(self, only: str | None = None) -> None:
        sources = load_json(SOURCES_PATH, {})
        if not sources.get("councils"):
            raise SystemExit("config/sources.json is missing or empty: run `python -m monitor.discover` first")
        all_events: list[Event] = []
        health: list[dict] = []
        for c in sources["councils"]:
            if only and only.lower() not in c["name"].lower():
                continue
            for src in c.get("sources", []):
                try:
                    evs, h = self.source(c["name"], src)
                except Exception as exc:  # noqa: BLE001 - keep going if one site misbehaves
                    log.exception("Source failed: %s", src["url"])
                    evs, h = [], {"source": c["name"], "url": src["url"], "status": "error", "events": 0,
                                  "detail": str(exc)[:200]}
                log.info("%-32s %4d events  %s", c["name"], len(evs), src["url"])
                all_events += evs
                health.append(h)

        organisers = self._organisers(sources)
        eb_events, eb_health = collect_eventbrite(organisers, self.http)
        all_events += eb_events
        health += eb_health
        self._finish(all_events, health, sources)

    def _organisers(self, sources: dict) -> list[dict]:
        manual = {str(o["id"]): o for o in (load_yaml("eventbrite_organisers.yaml").get("organisers") or [])}
        orgs = {}
        for o in sources.get("eventbrite_organisers", []):
            orgs[o["id"]] = {"id": o["id"], "council": o["found_on"][0] if len(o["found_on"]) == 1 else ""}
        for oid, o in manual.items():
            orgs[oid] = {**orgs.get(oid, {}), **o, "id": oid}
        return [o for o in orgs.values() if not o.get("skip")]

    def _finish(self, events: list[Event], health: list[dict], sources: dict) -> None:
        now = self.today
        horizon = now + timedelta(days=int(self.s["horizon_days"]))
        upcoming = [e for e in events if e.start and (e.end or e.start) >= now - timedelta(hours=2)
                    and e.start <= horizon and e.title]
        for ev in upcoming:
            apply_price(ev)
            categorise(ev, self.s)
        merged = dedupe(upcoming)

        geo = Geocoder(self.s, self.http)
        for ev in merged:
            geo.locate(ev)
        geo.save()

        # First-seen dates drive "new" badges and the weekly digest.
        state = load_json(STATE, {"first_seen": {}, "last_digest": None})
        today = now.date().isoformat()
        first_run = not state["first_seen"]
        site_events = []
        for ev in sorted(merged, key=lambda e: e.start):
            d = ev.to_site()
            d["first_seen"] = state["first_seen"].setdefault(d["id"], "initial" if first_run else today)
            site_events.append(d)
        cutoff = (now - timedelta(days=150)).date().isoformat()
        state["first_seen"] = {k: v for k, v in state["first_seen"].items() if v == "initial" or v >= cutoff}
        save_json(STATE, state)

        # Compare with the previous run so a site that suddenly returns nothing is flagged.
        previous = {h["url"]: h for h in load_json(SITE_DATA / "health.json", {}).get("sources", [])}
        for h in health:
            prev = previous.get(h["url"], {})
            h["previous_events"] = prev.get("events")
            h["zero_runs"] = (prev.get("zero_runs", 0) + 1) if h["events"] == 0 else 0
            h["needs_attention"] = h["status"] == "error" or (h["zero_runs"] >= 2 and bool(prev.get("last_nonzero")))
            h["last_nonzero"] = today if h["events"] else prev.get("last_nonzero")
        missing = [c["name"] for c in sources["councils"] if not c.get("sources")]

        cats = {k: v["label"] for k, v in self.s["categories"].items()}
        cats["other"] = "Other"
        save_json(SITE_DATA / "events.json", {
            "generated": now.isoformat(timespec="minutes"), "version": __version__,
            "categories": cats, "interests": self.s.get("interests", []),
            "councils": sorted({e["council"] for e in site_events}),
            "events": site_events,
        }, pretty=False)
        save_json(SITE_DATA / "health.json", {
            "generated": now.isoformat(timespec="minutes"), "sources": health,
            "councils_without_sources": missing,
            "ai": {"calls": self.ai.calls, "cache_hits": self.ai.cache_hits, "note": self.ai.stopped_reason},
        })
        self._prune_cache()
        save_json(PAGE_CACHE, self.page_cache, pretty=False)
        self.ai.save()
        log.info("Wrote %d events (%d before merging duplicates). AI calls %d, cache hits %d. %d web requests.",
                 len(site_events), len(upcoming), self.ai.calls, self.ai.cache_hits, self.http.requests_made)

    def _prune_cache(self) -> None:
        cutoff = self.today - timedelta(days=30)
        self.page_cache = {k: v for k, v in self.page_cache.items()
                           if datetime.fromisoformat(v.get("seen", v["fetched"])) >= cutoff}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--council", help="only councils whose name contains this text (for testing)")
    args = ap.parse_args(argv)
    setup_logging()
    Collector(load_yaml("settings.yaml")).run(only=args.council)


if __name__ == "__main__":
    main()
