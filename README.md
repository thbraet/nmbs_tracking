# NMBS tracking — Diest / Leuven / Halle ⇄ Brussels

Live dashboards + an automatic daily delay log for the **direct** trains across a
small monitored network, built on the open [iRail API](https://docs.irail.be/).

**🌐 Live dashboards:** https://thbraet.github.io/nmbs_tracking/

## What's tracked

Six monitored stations: **Diest, Leuven, Halle** (the *anchors*) and
**Brussel-Noord, -Centraal, -Zuid** (pass-through).

A train is logged only if its trajectory visits **at least one anchor** *and*
**at least two monitored stations**. That keeps the anchored trajectories you
care about (e.g. a Knokke→Liège IC passing Brussel-Zuid → -Centraal → -Noord →
Leuven is tracked for its Brussels⇄Leuven segment) while dropping the firehose
of trains that merely clip two Brussels stations with no anchor on the route.

Each kept train is sliced to its **segment** — the span from the first to the
last monitored stop (inclusive, intermediary stops kept) — labelled `origin → dest`.

## Contents

| File | What it is |
|------|------------|
| `index.html` | Landing page linking the two dashboards. |
| `live-board.html` | Live departure/arrival board for all six stations, with a departures⇄arrivals toggle. Auto-refreshes every 60 s. |
| `segment-explorer.html` | Pick any tracked segment: day summary, per-stop delay table, a train×stop delay-propagation matrix, and a multi-day heatmap. Today is computed live in the browser; past days come from the log. |
| `fetch_log.py` | The daily collector. Discovers the day's direct trains across the network and logs their per-stop delays. Run by the workflow. |
| `migrate_logs.py` | One-shot migration from the old per-route schema (already run). |
| `.github/workflows/daily.yml` | GitHub Actions cron (≈23:45 Brussels) that runs the collector and commits the log. |
| `log/trips_history.csv` | Long-term log: one row per train per day (`origin`, `dest`, departure & arrival delay). |
| `log/stops_history.csv` | Per-stop log: one row per train per stop per day along the segment. |

## How the daily log works

iRail only exposes **real-time delays for the current day**, so the log must be
captured before midnight Brussels time. The workflow runs late each evening,
sweeps the connections endpoint to discover every direct train touching the
network, fetches each one's full stop list, slices it to the monitored segment,
and commits the results.

Both CSVs are idempotent per day: re-running (manually via the Actions tab)
replaces that day's rows instead of duplicating them.

## One-time setup

1. In the repo: **Settings → Actions → General → Workflow permissions** →
   enable **Read and write permissions**.
2. (Optional) Trigger a first run: **Actions → "Daily NMBS train log" → Run workflow**.

## Local run

```bash
pip install certifi
python fetch_log.py
```
