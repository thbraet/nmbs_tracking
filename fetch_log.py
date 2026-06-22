#!/usr/bin/env python3
"""Fetch today's DIRECT Diest -> Halle train departures from the iRail API and
append them to a daily log.

Writes two things under ./log/:
  - <YYYY-MM-DD>.json : full detail + summary for that day
  - history.csv       : one appended row per train per day (the long-term log)

iRail only keeps real-time delay values for the *current* day, so this script
must run late in the evening (Brussels time) to capture a full day of trips.
"""

import csv
import json
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BXL = ZoneInfo("Europe/Brussels")
CONN_URL = "https://api.irail.be/v1/connections"
FROM, TO = "Diest", "Halle"
LOG_DIR = Path(__file__).resolve().parent / "log"

# Verified TLS context (use certifi if available; falls back to system store).
try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()


def fetch(date_ddmmyy: str, time_hhmm: str) -> dict:
    params = urllib.parse.urlencode({
        "from": FROM, "to": TO, "date": date_ddmmyy, "time": time_hhmm,
        "timesel": "departure", "format": "json", "lang": "en",
    })
    req = urllib.request.Request(
        f"{CONN_URL}?{params}",
        headers={"User-Agent": "diest-halle-tracker/1.0 (personal daily log)"},
    )
    with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
        return json.load(r)


def collect_direct_trips(now: datetime) -> list[dict]:
    """Paginate the connections endpoint across the whole day and return all
    DIRECT (0-transfer) trips that have already departed by `now`."""
    date_ddmmyy = now.strftime("%d%m%y")
    now_sec = int(now.timestamp())
    trips: dict[str, dict] = {}
    cursor = now.replace(hour=0, minute=0, second=0, microsecond=0)  # start of today
    last_dep = 0

    for _ in range(40):  # generous page guard
        data = fetch(date_ddmmyy, cursor.strftime("%H%M"))
        conns = data.get("connection", []) or []
        if not conns:
            break

        for c in conns:
            if int((c.get("vias") or {}).get("number", "0")) != 0:
                continue  # skip anything requiring a transfer
            dep, arr = c["departure"], c["arrival"]
            dep_t = int(dep["time"])
            if dep_t > now_sec:
                continue  # not departed yet
            veh = (dep.get("vehicleinfo") or {}).get("shortname") \
                or dep.get("vehicle", "").split(".")[-1]
            trips[f"{dep_t}|{veh}"] = {
                "dep_unix": dep_t,
                "dep_time": datetime.fromtimestamp(dep_t, BXL).strftime("%H:%M"),
                "train": veh,
                "platform": (dep.get("platforminfo") or {}).get("name") or dep.get("platform") or "",
                "dep_delay_min": round(int(dep.get("delay", "0")) / 60),
                "arr_delay_sec": int(arr.get("delay", "0")),
                "arr_delay_min": round(int(arr.get("delay", "0")) / 60),
                "canceled": dep.get("canceled") == "1" or arr.get("canceled") == "1",
            }

        max_dep = max(int(c["departure"]["time"]) for c in conns)
        if max_dep <= last_dep:
            max_dep = last_dep + 3600  # force forward progress
        last_dep = max_dep
        if last_dep > now_sec + 1800:
            break
        cursor = datetime.fromtimestamp(last_dep + 60, BXL)

    return sorted(trips.values(), key=lambda t: t["dep_unix"])


def summarize(trips: list[dict]) -> dict:
    ran = [t for t in trips if not t["canceled"]]
    arr = [t["arr_delay_min"] for t in ran]
    on_time = sum(1 for m in arr if m <= 0)
    return {
        "total_trips": len(trips),
        "cancelled": len(trips) - len(ran),
        "on_time": on_time,
        "late": sum(1 for m in arr if m > 0),
        "on_time_pct": round(100 * on_time / len(arr)) if arr else None,
        "avg_arr_delay_min": round(sum(arr) / len(arr), 2) if arr else None,
        "max_arr_delay_min": max(arr) if arr else None,
    }


def write_outputs(date_iso: str, trips: list[dict], summary: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Per-day JSON (overwrite — represents the final snapshot for that day).
    day_file = LOG_DIR / f"{date_iso}.json"
    day_file.write_text(json.dumps(
        {"date": date_iso, "route": f"{FROM} -> {TO} (direct)",
         "summary": summary, "trips": trips},
        indent=2, ensure_ascii=False))

    # Upsert per-trip rows into the long-term history CSV (idempotent per day:
    # any existing rows for `date_iso` are replaced, so re-runs never duplicate).
    hist = LOG_DIR / "history.csv"
    cols = ["date", "dep_time", "train", "platform",
            "dep_delay_min", "arr_delay_min", "arr_delay_sec", "canceled"]
    kept = []
    if hist.exists():
        with hist.open(newline="") as f:
            kept = [row for row in csv.DictReader(f) if row.get("date") != date_iso]
    with hist.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in kept:
            w.writerow(row)
        for t in trips:
            w.writerow({
                "date": date_iso, "dep_time": t["dep_time"], "train": t["train"],
                "platform": t["platform"], "dep_delay_min": t["dep_delay_min"],
                "arr_delay_min": t["arr_delay_min"], "arr_delay_sec": t["arr_delay_sec"],
                "canceled": int(t["canceled"]),
            })


def main() -> int:
    now = datetime.now(BXL)
    date_iso = now.strftime("%Y-%m-%d")
    trips = collect_direct_trips(now)
    summary = summarize(trips)
    write_outputs(date_iso, trips, summary)

    print(f"[{date_iso}] {FROM} -> {TO} direct: {summary['total_trips']} trips, "
          f"{summary['on_time_pct']}% on time, "
          f"avg arr delay {summary['avg_arr_delay_min']} min, "
          f"max {summary['max_arr_delay_min']} min "
          f"(cancelled: {summary['cancelled']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
