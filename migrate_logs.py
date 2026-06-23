#!/usr/bin/env python3
"""One-shot migration from the old per-route log schema to the new
origin/dest-segment schema.

The old collector tracked six fixed routes and wrote:
  - log/history.csv        : one row per train per day per *route* (redundant —
                             the same physical train appeared under several
                             route slugs measured to different endpoints).
  - log/stops_history.csv  : per-stop rows with a `direction` column
                             (diest-halle | halle-diest).
  - log/<date>.json        : per-route connection snapshot.

The new collector keys everything on the train's monitored *segment*
(origin → dest = first/last monitored stop). The old per-stop data already
carries the full Diest⇄Halle segment, so it maps straight across; the redundant
per-route history.csv rows are dropped. Run once:

    python migrate_logs.py
"""

import csv
import sys
from pathlib import Path

import fetch_log as F

LOG = F.LOG_DIR
DIRECTION_ENDPOINTS = {
    "diest-halle": ("Diest", "Halle"),
    "halle-diest": ("Halle", "Diest"),
}


def main() -> int:
    old_stops = LOG / "stops_history.csv"
    if not old_stops.exists():
        print("No old stops_history.csv found — nothing to migrate.")
        return 0

    with old_stops.open(newline="") as f:
        old_rows = list(csv.DictReader(f))

    # Old rows already in the new schema (have an `origin` column)? Bail out.
    if old_rows and "origin" in old_rows[0]:
        print("stops_history.csv already in the new schema — nothing to do.")
        return 0

    stop_rows, runs = [], {}
    for r in old_rows:
        direction = r.get("direction", "")
        if direction not in DIRECTION_ENDPOINTS:
            continue
        origin, dest = DIRECTION_ENDPOINTS[direction]
        key = (r["date"], r["train"], r["dep_time"])
        seq = int(r["seq"])
        row = {
            "date": r["date"], "origin": origin, "dest": dest,
            "train": r["train"], "dep_time": r["dep_time"],
            "seq": seq, "stop": r["stop"],
            "monitored": int(r["stop"] in F.MONITORED),
            "sched_arr": r.get("sched_arr", ""), "sched_dep": r.get("sched_dep", ""),
            "arr_delay_min": r.get("arr_delay_min", "0"),
            "arr_delay_sec": r.get("arr_delay_sec", "0"),
            "dep_delay_min": r.get("dep_delay_min", "0"),
            "dep_delay_sec": r.get("dep_delay_sec", "0"),
            "canceled": r.get("canceled", "0"),
        }
        stop_rows.append(row)
        runs.setdefault(key, {"origin": origin, "dest": dest, "stops": []})
        runs[key]["stops"].append(row)

    # Derive compact trip rows: origin departure delay + dest arrival delay.
    trip_rows = []
    for (date, train, dep_time), run in runs.items():
        ordered = sorted(run["stops"], key=lambda s: s["seq"])
        first, last = ordered[0], ordered[-1]
        trip_rows.append({
            "date": date, "origin": run["origin"], "dest": run["dest"],
            "train": train, "dep_time": dep_time,
            "dep_delay_min": first["dep_delay_min"],
            "arr_delay_min": last["arr_delay_min"],
            "arr_delay_sec": last["arr_delay_sec"],
            "canceled": last["canceled"],
        })

    # Write new-schema CSVs (full overwrite — this is a clean migration).
    with (LOG / "stops_history.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=F.STOPS_CSV_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(stop_rows, key=lambda r: (r["date"], r["origin"], r["dest"],
                                                      r["dep_time"], r["seq"])))
    with (LOG / "trips_history.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=F.TRIPS_CSV_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(trip_rows, key=lambda r: (r["date"], r["origin"],
                                                     r["dest"], r["dep_time"])))

    # Drop the now-redundant per-route history.csv and old-format per-day JSON.
    removed = []
    legacy = LOG / "history.csv"
    if legacy.exists():
        legacy.unlink()
        removed.append(legacy.name)
    for p in LOG.glob("*.json"):
        p.unlink()
        removed.append(p.name)

    print(f"Migrated {len(stop_rows)} stop rows / {len(trip_rows)} trip rows "
          f"across {len({r['date'] for r in trip_rows})} day(s).")
    if removed:
        print("Removed legacy files: " + ", ".join(sorted(removed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
