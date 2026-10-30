"""Common event model, date/price parsing, categorising and duplicate removal."""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, time, timedelta
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser

PERTH = ZoneInfo("Australia/Perth")


@dataclass
class Event:
    title: str
    start: datetime
    end: datetime | None = None
    all_day: bool = False
    venue: str = ""
    address: str = ""
    lat: float | None = None
    lon: float | None = None
    description: str = ""
    url: str = ""
    booking_url: str = ""
    price_text: str = ""
    price_min: float | None = None
    price_max: float | None = None
    is_free: bool | None = None
    categories: list[str] = field(default_factory=list)
    audience: str = ""        # kids | adults | all
    hint_category: str = ""   # category suggested by the AI step
    hint_audience: str = ""
    council: str = ""
    source: str = ""          # council | eventbrite
    source_url: str = ""
    method: str = ""          # ical | jsonld | ai | api

    # ---- serialisation -------------------------------------------------
    def to_cache(self) -> dict:
        d = asdict(self)
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat() if self.end else None
        return d

    @classmethod
    def from_cache(cls, d: dict) -> "Event":
        names = {f.name for f in fields(cls)}
        d = {k: v for k, v in d.items() if k in names}
        d["start"] = datetime.fromisoformat(d["start"])
        d["end"] = datetime.fromisoformat(d["end"]) if d.get("end") else None
        return cls(**d)

    @property
    def uid(self) -> str:
        key = f"{norm_title(self.title)}|{self.start.date().isoformat()}|{norm_title(self.venue)}"
        return hashlib.sha1(key.encode()).hexdigest()[:12]

    def to_site(self) -> dict:
        s = self.start
        tod = "all-day" if self.all_day else ("morning" if s.hour < 12 else "afternoon" if s.hour < 17 else "evening")
        return {
            "id": self.uid,
            "title": self.title,
            "start": s.isoformat(),
            "end": self.end.isoformat() if self.end else None,
            "all_day": self.all_day,
            "date": s.date().isoformat(),
            "time_of_day": tod,
            "weekday": s.strftime("%a"),
            "weekend": s.weekday() >= 5,
            "venue": self.venue,
            "address": self.address,
            "lat": round(self.lat, 5) if self.lat is not None else None,
            "lon": round(self.lon, 5) if self.lon is not None else None,
            "description": self.description[:400],
            "url": self.url or self.booking_url,
            "booking_url": self.booking_url if self.booking_url != self.url else "",
            "price_text": self.price_text,
            "price_min": self.price_min,
            "price_max": self.price_max,
            "is_free": self.is_free,
            "categories": self.categories,
            "kids": self.audience == "kids",
            "council": self.council,
            "source": self.source,
            "method": self.method,
        }


# ---- text helpers -------------------------------------------------------
_STOP = {"the", "a", "an", "at", "and", "with", "in", "of", "for", "on", "to", "library", "event"}


def clean(text: str | None, limit: int = 0) -> str:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return t[:limit].rstrip() + "…" if limit and len(t) > limit else t


def norm_title(text: str) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split()
    return " ".join(w for w in words if w not in _STOP)


# ---- dates --------------------------------------------------------------
def parse_dt(value) -> tuple[datetime | None, bool]:
    """Return (aware Perth datetime, all_day) from a str/date/datetime."""
    if value is None or value == "":
        return None, False
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        return datetime.combine(value, time(0, 0), PERTH), True
    else:
        s = str(value).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return datetime.combine(date.fromisoformat(s), time(0, 0), PERTH), True
        try:
            dt = dtparser.isoparse(s)
        except (ValueError, OverflowError):
            try:
                dt = dtparser.parse(s, dayfirst=True)
            except (ValueError, OverflowError):
                return None, False
    dt = dt.replace(tzinfo=PERTH) if dt.tzinfo is None else dt.astimezone(PERTH)
    return dt, False


# ---- prices ---------------------------------------------------------------
_PRICE_RE = re.compile(r"\$\s?(\d{1,4}(?:\.\d{1,2})?)")
_FREE_DESC_RE = re.compile(
    r"\b(free (event|entry|admission|session|workshop|concert|talk|class|performance)|"
    r"entry is free|free of charge|cost:?\s*free|this event is free|free to attend)\b")


def parse_price(text: str) -> tuple[float | None, float | None, bool | None]:
    t = (text or "").lower()
    nums = [float(x) for x in _PRICE_RE.findall(t)]
    if nums:
        lo, hi = min(nums), max(nums)
        if re.search(r"\bfree\b", t):
            lo = 0.0
        return lo, hi, hi == 0
    if "gold coin" in t or "donation" in t:
        return 0.0, 2.0, False
    if re.search(r"\bfree\b", t) or "no cost" in t or "no charge" in t:
        return 0.0, 0.0, True
    return None, None, None


def apply_price(ev: Event) -> None:
    if ev.is_free is None and ev.price_min is None:
        lo, hi, free = parse_price(ev.price_text)
        if free is None and _FREE_DESC_RE.search((ev.description or "").lower()):
            lo, hi, free = 0.0, 0.0, True
        ev.price_min, ev.price_max, ev.is_free = lo, hi, free
    if ev.is_free and ev.price_min is None:
        ev.price_min = ev.price_max = 0.0
    if not ev.price_text:
        if ev.is_free:
            ev.price_text = "Free"
        elif ev.price_min is not None:
            ev.price_text = (f"${ev.price_min:g}" if ev.price_max in (None, ev.price_min)
                             else f"${ev.price_min:g}–${ev.price_max:g}")


# ---- categories -----------------------------------------------------------
def _kw_hit(text: str, kw: str) -> bool:
    return re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", text) is not None


def categorise(ev: Event, settings: dict) -> None:
    title = (ev.title or "").lower()
    text = f"{title} {(ev.description or '').lower()}"
    cats: list[str] = []
    for key, spec in settings["categories"].items():
        kws = spec.get("keywords", [])
        if any(_kw_hit(title, k) for k in kws) or sum(_kw_hit(text, k) for k in kws) >= 2:
            cats.append(key)
    if ev.hint_category in settings["categories"] and ev.hint_category not in cats:
        cats.append(ev.hint_category)
    ev.categories = cats or ["other"]

    kids_title = any(_kw_hit(title, k) for k in settings.get("kids_keywords", []))
    kids_hint = ev.hint_audience in ("kids", "teens")
    ev.audience = "kids" if (kids_title or kids_hint) else "all"


# ---- duplicates -------------------------------------------------------------
_METHOD_RANK = {"jsonld": 0, "ical": 1, "api": 2, "ai": 3}


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split())


def _similar(a: str, b: str, pa: str = "", pb: str = "") -> bool:
    """a/b: titles without stop words; pa/pb: plain titles (for 'X' vs 'X - with Y' matches)."""
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(pa), len(pb)) >= 12 and (pa.startswith(pb) or pb.startswith(pa)):
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.84


def _merge(keep: Event, other: Event) -> None:
    for f in ("end", "venue", "address", "lat", "lon", "description", "price_text",
              "price_min", "price_max", "is_free", "url"):
        if getattr(keep, f) in (None, "") and getattr(other, f) not in (None, ""):
            setattr(keep, f, getattr(other, f))
    if other.source == "eventbrite" and not keep.booking_url:
        keep.booking_url = other.url
    elif other.booking_url and not keep.booking_url:
        keep.booking_url = other.booking_url


def dedupe(events: list[Event]) -> list[Event]:
    """Merge the same event listed on several pages or on both a council site and Eventbrite."""
    ordered = sorted(events, key=lambda e: (e.source != "council", _METHOD_RANK.get(e.method, 9)))
    by_day: dict[date, list[tuple[str, str, Event]]] = defaultdict(list)
    out: list[Event] = []
    for ev in ordered:
        nt, pt = norm_title(ev.title), _plain(ev.title)
        day = ev.start.date()
        match = None
        for ont, opt, other in by_day[day]:
            close_in_time = ev.all_day or other.all_day or abs((ev.start - other.start)) <= timedelta(minutes=60)
            if close_in_time and _similar(nt, ont, pt, opt):
                match = other
                break
        if match:
            _merge(match, ev)
        else:
            by_day[day].append((nt, pt, ev))
            out.append(ev)
    return out
