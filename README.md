# NMBS tracking — direct connections from Diest

Live dashboard + automatic daily delay log for the **direct** trains between
Diest and Halle, Brussel-Noord, and Brussel-Centraal — both directions of each
pair — built on the open [iRail API](https://docs.irail.be/).

**🌐 Live dashboard:** https://thbraet.github.io/nmbs_tracking/

## Tracked routes

Three station pairs, each logged in both directions (6 routes total):

| Pair | Slugs |
|------|-------|
| Diest ↔ Halle | `diest-halle`, `halle-diest` |
| Diest ↔ Brussel-Noord | `diest-brussels-north`, `brussels-north-diest` |
| Diest ↔ Brussel-Centraal | `diest-brussels-central`, `brussels-central-diest` |

## Contents

| File | What it is |
|------|------------|
| `train-dashboard.html` | Self-contained dashboard. Open it in a browser. Pick a **connection** and **direction** at the top; live departure boards for the origin & destination station, a "direct tracker" for today's trips, and a multi-day history panel all follow the selection. Refresh button re-pulls live data. |
| `fetch_log.py` | Fetches today's direct trips for every tracked route and writes the log. Run by the daily workflow. |
| `.github/workflows/daily.yml` | GitHub Actions cron job (21:45 UTC ≈ 23:45 Brussels) that runs the script and commits the log. |
| `log/history.csv` | Long-term log: one row per train per day per route (carries a `route` column). |
| `log/<YYYY-MM-DD>.json` | Full per-day snapshot with summary stats, keyed per route. |

## How the daily log works

iRail only exposes **real-time delays for the current day**, so the log must be
captured before midnight Brussels time. The workflow runs late each evening,
collects every direct trip that departed that day across all routes, and commits
the results.

`history.csv` is idempotent per day: re-running (manually via the Actions tab)
replaces that day's rows instead of duplicating them.

## One-time setup

1. Create a GitHub repo and push this folder (see chat instructions).
2. In the repo: **Settings → Actions → General → Workflow permissions** →
   enable **Read and write permissions**.
3. (Optional) Trigger a first run: **Actions → "Daily NMBS train log" → Run workflow**.

## Local run

```bash
pip install certifi
python fetch_log.py
```
