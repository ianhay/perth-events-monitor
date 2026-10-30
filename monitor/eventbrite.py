"""Events from followed Eventbrite organisers, through Eventbrite's official API.

Needs a free private token from your Eventbrite account
(Account settings > Developer links > API keys), stored as EVENTBRITE_TOKEN.
"""
from __future__ import annotations

import logging
import os

from .common import Http
from .normalise import Event, clean, parse_dt

log = logging.getLogger("monitor")
API = "https://www.eventbriteapi.com/v3"


def _price(ev: dict) -> tuple[float | None, float | None]:
    ta = ev.get("ticket_availability") or {}

    def major(key):
        v = (ta.get(key) or {}).get("major_value")
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return major("minimum_ticket_price"), major("maximum_ticket_price")


def organiser_events(org: dict, http: Http, token: str) -> tuple[list[Event], str]:
    """Return (events, error message or "")."""
    url = f"{API}/organizers/{org['id']}/events/"
    params = {"status": "live", "order_by": "start_asc", "expand": "venue,ticket_availability",
              "page_size": 50, "time_filter": "current_future"}
    headers = {"Authorization": f"Bearer {token}"}
    out: list[Event] = []
    for page in range(1, 6):
        params["page"] = page
        r = http.session.get(url, params=params, headers=headers, timeout=30)
        http.requests_made += 1
        if r.status_code != 200:
            return out, f"HTTP {r.status_code}: {r.text[:120]}"
        data = r.json()
        for ev in data.get("events", []):
            start, _ = parse_dt((ev.get("start") or {}).get("local"))
            if not start:
                continue
            end, _ = parse_dt((ev.get("end") or {}).get("local"))
            venue = ev.get("venue") or {}
            addr = (venue.get("address") or {})
            lo, hi = _price(ev)
            lat = venue.get("latitude") or addr.get("latitude")
            lon = venue.get("longitude") or addr.get("longitude")
            out.append(Event(
                title=clean((ev.get("name") or {}).get("text")), start=start, end=end,
                venue=clean(venue.get("name")), address=clean(addr.get("localized_address_display")),
                lat=float(lat) if lat else None, lon=float(lon) if lon else None,
                description=clean(ev.get("summary") or (ev.get("description") or {}).get("text"), 600),
                url=ev.get("url", ""), is_free=bool(ev.get("is_free")),
                price_min=0.0 if ev.get("is_free") else lo, price_max=0.0 if ev.get("is_free") else hi,
                council=org.get("council") or "Eventbrite", source="eventbrite",
                source_url=f"https://www.eventbrite.com.au/o/{org['id']}", method="api",
            ))
        if not (data.get("pagination") or {}).get("has_more_items"):
            break
    return out, ""


def collect(organisers: list[dict], http: Http) -> tuple[list[Event], list[dict]]:
    token = os.environ.get("EVENTBRITE_TOKEN", "")
    if not organisers:
        return [], []
    if not token:
        log.warning("EVENTBRITE_TOKEN not set; skipping %d Eventbrite organisers", len(organisers))
        return [], [{"source": "Eventbrite", "url": "", "events": 0, "status": "skipped", "detail": "EVENTBRITE_TOKEN secret not set"}]
    events, health = [], []
    for org in organisers:
        evs, err = organiser_events(org, http, token)
        events.extend(evs)
        health.append({"source": f"Eventbrite: {org.get('name') or org['id']}",
                       "url": f"https://www.eventbrite.com.au/o/{org['id']}",
                       "status": "error" if err else "ok", "events": len(evs), "detail": err})
        log.info("Eventbrite %s: %d events %s", org.get("name") or org["id"], len(evs), err)
    return events, health
