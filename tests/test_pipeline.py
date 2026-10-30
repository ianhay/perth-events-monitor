"""Offline tests: parsers, pricing, categories, duplicates and a full collect run with fake websites."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from monitor import collect as collect_mod
from monitor import common
from monitor.extractors import detail_links, find_ical_links, next_page, parse_ical, parse_jsonld, soupify
from monitor.normalise import PERTH, Event, apply_price, categorise, dedupe, parse_price

SETTINGS = common.load_yaml("settings.yaml")
SOON = (datetime.now(PERTH) + timedelta(days=5)).replace(hour=19, minute=0, second=0, microsecond=0)
D = SOON.strftime("%Y-%m-%d")

LISTING = f"""<html><body><main><h1>What's on</h1>
<a href="/whats-on/jazz-in-the-library">Jazz in the Library</a>
<a href="/whats-on/rhyme-time">Rhyme Time</a>
<a href="/whats-on/watercolour-workshop">Watercolour workshop</a>
<a href="/news/latest">News</a><a href="/council-meetings/agenda">Agenda</a>
<a href="/whats-on?page=2" rel="next">Next</a>
<a href="https://www.eventbrite.com.au/o/riverside-library-friends-12345678901">Friends</a>
<a href="/whats-on/calendar.ics">Subscribe (iCal)</a>
</main></body></html>"""

DETAIL = """<html><head><script type="application/ld+json">{
 "@context":"https://schema.org","@type":"MusicEvent","name":"Jazz in the Library",
 "startDate":"%sT19:00:00+08:00","endDate":"%sT21:00:00+08:00",
 "location":{"@type":"Place","name":"Riverside Library","address":{"streetAddress":"1 River Rd","addressLocality":"Applecross"},
   "geo":{"latitude":-32.01,"longitude":115.83}},
 "offers":{"@type":"Offer","price":"0","priceCurrency":"AUD"},
 "description":"An evening concert with the Swan River Jazz Trio."}</script></head><body>x</body></html>""" % (D, D)

ICS = f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:1@test
SUMMARY:Author talk: Tim Winton in conversation
DTSTART;TZID=Australia/Perth:{SOON.strftime('%Y%m%dT180000')}
DTEND;TZID=Australia/Perth:{SOON.strftime('%Y%m%dT193000')}
LOCATION:Fremantle Library, 70 Parry St, Fremantle
DESCRIPTION:Free event. Bookings essential.
END:VEVENT
BEGIN:VEVENT
UID:2@test
SUMMARY:Baby Rhyme Time
DTSTART;TZID=Australia/Perth:{SOON.strftime('%Y%m%dT100000')}
RRULE:FREQ=WEEKLY;COUNT=3
LOCATION:Fremantle Library
END:VEVENT
END:VCALENDAR
"""


def test_jsonld_and_price():
    evs = parse_jsonld(soupify(DETAIL), "https://example.wa.gov.au/whats-on/jazz-in-the-library")
    assert len(evs) == 1
    ev = evs[0]
    assert ev.venue == "Riverside Library" and ev.is_free is True and ev.lat == -32.01
    apply_price(ev)
    categorise(ev, SETTINGS)
    assert ev.price_text == "Free" and "music" in ev.categories and ev.audience == "all"


def test_listing_links():
    soup = soupify(LISTING)
    links = detail_links(soup, "https://example.wa.gov.au/whats-on")
    assert links == ["https://example.wa.gov.au/whats-on/jazz-in-the-library",
                     "https://example.wa.gov.au/whats-on/rhyme-time",
                     "https://example.wa.gov.au/whats-on/watercolour-workshop"]
    assert next_page(soup, "https://example.wa.gov.au/whats-on").endswith("page=2")
    assert find_ical_links(soup, "https://example.wa.gov.au/whats-on") == ["https://example.wa.gov.au/whats-on/calendar.ics"]


def test_ical_with_recurrence():
    evs = parse_ical(ICS.encode(), 90)
    titles = [e.title for e in evs]
    assert titles.count("Baby Rhyme Time") == 3
    talk = next(e for e in evs if e.title.startswith("Author"))
    assert talk.venue == "Fremantle Library" and talk.address.startswith("70 Parry")
    apply_price(talk)
    categorise(talk, SETTINGS)
    assert talk.is_free and "talk" in talk.categories
    baby = next(e for e in evs if e.title == "Baby Rhyme Time")
    categorise(baby, SETTINGS)
    assert baby.audience == "kids"


@pytest.mark.parametrize("text,expected", [
    ("Free, bookings required", (0.0, 0.0, True)),
    ("$15", (15.0, 15.0, False)),
    ("$10 - $25.50", (10.0, 25.5, False)),
    ("Gold coin donation", (0.0, 2.0, False)),
    ("Free for members, $12 non-members", (0.0, 12.0, False)),
    ("", (None, None, None)),
])
def test_parse_price(text, expected):
    assert parse_price(text) == expected


def test_dedupe_council_and_eventbrite():
    a = Event(title="Jazz in the Library", start=SOON, venue="Riverside Library", source="council", method="jsonld")
    b = Event(title="Jazz in the Library - Swan River Jazz Trio", start=SOON + timedelta(minutes=30),
              source="eventbrite", method="api", url="https://www.eventbrite.com.au/e/123")
    c = Event(title="Jazz in the Library", start=SOON + timedelta(days=7), source="council")
    out = dedupe([b, a, c])
    assert len(out) == 2
    merged = next(e for e in out if e.start == SOON)
    assert merged.source == "council" and merged.booking_url == "https://www.eventbrite.com.au/e/123"


class FakeResponse:
    def __init__(self, url, text, ctype="text/html"):
        self.url, self.text, self.content = url, text, text.encode()
        self.headers = {"content-type": ctype}
        self.status_code = 200


class FakeHttp:
    """Serves fixture pages instead of the internet."""
    pages = {}

    def __init__(self, *a, **k):
        self.requests_made = 0

    def get(self, url, **kw):
        self.requests_made += 1
        if url in self.pages:
            return FakeResponse(url, self.pages[url])
        return None


def test_full_collect_run(tmp_path, monkeypatch):
    base = "https://example.wa.gov.au"
    plain_listing = (f"<html><body><main><h2>Life drawing for adults</h2><p>{SOON.strftime('%A %d %B %Y')} 6pm, "
                     "Town Hall. $12.</p></main></body></html>")
    FakeHttp.pages = {
        f"{base}/whats-on": LISTING,
        f"{base}/whats-on/jazz-in-the-library": DETAIL,
        f"{base}/whats-on/rhyme-time": "<html>no data</html>",
        f"{base}/whats-on/watercolour-workshop": "<html>no data</html>",
        f"{base}/library/events": plain_listing,
        f"{base}/cal.ics": ICS,
    }
    for name in ("DATA", "SITE_DATA"):
        monkeypatch.setattr(common, name, tmp_path / name.lower())
    monkeypatch.setattr(collect_mod, "SITE_DATA", tmp_path / "site_data")
    monkeypatch.setattr(collect_mod, "PAGE_CACHE", tmp_path / "page_cache.json")
    monkeypatch.setattr(collect_mod, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(collect_mod, "Http", FakeHttp)
    monkeypatch.setattr("monitor.ai.CACHE_PATH", tmp_path / "ai.json")
    monkeypatch.setattr("monitor.geocode.CACHE_PATH", tmp_path / "geo.json")
    monkeypatch.setenv("GITHUB_TOKEN", "test")
    monkeypatch.delenv("EVENTBRITE_TOKEN", raising=False)

    ai_calls = []

    class FakePost:
        status_code = 200
        headers = {}
        text = "{}"

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({"events": [
                {"title": "Life drawing for adults", "start": SOON.strftime("%Y-%m-%dT18:00"), "venue": "Town Hall",
                 "price_text": "$12", "audience": "adults", "category": "art_workshop", "url": ""}]})}}]}

    def fake_post(url, **kw):
        ai_calls.append(url)
        return FakePost()

    monkeypatch.setattr("monitor.ai.requests.post", fake_post)
    monkeypatch.setattr("monitor.ai.time.sleep", lambda s: None)
    sources = {"councils": [
        {"name": "City of Example", "sources": [
            {"url": f"{base}/whats-on", "method": "html"},
            {"url": f"{base}/library/events", "method": "html"},
            {"url": f"{base}/events-feed", "method": "ical", "ical_url": f"{base}/cal.ics"}]},
        {"name": "Shire of Nowhere", "sources": []}],
        "eventbrite_organisers": [{"id": "12345678901", "found_on": ["City of Example"]}]}
    src_path = tmp_path / "sources.json"
    src_path.write_text(json.dumps(sources))
    monkeypatch.setattr(collect_mod, "SOURCES_PATH", src_path)
    settings = {**SETTINGS, "geocode": {"enabled": False}}

    collect_mod.Collector(settings).run()
    out = json.loads((tmp_path / "site_data" / "events.json").read_text())
    titles = sorted({e["title"] for e in out["events"]})
    assert titles == ["Author talk: Tim Winton in conversation", "Baby Rhyme Time",
                      "Jazz in the Library", "Life drawing for adults"]
    assert len(ai_calls) == 1  # only the listing page with no structured data
    life = next(e for e in out["events"] if e["title"].startswith("Life"))
    assert life["price_min"] == 12 and "art_workshop" in life["categories"] and life["time_of_day"] == "evening"
    assert all(e["first_seen"] == "initial" for e in out["events"])
    assert any(e["kids"] for e in out["events"])
    health = json.loads((tmp_path / "site_data" / "health.json").read_text())
    assert health["councils_without_sources"] == ["Shire of Nowhere"]
    assert any(h["status"] == "skipped" for h in health["sources"])  # Eventbrite without a token

    # Second run: AI result comes from cache, and events are no longer "initial".
    collect_mod.Collector(settings).run()
    assert len(ai_calls) == 1


def test_page_text_keeps_aspnet_form_content():
    from monitor.extractors import page_text
    html = ("<html><body><form id='aspnetForm'><nav>Menu</nav><main><h1>What's on</h1>"
            "<select><option>Location A</option></select><div class='event'>Chatty Cafe 30 October 2026</div>"
            "</main></form></body></html>")
    text = page_text(html, 5000)
    assert "Chatty Cafe 30 October 2026" in text and "Location A" not in text and "Menu" not in text


@pytest.mark.parametrize("reply,count", [
    ('{"events": [{"title": "A", "start": "2026-11-01"}]}', 1),
    ('```json\n{"events": [{"title": "A", "start": "2026-11-01"}]}\n```', 1),
    ('Here you go: {"events": []}', 0),
    ('[{"title": "A", "start": "2026-11-01"}]', 1),
    ("not json", None),
])
def test_parse_ai_reply(reply, count):
    from monitor.ai import parse_reply
    out = parse_reply(reply)
    assert (None if out is None else len(out)) == count


def test_ai_retries_plain_mode_after_empty_json_reply(monkeypatch, tmp_path):
    from monitor import ai
    monkeypatch.setattr(ai, "CACHE_PATH", tmp_path / "c.json")
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    seen = []

    class R:
        status_code, headers, text = 200, {}, "{}"

        def __init__(self, content):
            self.content = content

        def json(self):
            return {"choices": [{"message": {"content": self.content}}]}

    def post(url, json, **kw):
        seen.append("response_format" in json)
        return R("" if "response_format" in json else
                 '{"events": [{"title": "Jazz night", "start": "%s"}]}' % SOON.strftime("%Y-%m-%dT19:00"))

    monkeypatch.setattr(ai.requests, "post", post)
    x = ai.AIExtractor(SETTINGS)
    assert [e.title for e in x.extract("page", "", "https://a.wa.gov.au/events")] == ["Jazz night"]
    assert seen == [True, False]
    x.extract("other page", "", "https://a.wa.gov.au/events2")
    assert seen == [True, False, False]  # stays in plain mode for the rest of the run
