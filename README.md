# NMBS tracking — Diest ↔ Halle

Live dashboard + automatic daily delay log for the **direct** Diest → Halle
train (the hourly IC 17xx service), built on the open [iRail API](https://docs.irail.be/).

**🌐 Live dashboard:** https://thbraet.github.io/nmbs_tracking/

## Contents

| File | What it is |
|------|------------|
| `train-dashboard.html` | Self-contained dashboard. Open it in a browser. Live departure boards for Diest & Halle + a "direct tracker" panel for today's Diest→Halle trips. Refresh button re-pulls live data. |
| `fetch_log.py` | Fetches today's direct Diest→Halle trips and writes the log. Run by the daily workflow. |
| `.github/workflows/daily.yml` | GitHub Actions cron job (21:45 UTC ≈ 23:45 Brussels) that runs the script and commits the log. |
| `log/history.csv` | Long-term log: one row per train per day (the multi-day history). |
| `log/<YYYY-MM-DD>.json` | Full per-day snapshot with summary stats. |

## How the daily log works

iRail only exposes **real-time delays for the current day**, so the log must be
captured before midnight Brussels time. The workflow runs late each evening,
collects every direct trip that departed that day, and commits the results.

`history.csv` is idempotent per day: re-running (manually via the Actions tab)
replaces that day's rows instead of duplicating them.

## One-time setup

1. Create a GitHub repo and push this folder (see chat instructions).
2. In the repo: **Settings → Actions → General → Workflow permissions** →
   enable **Read and write permissions**.
3. (Optional) Trigger a first run: **Actions → "Daily Diest→Halle train log" → Run workflow**.

## Local run

```bash
pip install certifi
python fetch_log.py
```
