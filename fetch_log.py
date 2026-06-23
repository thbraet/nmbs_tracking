#!/usr/bin/env python3
"""Fetch today's DIRECT NMBS trains across the monitored station network and log
their per-stop delays.

What "monitored network" means
------------------------------
We watch six stations:

    Diest, Halle, Leuven, Brussels-North, Brussels-Central, Brussels-South

Of these, three are ANCHORS — Diest, Halle, Leuven. A train is only interesting
if its trajectory visits **at least one anchor** *and* **at least two monitored
stations**. That rule deliberately excludes the firehose of trains that merely
clip two Brussels stations on their way between, say, Mons and Antwerp: a pure
Brussels↔Brussels hop has no anchor, so it is dropped. But a Knokke→Liège IC
that passes Brussels-South → Central → North → Leuven *is* kept (Leuven is an
anchor) and we log its Brussels↔Leuven segment.

For every kept train we slice its full stop list to the span between the first
and last monitored stop (inclusive — intermediary non-monitored stops like
Aarschot or Brussels Airport are kept so you can watch delays evolve along the
trajectory) and call that the train's "segment", labelled origin → dest by the
first/last monitored stop.

Outputs (under ./log/, both idempotent per day)
-----------------------------------------------
  - trips_history.csv : one row per train per day — segment endpoints plus the
                        origin departure delay and dest arrival delay. Compact;
                        powers the long-term overview.
  - stops_history.csv : one row per train per stop per day along the segment.
                        Powers the stop-by-stop deep dive and multi-day heatmap.

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
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BXL = ZoneInfo("Europe/Brussels")
CONN_URL = "https://api.irail.be/v1/connections"
VEHICLE_URL = "https://api.irail.be/v1/vehicle/"
LOG_DIR = Path(__file__).resolve().parent / "log"

# The six monitored stations, by their iRail station names. ANCHORS are the
# subset that make a trajectory worth tracking; the Brussels trio are pass-
# through only and count solely when an anchor is also on the route.
MONITORED = {
    "Diest", "Halle", "Leuven",
    "Brussels-North", "Brussels-Central", "Brussels-South/Brussels-Midi",
}
ANCHORS = {"Diest", "Halle", "Leuven"}

# Ordered station pairs we sweep on the connections endpoint to *discover* the
# vehicles that touch the network. We sweep every ordered pair with at least one
# anchor endpoint (i.e. everything except Brussels→Brussels). connections(A,B)
# returns any direct train whose run includes A…B, so a train terminating at a
# Brussels station is still found via its anchor end, and a through train (e.g.
# Knokke→Liège) is found via connections(Brussels-X, Leuven). Deduped by vehicle.
DISCOVERY_PAIRS = [(a, b) for a in MONITORED for b in MONITORED
                   if a != b and (a in ANCHORS or b in ANCHORS)]

# --- iRail API politeness ---------------------------------------------------
# iRail asks for a descriptive User-Agent (ideally with a contact/URL) and rate-
# limits to a few requests per second. We space requests out well under that cap
# and back off on errors. A full run is ~24 connection sweeps to discover the
# day's vehicles plus one /vehicle fetch per unique vehicle (a few hundred all
# told) — sustained ~1.4 req/s stays comfortably under iRail's ~3 req/s ceiling.
USER_AGENT = ("nmbs-network-tracker/2.0 (personal daily delay log; "
              "+https://github.com/thbraet/nmbs_tracking)")
MIN_REQUEST_INTERVAL = 0.7   # seconds between requests (~1.4 req/s)
MAX_RETRIES = 5
_last_request_at = 0.0       # monotonic timestamp of the previous request

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


def _get_json(url: str, label: str) -> dict:
    """GET one iRail endpoint as JSON, throttled and with retry/backoff.

    Retries on rate limiting (HTTP 429, honoring Retry-After), transient 5xx,
    network errors, and malformed JSON, with exponential backoff. `label` only
    makes the give-up error message legible."""
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
                       f"({label}): {last_err}")


def fetch_connections(frm: str, to: str, date_ddmmyy: str, time_hhmm: str) -> dict:
    """Fetch one connections page."""
    params = urllib.parse.urlencode({
        "from": frm, "to": to, "date": date_ddmmyy, "time": time_hhmm,
        "timesel": "departure", "format": "json", "lang": "en",
    })
    return _get_json(f"{CONN_URL}?{params}", f"{frm}->{to} @ {time_hhmm}")


def fetch_vehicle(vehicle_id: str, date_ddmmyy: str) -> dict:
    """Fetch the full stop-by-stop detail for one vehicle run on a given day."""
    params = urllib.parse.urlencode({
        "id": vehicle_id, "date": date_ddmmyy, "format": "json", "lang": "en",
    })
    return _get_json(f"{VEHICLE_URL}?{params}", f"vehicle {vehicle_id}")


# --- Discovery --------------------------------------------------------------

def _vehicle_id(dep: dict) -> tuple[str, str]:
    """Return (normalized id, display shortname) for a connection departure."""
    short = (dep.get("vehicleinfo") or {}).get("shortname") \
        or dep.get("vehicle", "").split(".")[-1]
    return short.replace(" ", ""), short


def sweep_pair(frm: str, to: str, now: datetime) -> dict[str, str]:
    """Paginate one O-D across the whole day; return {vehicle_id: shortname} for
    every DIRECT (0-transfer) trip. Spans 00:00 → end of day regardless of the
    current time so a single late-evening run captures the full day."""
    date_ddmmyy = now.strftime("%d%m%y")
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = (day_start + timedelta(days=1)).timestamp()
    found: dict[str, str] = {}
    cursor = day_start
    last_dep = 0

    for _ in range(40):  # generous page guard
        data = fetch_connections(frm, to, date_ddmmyy, cursor.strftime("%H%M"))
        conns = data.get("connection", []) or []
        if not conns:
            break
        for c in conns:
            if int((c.get("vias") or {}).get("number", "0")) != 0:
                continue  # skip anything requiring a transfer
            if int(c["departure"]["time"]) >= day_end:
                continue  # spilled into tomorrow
            vid, short = _vehicle_id(c["departure"])
            if vid:
                found[vid] = short

        max_dep = max(int(c["departure"]["time"]) for c in conns)
        if max_dep <= last_dep:
            max_dep = last_dep + 3600  # force forward progress
        last_dep = max_dep
        if last_dep >= day_end:
            break
        cursor = datetime.fromtimestamp(last_dep + 60, BXL)

    return found


def discover_vehicles(now: datetime) -> dict[str, str]:
    """Sweep every discovery pair and return the union {vehicle_id: shortname}."""
    vehicles: dict[str, str] = {}
    for frm, to in DISCOVERY_PAIRS:
        vehicles.update(sweep_pair(frm, to, now))
    return vehicles


# --- Segment extraction -----------------------------------------------------

def _hhmm(ts) -> str:
    ts = int(ts or 0)
    return datetime.fromtimestamp(ts, BXL).strftime("%H:%M") if ts > 0 else ""


def _is_set(v) -> bool:
    return str(v) == "1"


def _stop_row(seq: int, s: dict) -> dict:
    """Flatten one iRail vehicle stop into our per-stop schema."""
    station = s.get("station", "")
    arr_sec = int(s.get("arrivalDelay", "0") or 0)
    dep_sec = int(s.get("departureDelay", "0") or 0)
    return {
        "seq": seq,
        "stop": station,
        "monitored": station in MONITORED,
        "sched_arr": _hhmm(s.get("scheduledArrivalTime")),
        "sched_dep": _hhmm(s.get("scheduledDepartureTime")),
        "arr_delay_sec": arr_sec,
        "arr_delay_min": round(arr_sec / 60),
        "dep_delay_sec": dep_sec,
        "dep_delay_min": round(dep_sec / 60),
        "canceled": _is_set(s.get("canceled"))
                    or _is_set(s.get("arrivalCanceled"))
                    or _is_set(s.get("departureCanceled")),
    }


def extract_segment(stops: list[dict]) -> dict | None:
    """Slice a vehicle's full stop list to the monitored segment (first → last
    monitored stop, inclusive). Returns {origin, dest, stops} or None if the run
    doesn't qualify (fewer than two monitored stops, or no anchor among them)."""
    monitored = [(i, s.get("station")) for i, s in enumerate(stops)
                 if s.get("station") in MONITORED]
    if len(monitored) < 2:
        return None
    if not any(name in ANCHORS for _, name in monitored):
        return None
    lo, hi = monitored[0][0], monitored[-1][0]
    seg = [_stop_row(seq, s) for seq, s in enumerate(stops[lo:hi + 1])]
    return {"origin": stops[lo]["station"], "dest": stops[hi]["station"],
            "stops": seg}


def collect_runs(vehicles: dict[str, str], date_ddmmyy: str) -> list[dict]:
    """Fetch each discovered vehicle, keep the ones with a qualifying segment.

    Returns a list of runs: {train, origin, dest, dep_time, stops}. One bad
    vehicle (e.g. JourneyNotFoundException) is skipped, never fatal."""
    runs = []
    for vid, short in sorted(vehicles.items()):
        try:
            data = fetch_vehicle(vid, date_ddmmyy)
        except Exception as e:                     # one bad train shouldn't sink the run
            print(f"  vehicle {vid} failed: {e}", file=sys.stderr)
            continue
        stops = (data.get("stops") or {}).get("stop") or []
        seg = extract_segment(stops)
        if not seg:
            continue
        first = seg["stops"][0]
        runs.append({
            "train": short, "dep_time": first["sched_dep"] or first["sched_arr"],
            **seg,
        })
    runs.sort(key=lambda r: (r["origin"], r["dest"], r["dep_time"]))
    return runs


# --- Outputs ----------------------------------------------------------------

TRIPS_CSV_COLS = ["date", "origin", "dest", "train", "dep_time",
                  "dep_delay_min", "arr_delay_min", "arr_delay_sec", "canceled"]

STOPS_CSV_COLS = ["date", "origin", "dest", "train", "dep_time", "seq", "stop",
                  "monitored", "sched_arr", "sched_dep",
                  "arr_delay_min", "arr_delay_sec",
                  "dep_delay_min", "dep_delay_sec", "canceled"]


def _upsert_csv(path: Path, cols: list[str], date_iso: str, new_rows: list[dict]) -> None:
    """Rewrite `path` keeping every row whose date != date_iso, then append
    new_rows. Idempotent per day: re-running a date never duplicates it."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    kept = []
    if path.exists():
        with path.open(newline="") as f:
            kept = [row for row in csv.DictReader(f) if row.get("date") != date_iso]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in kept:
            w.writerow(row)
        for row in new_rows:
            w.writerow(row)


def write_outputs(date_iso: str, runs: list[dict]) -> None:
    trip_rows, stop_rows = [], []
    for run in runs:
        first, last = run["stops"][0], run["stops"][-1]
        trip_rows.append({
            "date": date_iso, "origin": run["origin"], "dest": run["dest"],
            "train": run["train"], "dep_time": run["dep_time"],
            "dep_delay_min": first["dep_delay_min"],
            "arr_delay_min": last["arr_delay_min"],
            "arr_delay_sec": last["arr_delay_sec"],
            "canceled": int(last["canceled"]),
        })
        for st in run["stops"]:
            stop_rows.append({
                "date": date_iso, "origin": run["origin"], "dest": run["dest"],
                "train": run["train"], "dep_time": run["dep_time"],
                "seq": st["seq"], "stop": st["stop"], "monitored": int(st["monitored"]),
                "sched_arr": st["sched_arr"], "sched_dep": st["sched_dep"],
                "arr_delay_min": st["arr_delay_min"], "arr_delay_sec": st["arr_delay_sec"],
                "dep_delay_min": st["dep_delay_min"], "dep_delay_sec": st["dep_delay_sec"],
                "canceled": int(st["canceled"]),
            })
    _upsert_csv(LOG_DIR / "trips_history.csv", TRIPS_CSV_COLS, date_iso, trip_rows)
    _upsert_csv(LOG_DIR / "stops_history.csv", STOPS_CSV_COLS, date_iso, stop_rows)


def main() -> int:
    now = datetime.now(BXL)
    date_iso = now.strftime("%Y-%m-%d")

    print(f"[{date_iso}] discovering vehicles across {len(DISCOVERY_PAIRS)} "
          f"connection sweeps…")
    vehicles = discover_vehicles(now)
    print(f"[{date_iso}] {len(vehicles)} unique direct vehicles touch the network; "
          f"fetching each /vehicle…")

    runs = collect_runs(vehicles, now.strftime("%d%m%y"))
    write_outputs(date_iso, runs)

    segments = sorted({(r["origin"], r["dest"]) for r in runs})
    n_stops = sum(len(r["stops"]) for r in runs)
    print(f"[{date_iso}] kept {len(runs)} runs across {len(segments)} segments, "
          f"{n_stops} stop rows")
    for origin, dest in segments:
        n = sum(1 for r in runs if r["origin"] == origin and r["dest"] == dest)
        print(f"    {origin} → {dest}: {n} trains")
    return 0


if __name__ == "__main__":
    sys.exit(main())
