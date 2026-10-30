"""Weekly email digest of newly listed events, sent with Resend.

    python -m monitor.digest            # send
    python -m monitor.digest --preview  # write reports/digest-preview.html, send nothing

Secrets: RESEND_API_KEY, DIGEST_TO (your address), DIGEST_FROM (optional; defaults to
Resend's test sender, which can only deliver to the address your Resend account uses).
"""
from __future__ import annotations

import argparse
import html
import logging
import os
from datetime import datetime

import requests

from . import __version__
from .collect import STATE
from .common import REPORTS, SITE_DATA, load_json, load_yaml, save_json, setup_logging
from .normalise import PERTH, norm_title

log = logging.getLogger("monitor")

BLUE, ORANGE, TEXT, MUTED, BORDER, BG, GREEN = "#24529A", "#D87822", "#191D23", "#505A68", "#C9CED6", "#F3F5F8", "#3E745A"


def _when(e: dict) -> str:
    start = datetime.fromisoformat(e["start"])
    day = start.strftime("%a %-d %b")
    if e["all_day"]:
        return day
    return f"{day}, {start.strftime('%-I:%M%p').lower().replace(':00', '')}"


def _price(e: dict) -> str:
    if e.get("is_free"):
        return f'<span style="color:{GREEN};font-weight:600">Free</span>'
    return html.escape(e.get("price_text") or "Price not listed")


def build(events: list[dict], cats: dict, interests: list[str], settings: dict, health: dict) -> tuple[str, int]:
    max_items = int(settings["digest"].get("max_items_per_category", 25))
    site = settings["digest"].get("site_url") or ""
    parts, counted = [], set()
    for key in interests:
        items = [e for e in events if key in e["categories"]]
        if not items:
            continue
        counted.update(e["id"] for e in items)
        # A weekly session becomes one row listing its dates.
        series: dict[tuple, list[dict]] = {}
        for e in sorted(items, key=lambda e: e["start"]):
            series.setdefault((norm_title(e["title"]), (e.get("venue") or "").lower()), []).append(e)
        groups = sorted(series.values(), key=lambda g: (not g[0].get("is_free"), g[0]["start"]))
        rows = []
        for g in groups[:max_items]:
            e = g[0]
            where = " · ".join(x for x in (e.get("venue"), e.get("council")) if x)
            when = _when(e) if len(g) == 1 else (", ".join(_when(x).split(",")[0] for x in g[:4])
                                                  + (f" and {len(g) - 4} more dates" if len(g) > 4 else ""))
            rows.append(
                f'<tr><td style="padding:10px 0;border-bottom:1px solid {BORDER}">'
                f'<a href="{html.escape(e["url"] or site)}" style="color:{BLUE};font-weight:600;text-decoration:none">'
                f'{html.escape(e["title"])}</a><br>'
                f'<span style="color:{TEXT}">{html.escape(when)} · {_price(e)}</span><br>'
                f'<span style="color:{MUTED};font-size:13px">{html.escape(where)}</span></td></tr>')
        items = groups
        more = len(items) - max_items
        if more > 0:
            rows.append(f'<tr><td style="padding:8px 0;color:{MUTED}">…and {more} more on the website.</td></tr>')
        parts.append(f'<h2 style="font-family:Arial Narrow,Arial,sans-serif;color:{TEXT};font-size:20px;'
                     f'margin:24px 0 4px;border-left:3px solid {ORANGE};padding-left:8px">'
                     f'{html.escape(cats.get(key, key))} ({len(groups)})</h2><table width="100%" '
                     f'cellspacing="0" cellpadding="0">{"".join(rows)}</table>')

    attention = [h for h in health.get("sources", []) if h.get("needs_attention")]
    missing = health.get("councils_without_sources", [])
    if attention or missing:
        li = "".join(f"<li>{html.escape(h['source'])}: {html.escape(h.get('detail') or h['status'])} "
                     f"(<a href=\"{html.escape(h['url'])}\" style=\"color:{BLUE}\">page</a>)</li>" for h in attention)
        li += "".join(f"<li>{html.escape(n)}: no events page found</li>" for n in missing)
        parts.append(f'<h2 style="font-family:Arial Narrow,Arial,sans-serif;color:{TEXT};font-size:18px;'
                     f'margin:28px 0 4px">Sources needing attention</h2>'
                     f'<ul style="color:{TEXT};font-size:14px;padding-left:18px">{li}</ul>')

    total = len(counted)
    link = (f'<p><a href="{html.escape(site)}" style="color:{BLUE};font-weight:600">Open the full list and map</a></p>'
            if site else "")
    body = "".join(parts) or f'<p style="color:{TEXT}">No new events in your categories this week.</p>'
    page = (f'<div style="background:{BG};padding:24px 12px"><div style="max-width:640px;margin:0 auto;'
            f'background:#fff;border:1px solid {BORDER};border-radius:4px;padding:20px 24px;'
            f'font-family:IBM Plex Sans,Segoe UI,Arial,sans-serif;font-size:15px;line-height:1.45">'
            f'<h1 style="font-family:Arial Narrow,Arial,sans-serif;font-size:26px;color:{TEXT};margin:0">'
            f'New Perth events this week</h1>'
            f'<p style="color:{MUTED};margin:4px 0 0">{total} new events in your categories · '
            f'Perth Events Monitor v{__version__}</p>{link}{body}</div></div>')
    return page, total


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--preview", action="store_true", help="write an HTML preview instead of sending")
    args = ap.parse_args(argv)
    setup_logging()
    settings = load_yaml("settings.yaml")
    data = load_json(SITE_DATA / "events.json", {})
    health = load_json(SITE_DATA / "health.json", {})
    state = load_json(STATE, {"first_seen": {}, "last_digest": None})
    since = state.get("last_digest") or "0000"
    new = [e for e in data.get("events", [])
           if e["first_seen"] != "initial" and e["first_seen"] > since[:10] and not e["kids"]]
    page, total = build(new, data.get("categories", {}), data.get("interests", []), settings, health)

    if args.preview:
        REPORTS.mkdir(exist_ok=True)
        (REPORTS / "digest-preview.html").write_text(page, encoding="utf-8")
        log.info("Preview written with %d events", total)
        return

    key, to = os.environ.get("RESEND_API_KEY"), os.environ.get("DIGEST_TO")
    if not key or not to:
        raise SystemExit("Set the RESEND_API_KEY and DIGEST_TO secrets to send the digest")
    sender = os.environ.get("DIGEST_FROM") or "Perth Events <onboarding@resend.dev>"
    r = requests.post("https://api.resend.com/emails", timeout=30,
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      json={"from": sender, "to": [a.strip() for a in to.split(",")],
                            "subject": f"{settings['digest']['subject']} ({total})", "html": page})
    if r.status_code >= 300:
        raise SystemExit(f"Resend refused the email: {r.status_code} {r.text[:300]}")
    state["last_digest"] = datetime.now(PERTH).date().isoformat()
    save_json(STATE, state)
    log.info("Digest sent to %s with %d events", to, total)


if __name__ == "__main__":
    main()
