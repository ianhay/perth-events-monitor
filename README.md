# Perth Events Monitor

**v0.1.2** · Collects free and low-cost events (concerts, art classes, talks and more) from council
and library websites across Perth and Peel, plus Eventbrite organisers you follow. Runs by itself on
GitHub Actions, publishes a filterable list and map on GitHub Pages, and emails a weekly digest of
new events.

## What runs when

| Workflow | When (Perth time) | What it does |
|---|---|---|
| **Collect events** | Daily, 5:43am | Reads every council source and Eventbrite organiser, merges duplicates, updates the website data |
| **Find event pages** | 1st of each month, 4:17am | Re-finds each council's events and library pages, in case sites have changed |
| **Weekly digest** | Fridays, 7:37am | Emails events added since the last digest in your categories, via Resend |

Everything is free: GitHub Actions and Pages, GitHub Models for AI extraction, the Resend free tier,
and OpenStreetMap geocoding.

## Setup (about 15 minutes)

1. **Create a GitHub repository** (public is simplest: Pages and Actions minutes are free) and push
   this folder to it.
2. **Allow the workflows to save data:** *Settings → Actions → General → Workflow permissions →
   Read and write permissions*.
3. **Turn on the website:** *Settings → Pages → Build and deployment → Deploy from a branch →
   `main` / `/docs`*. Note the site URL and put it in `config/settings.yaml` under `digest.site_url`.
4. **Set up Resend** at resend.com (free). Create an API key, then add these under
   *Settings → Secrets and variables → Actions → New repository secret*:
   - `RESEND_API_KEY` – the key
   - `DIGEST_TO` – your email address
   - `DIGEST_FROM` – optional. Without it the digest comes from Resend's shared test sender, which only
     delivers to the address your Resend account is registered with. To send from your own address,
     verify a domain in Resend and set e.g. `Perth Events <events@yourdomain.com>`.
5. **Eventbrite (optional):** in Eventbrite go to *Account settings → Developer links → API keys*,
   copy your **private token**, and add it as the secret `EVENTBRITE_TOKEN`. Without it, Eventbrite is
   skipped and everything else still works.
6. **First run:** *Actions → Collect events → Run workflow*. Because there's no
   `config/sources.json` yet, it first finds every council's event pages (20–30 minutes), then collects.
7. **Check the discovery report** in `reports/discovery.md`. Councils marked ⚠ need a page pinned by
   hand (see below). Most councils should be found automatically.

### Free AI extraction

Pages with no calendar feed or structured event data are read by a small AI model through
[GitHub Models](https://github.com/marketplace/models), using the workflow's built-in token (the
workflow already requests `models: read`). One call reads a whole listing page, and results are
cached by page content, so unchanged pages never cost a call. The free tier has a daily request cap;
if it's reached, remaining pages keep yesterday's results and are retried the next day. The first
few runs may take a couple of days to fill in completely.

If the run log says GitHub Models refused the token, create a fine-grained personal access token
with only **Models: read** permission and add it as the secret `MODELS_TOKEN`.

## Fixing or adding sources

`config/councils.yaml` lists the 34 Perth metro and Peel councils. Discovery fills in the rest. To
correct it for a council, add keys to its entry:

```yaml
  - name: City of Melville
    region: metro
    homepage: https://www.melvillecity.com.au
    pin:                     # always use these pages
      - https://www.melvillecity.com.au/things-to-do/whats-on
    exclude:                 # never use pages starting with these
      - https://www.melvillecity.com.au/our-city/news
```

Then run *Find event pages* with that council's name to refresh just that one.

**Eventbrite organisers:** discovery automatically follows any Eventbrite organiser linked from a
council or library page (listed in the discovery report). Add others in
`config/eventbrite_organisers.yaml` using the number at the end of their profile URL.

## Tuning

`config/settings.yaml` controls:

- `interests` – the categories the website and digest default to (currently concerts and live
  music, art classes and workshops, talks and author events)
- category keywords, and `kids_keywords` for children's sessions (hidden by default, one toggle shows them)
- how far ahead to look (`horizon_days`), request politeness, and the AI call budget

## Health checks

Each run records per-source results in `docs/data/health.json`. A source is flagged when it errors,
or returns no events twice in a row after previously returning some (usually a council redesign).
Flagged sources appear at the top of the website and at the end of the weekly digest.

## Running locally

```bash
pip install -r requirements.txt
python -m monitor.discover --council Perth   # find pages for one council
python -m monitor.collect --council Perth    # collect just that council (replaces the event list)
python -m monitor.digest --preview           # writes reports/digest-preview.html, sends nothing
python -m http.server -d docs 8000           # view the site at http://localhost:8000
python -m pytest -q tests                    # offline tests
```

For AI extraction locally, set `MODELS_TOKEN` to a token with *Models: read*.

## How it works

1. **Discovery** starts from each council homepage, reads its sitemap and navigation for events,
   what's on and library pages, opens the most promising ones, and keeps up to three pages per
   council that actually list events. It prefers an iCal feed when the page offers one.
2. **Collection** reads each source in the cheapest reliable way: iCal feed → schema.org event data
   on the listing → schema.org data on individual event pages → AI extraction of the listing page.
   It respects `robots.txt` and waits between requests to the same site.
3. **Normalising** gives every event the same fields (time, venue, price, categories), parses prices
   like "Free, bookings required" or "$10–$25", and tags children's sessions.
4. **Merging** removes duplicates, such as an event listed on both the council site and Eventbrite
   (the council listing is kept, with the Eventbrite link as the booking link).
5. **Geocoding** places venues on the map using OpenStreetMap Nominatim (cached).
6. The **website** (`docs/index.html`) filters by date, days, time of day, price, category, council
   and source; sorts by date, price, recently added or distance; and saves filters in the URL so a
   view can be bookmarked.

Always check the organiser's page before going: council listings change and the extraction can
occasionally misread a page.
