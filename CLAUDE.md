# Perth Events Monitor: handoff

Automated monitor of council, library and Eventbrite events across Perth metro and Peel (34 local
governments). Runs on GitHub Actions, publishes a filterable list and map on GitHub Pages
(https://ianhay.github.io/perth-events-monitor/), and emails a weekly digest through Resend.
Owner: Ian Hay (GitHub `ianhay`). Read `README.md` for the user-facing setup.

## State at handoff (v0.2.0, 1 Nov 2026)

Working:
- Daily collection, monthly discovery and Friday digest workflows exist; the daily run commits data
  successfully (workflow write permission and Pages are both on).
- Discovery finds event pages for 31 of 34 councils (`reports/discovery.md`, `config/sources.json`).
- iCal and schema.org JSON-LD extraction work (Mosman Park, Kalamunda).
- Website: filters (date, days, time of day, price, category, council, source, children's toggle),
  sort (date, price, recently added, distance), URL-hash state, light and dark themes, MapLibre map.
- 18 offline tests pass (`python -m pytest -q tests`).

Not working yet:
- **Only 7 events are collected.** Almost every council page needs the AI step, which has never run
  successfully: GitHub Models (the original choice) was retired on 30 Jul 2026, and the replacement,
  the Gemini API free tier (`gemini-3.5-flash-lite` via the OpenAI-compatible endpoint), is waiting
  for Ian to add the `GEMINI_API_KEY` secret. The new code has not yet run against the real API.
- Secrets not yet added: `GEMINI_API_KEY`, `RESEND_API_KEY`, `DIGEST_TO`, `EVENTBRITE_TOKEN`.
  The Eventbrite organiser code has never run against the real API either.

## Next tasks, in order

1. **Prove AI extraction locally** on 3 to 4 councils before relying on Actions:
   `GEMINI_API_KEY=... python -m monitor.collect --council Vincent`, then inspect
   `data/ai_debug.json` (page text sent, status, raw reply, events parsed). City of Vincent's
   `/events/` page has dates in plain HTML, so it should yield events; Bassendean too.
   Check the free-tier limits Ian's key actually gets (AI Studio shows them) and tune
   `ai.seconds_between_calls` and `ai.max_calls_per_run` in `config/settings.yaml`.
   A partial `--council` run replaces `docs/data/events.json`; don't commit one.
2. **Pin the three councils with no events page** in `config/councils.yaml` (`pin:` list):
   City of Bayswater, Shire of Serpentine Jarrahdale, City of Subiaco. Open their sites and find
   the what's on / library events pages.
3. **Clean up poor discovery picks.** Examples in the current report: Cockburn's "Choose your own
   adventure" and "Spend time not money" pages and a Business page; Gosnells' single-event pages
   (Learning Launchpad, JPs); Wanneroo's `event_search` page; Armadale's major-events page (the
   real listing is likely elsewhere on `my.armadale.wa.gov.au`). Use `exclude:` / `pin:`, or
   improve `url_score()` and `NOT_EVENTS` in `monitor/discover.py` / `monitor/extractors.py`.
4. **JavaScript-loaded listings.** Joondalup's events calendar returns "There are no results" in the
   raw HTML; events load client-side. For each such site, first look in the browser's network tab
   for the JSON endpoint the page calls and add a small adapter (preferred: cheap and stable).
   Fall back to Playwright in Actions only if needed. Several councils use the same CMS
   (`/events/?c=4` URLs: Bassendean, East Fremantle, Mundaring, Nedlands, Peppermint Grove,
   Victoria Park, Vincent, Murray, Waroona, Boddington), so one platform-specific parser for it
   could replace AI calls for about 10 councils.
5. **Verify Eventbrite** once `EVENTBRITE_TOKEN` exists: `monitor/eventbrite.py` uses
   `GET /v3/organizers/{id}/events/` with `expand=venue,ticket_availability`. Discovery found
   organisers 19043133306 (Bayswater) and 8254682107 (Canning).
6. **Test the digest**: `python -m monitor.digest --preview` writes
   `reports/digest-preview.html`; then run the "Weekly digest" workflow manually once the Resend
   secrets exist. Note: the digest only lists events first seen after the initial run, and the
   first run marked everything `initial`.

## How it fits together

| Path | Role |
|---|---|
| `config/councils.yaml` | Seed list of 34 councils, with optional `pin` / `exclude` / `skip` |
| `config/settings.yaml` | Horizon, politeness, AI settings, geocoding, categories and keywords, kids keywords, digest |
| `config/eventbrite_organisers.yaml` | Extra Eventbrite organisers to follow (IDs) |
| `config/sources.json` | Machine-written by discovery; deleting it makes the next collect re-discover |
| `monitor/discover.py` | Homepage + sitemap crawl, scores candidate pages, probes them, picks up to 3 per council |
| `monitor/collect.py` | Per source: iCal → listing JSON-LD → detail-page JSON-LD → AI on listing pages; then price, categories, dedupe, geocode, first-seen dates, health |
| `monitor/extractors.py` | iCal, JSON-LD, link finding, pagination, page text for the AI |
| `monitor/ai.py` | OpenAI-compatible chat call (Gemini by default), tolerant JSON parsing, content-hash cache, `ai_debug.json` |
| `monitor/normalise.py` | `Event` model, date and price parsing, categorising, duplicate merging |
| `monitor/eventbrite.py` | Official API, followed organisers only (no scraping of Eventbrite search) |
| `monitor/geocode.py` | Nominatim, Perth + Peel viewbox, cached in `data/geocache.json` |
| `monitor/digest.py` | Weekly HTML email via Resend; groups recurring sessions; lists sources needing attention |
| `docs/index.html` | The whole website (single file); reads `docs/data/events.json` and `health.json` |
| `data/` | Working state committed by Actions: caches, `state.json` (first-seen dates, last digest) |

Workflows (all Perth time): `collect.yml` daily 5:43am and on pushes touching config or code;
`discover.yml` monthly; `digest.yml` Fridays 7:37am. All share the `data-update` concurrency group.

## Decisions already made with Ian

- Coverage: Perth metro plus Peel. Interests: concerts and live music, art classes and workshops,
  talks and author events. Children's sessions hidden by default (one toggle shows them).
- Eventbrite: follow specific organisers only, via the official API. No scraping of Eventbrite search.
- AI: free tier only (no paid API). Now Gemini; GitHub Models is gone.
- Alerts: weekly email digest only, through Resend. Hosting: GitHub Actions + Pages, no database.

## Conventions

- **Version stamp**: single source is `monitor/__init__.py` `__version__` (collector) and
  `APP_VERSION` + `<meta name="version">` in `docs/index.html` (website). Bump the patch on every
  change, the minor for new capability, and keep the README's bold version line in step.
- **Visual style**: Ian's cobalt-blue (#24529A / dark #7EA8F2) and signal-orange (#D87822 / dark
  #F29B4B) system, Barlow Condensed headings, IBM Plex Sans body, IBM Plex Mono for metadata,
  4px radii, WCAG 2.2 AA contrast in light and dark modes. Tokens are at the top of `docs/index.html`.
- Be polite to council sites: respect robots.txt (the `Http` class does), keep
  `request_delay_seconds` at 1.5 or more, and keep request counts low.
- Never commit secrets. Keys live only in GitHub repository secrets or local environment variables.
