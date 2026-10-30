"""Find each council's events pages automatically.

    python -m monitor.discover                 # all councils
    python -m monitor.discover --council Perth # councils whose name contains "Perth"

Writes config/sources.json (used by the daily collector) and reports/discovery.md.
"""
from __future__ import annotations

import argparse
import gzip
import logging
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from . import __version__
from .common import CONFIG, REPORTS, Http, load_json, load_yaml, save_json, setup_logging
from .extractors import (EVENT_WORDS, NOT_EVENTS, detail_links, eventbrite_organisers, find_ical_links,
                         parse_jsonld, same_site, soupify)
from .normalise import PERTH

log = logging.getLogger("monitor")
SOURCES_PATH = CONFIG / "sources.json"

LISTING_NAMES = {"events", "whats-on", "what-s-on", "whatson", "event-calendar", "events-calendar",
                 "calendar", "library-events", "whats-on-calendar", "upcoming-events", "community-events",
                 "library-programs", "programs-and-events", "events-and-activities", "what-s-on-calendar"}
DATE_RE = re.compile(
    r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)|"
    r"\b\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+20\d\d|"
    r"\b\d{1,2}/\d{1,2}/20\d\d", re.I)


def url_score(url: str) -> int:
    p = urlparse(url)
    segs = [s.lower() for s in p.path.split("/") if s]
    if not segs or NOT_EVENTS.search(url):
        return -99
    path = "/".join(segs)
    score = 0
    if segs[-1] in LISTING_NAMES:
        score += 6
    if "library" in path or "libraries" in path:
        score += 2 if EVENT_WORDS.search(path) else 0
    if "event" in path:
        score += 2
    if "whats-on" in path or "what-s-on" in path or "whatson" in path:
        score += 3
    if len(segs) > 3:
        score -= 2 * (len(segs) - 3)  # deep pages are usually single events, not listings
    if p.query:
        score -= 2
    return score


def sitemap_urls(http: Http, base: str, limit: int = 4000) -> list[str]:
    roots = []
    http.allowed(base)  # loads robots.txt for this host (and its Sitemap: lines)
    rp = http._robots.get(f"{urlparse(base).scheme}://{urlparse(base).netloc}")
    if rp is not None:
        roots = list(rp.site_maps() or [])
    roots = roots or [urljoin(base, "/sitemap.xml")]
    seen, urls, queue = set(), [], list(roots)
    while queue and len(seen) < 12 and len(urls) < limit:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        r = http.get(sm, check_robots=False)
        if r is None:
            continue
        content = r.content
        if sm.endswith(".gz"):
            try:
                content = gzip.decompress(content)
            except OSError:
                pass
        soup = BeautifulSoup(content, "xml")
        if soup.find("sitemapindex"):
            children = [loc.get_text(strip=True) for loc in soup.select("sitemap > loc")]
            # Prefer page/content sitemaps over image/news ones.
            children.sort(key=lambda u: (("event" not in u.lower()) and ("page" not in u.lower()), u))
            queue.extend(children)
        else:
            urls.extend(loc.get_text(strip=True) for loc in soup.select("url > loc"))
    return urls[:limit]


def homepage_candidates(soup: BeautifulSoup, base: str) -> list[str]:
    out = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"]).split("#")[0]
        if not href.startswith("http") or not same_site(href, base):
            continue
        text = a.get_text(" ", strip=True).lower()
        if EVENT_WORDS.search(href) or re.search(r"what'?s on|events|library|libraries", text):
            out.append(href)
    return list(dict.fromkeys(out))


def probe(http: Http, url: str) -> dict | None:
    r = http.get(url)
    if r is None or "html" not in r.headers.get("content-type", "html"):
        return None
    html = r.text
    soup = soupify(html)
    jsonld = parse_jsonld(soup, r.url)
    links = detail_links(soup, r.url)
    icals = find_ical_links(soup, r.url)
    dates = len(DATE_RE.findall(soup.get_text(" ", strip=True)))
    return {"url": r.url, "jsonld": len(jsonld), "detail_links": len(links), "dates": dates,
            "ical_links": icals[:3], "eventbrite": sorted(eventbrite_organisers(soup)),
            "library_links": [u for u in homepage_candidates(soup, r.url)
                              if "librar" in u.lower() and EVENT_WORDS.search(u)][:5]}


def verify_ical(http: Http, url: str) -> bool:
    r = http.get(url)
    return r is not None and r.content.lstrip()[:15].upper().startswith(b"BEGIN:VCALENDAR")


def discover_council(http: Http, c: dict) -> dict:
    name = c["name"]
    result = {"name": name, "region": c.get("region", ""), "homepage": c["homepage"],
              "sources": [], "status": "ok", "notes": [], "eventbrite": []}
    excludes = c.get("exclude") or []

    r = http.get(c["homepage"])
    if r is None:
        result["status"] = "unreachable"
        result["notes"].append("Homepage could not be fetched; check the URL in councils.yaml")
        return result
    base = r.url
    result["final_url"] = base
    home = soupify(r.text)

    if c.get("pin"):
        candidates = list(c["pin"])
        result["notes"].append("Using pinned pages from councils.yaml")
    else:
        candidates = homepage_candidates(home, base) + [u for u in sitemap_urls(http, base) if EVENT_WORDS.search(u)]
        candidates = [u for u in dict.fromkeys(candidates)
                      if same_site(u, base) and not any(u.startswith(x) for x in excludes)]
        candidates.sort(key=url_score, reverse=True)
        candidates = [u for u in candidates if url_score(u) > 0][:10]

    probed, seen = [], set()
    queue = list(candidates)
    while queue and len(probed) < 14:
        u = queue.pop(0)
        if u in seen:
            continue
        seen.add(u)
        info = probe(http, u)
        if not info:
            continue
        info["url_score"] = url_score(info["url"])
        info["score"] = (info["url_score"] + min(info["jsonld"], 20) + min(info["detail_links"], 30) / 3
                         + min(info["dates"], 30) / 3 + (6 if info["ical_links"] else 0))
        probed.append(info)
        result["eventbrite"].extend(info["eventbrite"])
        for lib in info["library_links"]:  # one hop into library sections
            if lib not in seen and not any(lib.startswith(x) for x in excludes):
                queue.append(lib)

    good = [p for p in probed if c.get("pin") or p["jsonld"] or p["detail_links"] >= 3 or p["dates"] >= 5 or p["ical_links"]]
    good.sort(key=lambda p: p["score"], reverse=True)
    chosen = []
    def path_of(u):
        return urlparse(u).path.rstrip("/").lower()

    for p in good:
        # Skip repeats, filtered variants (?c=4) and single events beneath a listing already chosen.
        if any(path_of(p["url"]) == path_of(q["url"]) or path_of(p["url"]).startswith(path_of(q["url"]) + "/")
               for q in chosen):
            continue
        chosen.append(p)
        if len(chosen) >= (len(c.get("pin") or []) or 3):
            break

    for p in chosen:
        src = {"url": p["url"], "method": "html", "score": round(p["score"], 1),
               "signals": {k: p[k] for k in ("jsonld", "detail_links", "dates")}}
        for ical in p["ical_links"]:
            if verify_ical(http, ical):
                src.update(method="ical", ical_url=ical)
                break
        result["sources"].append(src)

    result["eventbrite"] = sorted(set(result["eventbrite"]) | eventbrite_organisers(home))
    if not result["sources"]:
        result["status"] = "no_events_page"
        result["notes"].append("No events page found; add one under pin: in councils.yaml")
    log.info("%-32s %d source(s)%s", name, len(result["sources"]),
             "" if result["sources"] else "  <-- needs attention")
    return result


def write_report(data: dict) -> None:
    lines = [f"# Discovery report", "",
             f"Generated {data['generated']} by v{__version__}. "
             "Fix anything marked ⚠ by adding `pin:` or `exclude:` entries in `config/councils.yaml`.", "",
             "| Council | Status | Event pages found | Method |", "|---|---|---|---|"]
    for c in data["councils"]:
        flag = "" if c["status"] == "ok" else "⚠ "
        pages = "<br>".join(s["url"] for s in c["sources"]) or "—"
        methods = "<br>".join(s["method"] for s in c["sources"]) or "—"
        lines.append(f"| {c['name']} | {flag}{c['status']} | {pages} | {methods} |")
    orgs = data.get("eventbrite_organisers", [])
    lines += ["", f"## Eventbrite organisers found on council pages ({len(orgs)})", ""]
    lines += [f"- `{o['id']}` (linked from {', '.join(o['found_on'])}) — "
              f"https://www.eventbrite.com.au/o/{o['id']}" for o in orgs] or ["None found."]
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "discovery.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--council", help="only councils whose name contains this text")
    args = ap.parse_args(argv)
    setup_logging()
    settings = load_yaml("settings.yaml")
    http = Http(delay=float(settings.get("request_delay_seconds", 1.5)))
    councils = [c for c in load_yaml("councils.yaml")["councils"] if not c.get("skip")]
    if args.council:
        councils = [c for c in councils if args.council.lower() in c["name"].lower()]

    previous = {c["name"]: c for c in load_json(SOURCES_PATH, {}).get("councils", [])}
    results = {**previous}
    for c in councils:
        try:
            results[c["name"]] = discover_council(http, c)
        except Exception as exc:  # noqa: BLE001 - one bad site must not stop the rest
            log.exception("Discovery failed for %s", c["name"])
            results[c["name"]] = {**previous.get(c["name"], {"sources": []}), "name": c["name"],
                                  "status": "error", "notes": [str(exc)]}

    order = [c["name"] for c in load_yaml("councils.yaml")["councils"]]
    ordered = [results[n] for n in order if n in results]
    orgs: dict[str, set] = {}
    for c in ordered:
        for oid in c.get("eventbrite", []):
            orgs.setdefault(oid, set()).add(c["name"])
    data = {"generated": datetime.now(PERTH).isoformat(timespec="minutes"), "version": __version__,
            "councils": ordered,
            "eventbrite_organisers": [{"id": k, "found_on": sorted(v)} for k, v in sorted(orgs.items())]}
    save_json(SOURCES_PATH, data)
    write_report(data)
    ok = sum(1 for c in ordered if c.get("sources"))
    log.info("Done: %d of %d councils have event sources; %d Eventbrite organisers; %d requests",
             ok, len(ordered), len(orgs), http.requests_made)


if __name__ == "__main__":
    main()
