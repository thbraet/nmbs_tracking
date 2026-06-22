#!/usr/bin/env python3
"""Fetch today's DIRECT NMBS train departures for every tracked route and
append them to a daily log.

Tracked routes (both directions of each pair):
  Diest <-> Halle
  Diest <-> Brussels-North  (Brussel-Noord)
  Diest <-> Brussels-Central (Brussel-Centraal)

Writes two things under ./log/:
  - <YYYY-MM-DD>.json : full detail + summary for that day, keyed per route
  - history.csv       : one appended row per train per day per route (the
                        long-term log; carries a `route` column)

iRail only keeps real-time delay values for the *current* day, so this script
must run late in the evening (Brussels time) to capture a full day of trips.
"""

import csv
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BXL = ZoneInfo("Europe/Brussels")
CONN_URL = "https://api.irail.be/v1/connections"
LOG_DIR = Path(__file__).resolve().parent / "log"

# --- iRail API politeness ---------------------------------------------------
# iRail asks for a descriptive User-Agent (ideally with a contact/URL) and rate-
# limits to a few requests per second. We space requests out well under that cap
# and back off on errors so a full multi-route run never trips the limiter.
USER_AGENT = ("nmbs-diest-tracker/1.0 (personal daily delay log; "
              "+https://github.com/thbraet/nmbs_tracking)")
MIN_REQUEST_INTERVAL = 0.7   # seconds between requests (~1.4 req/s, under iRail's ~3/s)
MAX_RETRIES = 5
_last_request_at = 0.0       # monotonic timestamp of the previous request

# Each route: stable slug, the API station names, and a friendly label.
# Slugs are also used by the dashboard to filter history.csv, so keep them stable.
ROUTES = [
    {"slug": "diest-halle",            "from": "Diest",            "to": "Halle"},
    {"slug": "halle-diest",            "from": "Halle",            "to": "Diest"},
    {"slug": "diest-brussels-north",   "from": "Diest",            "to": "Brussels-North"},
    {"slug": "brussels-north-diest",   "from": "Brussels-North",   "to": "Diest"},
    {"slug": "diest-brussels-central", "from": "Diest",            "to": "Brussels-Central"},
    {"slug": "brussels-central-diest", "from": "Brussels-Central", "to": "Diest"},
]

# Verified TLS context (use certifi if available; falls back to system store).
try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()


def _throttle() -> None:
    """Block until at least MIN_REQUEST_INTERVAL has passed since the last call."""
    global _last_request_at
    wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def fetch(frm: str, to: str, date_ddmmyy: str, time_hhmm: str) -> dict:
    """Fetch one connections page, throttled and with retry/backoff.

    Retries on rate limiting (HTTP 429, honoring Retry-After), transient 5xx,
    network errors, and malformed JSON, with exponential backoff."""
    params = urllib.parse.urlencode({
        "from": frm, "to": to, "date": date_ddmmyy, "time": time_hhmm,
        "timesel": "departure", "format": "json", "lang": "en",
    })
    url = f"{CONN_URL}?{params}"

    last_err = None
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:                      # rate limited
                retry_after = (e.headers.get("Retry-After") or "").strip()
                wait = float(retry_after) if retry_after.isdigit() else 2 ** (attempt + 1)
                print(f"  rate limited (429), waiting {wait:g}s…", file=sys.stderr)
                time.sleep(wait)
                continue
            if 500 <= e.code < 600:                # transient server error
                time.sleep(2 ** attempt)
                continue
            raise                                  # 4xx other than 429 → real error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(2 ** attempt)
            continue

    raise RuntimeError(f"iRail request failed after {MAX_RETRIES} attempts "
                       f"({frm}->{to} @ {time_hhmm}): {last_err}")


def collect_direct_trips(frm: str, to: str, now: datetime) -> list[dict]:
    """Paginate the connections endpoint across the whole day and return all
    DIRECT (0-transfer) trips that have already departed by `now`."""
    date_ddmmyy = now.strftime("%d%m%y")
    now_sec = int(now.timestamp())
    trips: dict[str, dict] = {}
    cursor = now.replace(hour=0, minute=0, second=0, microsecond=0)  # start of today
    last_dep = 0

    for _ in range(40):  # generous page guard
        data = fetch(frm, to, date_ddmmyy, cursor.strftime("%H%M"))
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


CSV_COLS = ["date", "route", "from", "to", "dep_time", "train", "platform",
            "dep_delay_min", "arr_delay_min", "arr_delay_sec", "canceled"]


def write_outputs(date_iso: str, per_route: list[dict]) -> None:
    """per_route: list of {route, from, to, trips, summary} for the day."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Per-day JSON (overwrite — the final snapshot for that day, keyed per route).
    day_file = LOG_DIR / f"{date_iso}.json"
    day_file.write_text(json.dumps(
        {"date": date_iso,
         "routes": {r["route"]: {"from": r["from"], "to": r["to"],
                                 "summary": r["summary"], "trips": r["trips"]}
                    for r in per_route}},
        indent=2, ensure_ascii=False))

    # Upsert per-trip rows into the long-term history CSV. One run regenerates the
    # whole day across all routes, so we drop every existing row for `date_iso`
    # and re-add the fresh ones (idempotent — re-runs never duplicate).
    hist = LOG_DIR / "history.csv"
    kept = []
    if hist.exists():
        with hist.open(newline="") as f:
            for row in csv.DictReader(f):
                if row.get("date") == date_iso:
                    continue
                # Migrate legacy rows (no `route` column) → the original route.
                row.setdefault("route", "diest-halle")
                row.setdefault("from", "Diest")
                row.setdefault("to", "Halle")
                kept.append(row)

    with hist.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for row in kept:
            w.writerow(row)
        for r in per_route:
            for t in r["trips"]:
                w.writerow({
                    "date": date_iso, "route": r["route"],
                    "from": r["from"], "to": r["to"],
                    "dep_time": t["dep_time"], "train": t["train"],
                    "platform": t["platform"], "dep_delay_min": t["dep_delay_min"],
                    "arr_delay_min": t["arr_delay_min"], "arr_delay_sec": t["arr_delay_sec"],
                    "canceled": int(t["canceled"]),
                })


def main() -> int:
    now = datetime.now(BXL)
    date_iso = now.strftime("%Y-%m-%d")

    per_route = []
    for r in ROUTES:
        trips = collect_direct_trips(r["from"], r["to"], now)
        summary = summarize(trips)
        per_route.append({"route": r["slug"], "from": r["from"], "to": r["to"],
                          "trips": trips, "summary": summary})
        print(f"[{date_iso}] {r['from']} -> {r['to']} direct: "
              f"{summary['total_trips']} trips, "
              f"{summary['on_time_pct']}% on time, "
              f"avg arr delay {summary['avg_arr_delay_min']} min, "
              f"max {summary['max_arr_delay_min']} min "
              f"(cancelled: {summary['cancelled']})")

    write_outputs(date_iso, per_route)
    return 0


if __name__ == "__main__":
    sys.exit(main())
