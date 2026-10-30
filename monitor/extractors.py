"""Turn feeds and web pages into Event objects: iCal, schema.org JSON-LD, and links."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup

from .normalise import PERTH, Event, clean, parse_dt

log = logging.getLogger("monitor")

EVENT_TYPES = {
    "Event", "MusicEvent", "EducationEvent", "ExhibitionEvent", "Festival", "LiteraryEvent",
    "SocialEvent", "TheaterEvent", "VisualArtsEvent", "ChildrensEvent", "ComedyEvent",
    "DanceEvent", "FoodEvent", "ScreeningEvent", "BusinessEvent", "CourseInstance", "SaleEvent",
}

# Words that mark a link or page as an events listing / event page.
EVENT_WORDS = re.compile(r"event|whats-?on|what-s-on|what's on|calendar|programs?|activities|workshops?", re.I)
# Paths that look event-ish but are not public events.
NOT_EVENTS = re.compile(
    r"council-meeting|meetings?-(and|&)-|agenda|minutes|tender|career|job|vacanc|hire|booking-a-|"
    r"venue-hire|news|media-release|crime|safety|ranger|sand-drift|sediment|organise-an-event|"
    r"planning|policy|policies|submit|apply|application|permit|"
    r"event-?(planning|organis|organiz|approval|application|toolkit|guide|funding|sponsor|grants?)|"
    r"hold(ing)?-an-event|run(ning)?-an-event|waste|bin|rates|login|signin|account|search\?|"
    r"\.(pdf|docx?|xlsx?|jpg|png|zip|ics)$", re.I)


def soupify(html: str | bytes) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


# ---- iCal ---------------------------------------------------------------------
def find_ical_links(soup: BeautifulSoup, base: str) -> list[str]:
    out = []
    for tag in soup.select('link[type="text/calendar"], a[href]'):
        href = (tag.get("href") or "").strip()
        if not href:
            continue
        low = href.lower()
        text = tag.get_text(" ", strip=True).lower() if tag.name == "a" else ""
        if (low.endswith(".ics") or low.startswith("webcal:") or "ical" in low or "format=ical" in low
                or tag.name == "link" or "subscribe to calendar" in text or text in ("ical", "ical feed")):
            if low.startswith("webcal:"):
                href = "https:" + href[7:]
            out.append(urljoin(base, href))
    return list(dict.fromkeys(out))


def parse_ical(data: bytes, horizon_days: int) -> list[Event]:
    import icalendar
    import recurring_ical_events

    try:
        cal = icalendar.Calendar.from_ical(data)
    except Exception as exc:  # noqa: BLE001 - malformed feeds are common
        log.warning("Could not read iCal feed: %s", exc)
        return []
    now = datetime.now(PERTH)
    events = []
    try:
        items = recurring_ical_events.of(cal).between(now - timedelta(days=1), now + timedelta(days=horizon_days))
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not expand iCal recurrences: %s", exc)
        items = cal.walk("VEVENT")
    for comp in items:
        title = clean(str(comp.get("SUMMARY", "")))
        start, all_day = parse_dt(comp.decoded("DTSTART") if comp.get("DTSTART") else None)
        if not title or not start:
            continue
        end, _ = parse_dt(comp.decoded("DTEND") if comp.get("DTEND") else None)
        location = clean(str(comp.get("LOCATION", "")))
        venue, _, address = location.partition(",")
        events.append(Event(
            title=title, start=start, end=end, all_day=all_day,
            venue=venue.strip(), address=address.strip(),
            description=clean(str(comp.get("DESCRIPTION", "")), 600),
            url=str(comp.get("URL", "") or ""), method="ical",
        ))
    return events


# ---- schema.org JSON-LD ----------------------------------------------------------
def _types(node: dict) -> set[str]:
    t = node.get("@type", [])
    return {x.split("/")[-1] for x in ([t] if isinstance(t, str) else t if isinstance(t, list) else [])}


def _walk(node):
    if isinstance(node, list):
        for x in node:
            yield from _walk(x)
    elif isinstance(node, dict):
        if _types(node) & EVENT_TYPES:
            yield node
        for key in ("@graph", "itemListElement", "item", "subEvent", "mainEntity", "event", "events"):
            if key in node:
                yield from _walk(node[key])


def _text(v) -> str:
    if isinstance(v, dict):
        return _text(v.get("name") or v.get("@value") or "")
    if isinstance(v, list):
        return _text(v[0]) if v else ""
    return clean(str(v or ""))


def _address(addr) -> str:
    if isinstance(addr, str):
        return clean(addr)
    if isinstance(addr, dict):
        parts = [addr.get(k) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode")]
        return clean(", ".join(str(p) for p in parts if p))
    if isinstance(addr, list) and addr:
        return _address(addr[0])
    return ""


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def jsonld_event(node: dict, page_url: str) -> Event | None:
    title = _text(node.get("name"))
    start, all_day = parse_dt(node.get("startDate"))
    if not title or not start:
        return None
    end, _ = parse_dt(node.get("endDate"))
    loc = node.get("location") or {}
    if isinstance(loc, list):
        loc = next((x for x in loc if isinstance(x, dict) and "VirtualLocation" not in _types(x)), loc[0] if loc else {})
    venue = address = ""
    lat = lon = None
    if isinstance(loc, dict):
        venue = _text(loc.get("name"))
        address = _address(loc.get("address"))
        geo = loc.get("geo") or {}
        if isinstance(geo, dict):
            lat, lon = _float(geo.get("latitude")), _float(geo.get("longitude"))
    elif isinstance(loc, str):
        venue = clean(loc)

    price_text = ""
    lo = hi = None
    is_free = node.get("isAccessibleForFree")
    is_free = is_free if isinstance(is_free, bool) else (str(is_free).lower() == "true" if is_free else None)
    offers = node.get("offers")
    offers = offers if isinstance(offers, list) else [offers] if isinstance(offers, dict) else []
    prices = []
    for o in offers:
        for k in ("price", "lowPrice", "highPrice"):
            p = _float(o.get(k)) if isinstance(o, dict) else None
            if p is not None:
                prices.append(p)
    if prices:
        lo, hi = min(prices), max(prices)
        if is_free is None:
            is_free = hi == 0
    url = node.get("url") or page_url
    return Event(
        title=title, start=start, end=end, all_day=all_day, venue=venue, address=address,
        lat=lat, lon=lon, description=clean(_text(node.get("description")), 600),
        url=urljoin(page_url, url if isinstance(url, str) else page_url),
        price_text=price_text, price_min=lo, price_max=hi, is_free=is_free, method="jsonld",
    )


def parse_jsonld(soup: BeautifulSoup, page_url: str) -> list[Event]:
    out = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw, strict=False)
        except json.JSONDecodeError:
            continue
        for node in _walk(data):
            ev = jsonld_event(node, page_url)
            if ev:
                out.append(ev)
    return out


# ---- links and page text -----------------------------------------------------------
def same_site(url: str, base: str) -> bool:
    a, b = urlparse(url).netloc.lower(), urlparse(base).netloc.lower()
    return a.removeprefix("www.") == b.removeprefix("www.")


def detail_links(soup: BeautifulSoup, listing_url: str) -> list[str]:
    """Links on a listing page that look like individual event pages."""
    lp = urlparse(listing_url).path.rstrip("/")
    out = []
    for a in soup.find_all("a", href=True):
        href, _ = urldefrag(urljoin(listing_url, a["href"]))
        p = urlparse(href)
        if p.scheme not in ("http", "https") or not same_site(href, listing_url):
            continue
        path = p.path.rstrip("/")
        if not path or path == lp or NOT_EVENTS.search(href):
            continue
        under_listing = lp and path.startswith(lp + "/")
        eventish = re.search(r"/(events?|whats-?on|what-s-on|programs?|activities)/[^/]+", path, re.I)
        if (under_listing or eventish) and not re.search(r"/(page|category|tag|type)/", path) and "page=" not in p.query:
            out.append(href)
    return list(dict.fromkeys(out))


def next_page(soup: BeautifulSoup, url: str) -> str | None:
    tag = soup.find("a", rel="next") or soup.find("link", rel="next")
    if not tag:
        for a in soup.find_all("a", href=True):
            label = (a.get_text(" ", strip=True) + " " + (a.get("aria-label") or "")).lower()
            if re.fullmatch(r"\s*(next( page)?|›|»|>)\s*", label) or "next page" in label:
                tag = a
                break
    if tag and tag.get("href"):
        nxt = urljoin(url, tag["href"])
        return nxt if nxt != url else None
    return None


def page_text(html: str, limit: int) -> str:
    """Readable main-content text (parses its own copy, since it strips tags)."""
    soup = soupify(html)
    for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer", "iframe", "select",
                     "option", "button", "aside", "dialog", "template", "picture", "img"]):
        tag.decompose()
    for tag in soup.select('[role=navigation], [class*=cookie], [id*=cookie], [class*=breadcrumb]'):
        tag.decompose()
    # Use <main> only when it holds most of the page's text; some sites put just a promoted item there.
    body = soup.body or soup
    main = soup.find("main") or soup.find(attrs={"role": "main"})
    body_text = body.get_text("\n", strip=True)
    root = main if main is not None and len(main.get_text(strip=True)) > 0.5 * len(body_text) else body
    text = re.sub(r"\n\s*\n+", "\n", root.get_text("\n", strip=True))
    return text[:limit]


def page_links_for_ai(soup: BeautifulSoup, base: str, limit: int = 60) -> str:
    """A compact list of the page's links so the AI can attach booking/detail URLs."""
    lines = []
    for a in soup.find_all("a", href=True):
        t = a.get_text(" ", strip=True)
        if t and len(t) > 3:
            lines.append(f"{t[:80]} -> {urljoin(base, a['href'])}")
        if len(lines) >= limit:
            break
    return "\n".join(lines)


EB_ORG_RE = re.compile(r"eventbrite\.[a-z.]+/o/[a-z0-9-]*?-?(\d{8,14})", re.I)


def eventbrite_organisers(soup: BeautifulSoup) -> set[str]:
    ids = set()
    for a in soup.find_all("a", href=True):
        m = EB_ORG_RE.search(a["href"])
        if m:
            ids.add(m.group(1))
    return ids
