# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A static dashboard + automated daily delay log for the **direct** (0-transfer) NMBS/SNCB
trains between Diest and Halle, Brussel-Noord, and Brussel-Centraal — both directions of
each pair (6 routes). Built entirely on the open [iRail API](https://docs.irail.be/).
No build step, no server framework — the site is plain HTML files served from GitHub Pages
(https://thbraet.github.io/nmbs_tracking/) and a single Python script run by a daily cron.

## Commands

```bash
# Run the daily collector locally (writes log/<today>.json + upserts log/history.csv)
pip install certifi
python fetch_log.py

# Serve the dashboards locally — the history panel fetches log/history.csv, which
# fails under file:// (CORS), so it MUST be served over http:
python -m http.server 8000     # then open http://localhost:8000/
```

There are no tests, linters, or package manifests. `fetch_log.py` uses only the standard
library plus optional `certifi`.

> Corporate-proxy caveat (see user memory): local Python HTTPS may fail with a cert error
> behind the corporate TLS proxy. If `fetch_log.py` can't reach iRail locally, that's the
> environment, not the code — the GitHub Actions run is the source of truth.

## Architecture

Three pieces share one contract: the **route slugs**.

1. **`fetch_log.py`** — the collector. Iterates `ROUTES` (slug + from/to station names),
   paginates the iRail `/connections` endpoint across the whole day, keeps only direct trips
   that have already departed, and writes outputs. Run by the daily workflow.

2. **`.github/workflows/daily.yml`** — GitHub Actions cron. GitHub cron is UTC-only but we
   want 23:45 *Brussels* time year-round, so it fires at **both** 21:45 and 22:45 UTC and a
   DST-aware gate step proceeds only on the trigger that lands on the 23:00 Brussels hour
   (manual `workflow_dispatch` runs always proceed). 23:45 is late enough to capture a full
   day yet still "today" in Brussels, which matters because **iRail only exposes real-time
   delays for the current day** — that's the whole reason this must run late each evening.
   Needs `contents: write` permission to commit the log back.

   After the connection sweep, the collector additionally walks the **diest-halle** and
   **halle-diest** routes' trains, fetches each train's full stop list from iRail's
   `/vehicle` endpoint, slices it to the **Diest⇄Halle segment** (inclusive) via
   `extract_segment`, and logs per-stop delays. Those two routes are exactly the trains that
   serve both endpoints, so one vehicle fetch per train covers the whole trajectory (both
   Brussels stops included) without duplication.

3. **The HTML dashboards** (`index.html` landing page → `train-dashboard.html`,
   `live-board.html`, `stop-analysis.html`) — self-contained, no dependencies. They call
   iRail's `/liveboard`, `/connections` and `/vehicle` endpoints directly from the browser
   for *live* data. `train-dashboard.html` additionally fetches `log/history.csv` for its
   multi-day history panel; `stop-analysis.html` (the Diest⇄Halle stop-by-stop deep dive)
   fetches `log/stops_history.csv` for past days and computes *today* live in-browser by
   enumerating today's direct trains via `/connections` then fetching each `/vehicle`.

### Data outputs (under `log/`)
- `log/<YYYY-MM-DD>.json` — full per-day snapshot, **overwritten** each run, keyed per route
  (`routes.<slug>.summary` + `.trips`).
- `log/history.csv` — long-term log, one row per train per day per route, carries a `route`
  column. **Idempotent per day**: a run drops all existing rows for today's date and
  re-writes them, so re-running never duplicates.
- `log/stops_history.csv` — per-stop trajectory log for the Diest⇄Halle deep dive, one row
  per train **per stop** per day, carries a `direction` column (`diest-halle`/`halle-diest`)
  and `seq` (0-based stop index along the segment). Schema is `STOPS_CSV_COLS`. Same
  idempotent-per-day upsert as `history.csv`.

## Conventions that matter

- **Route slugs are a cross-file contract.** `ROUTES[].slug` in `fetch_log.py` is written
  into `history.csv`'s `route` column and used by `train-dashboard.html` to filter history.
  Keep slugs stable — changing one orphans historical rows and breaks the dashboard filter.
- **Times are Brussels-local.** The collector works in `Europe/Brussels` (`zoneinfo`); the
  workflow gate uses `TZ=Europe/Brussels`. Don't introduce naive/UTC timestamps.
- **iRail politeness is deliberate.** All requests go through `_get_json()`, which throttles
  to ~1.4 req/s (`MIN_REQUEST_INTERVAL`), sends a descriptive `User-Agent`, and backs off on
  429/5xx/network errors honoring `Retry-After`. A full run is the 6-route connection sweep
  **plus one `/vehicle` fetch per Diest⇄Halle train** (~30/day) — still well under iRail's
  ~3 req/s cap. Preserve the throttle/backoff when editing fetch logic. The browser-side
  live fetch in `stop-analysis.html` caps `/vehicle` concurrency at 3 for the same reason.
- **The Diest⇄Halle segment is a second cross-file contract.** `SEGMENT_ENDPOINTS` and the
  station names emitted into `stops_history.csv`'s `stop` column must match the `STOP_ORDER`
  /`STOP_META` tables in `stop-analysis.html` (e.g. `Brussels-South/Brussels-Midi`). Direction
  is inferred from the relative order of Diest vs Halle in the train's full stop list. A
  vehicle whose `/vehicle` detail lacks both endpoints (e.g. `JourneyNotFoundException`) is
  skipped, not fatal.
- **CSV schema** is `CSV_COLS` in `fetch_log.py`. The upsert migrates legacy rows that
  predate the `route`/`from`/`to` columns by defaulting them to the original `diest-halle`
  route — keep that migration if you touch `write_outputs`.
