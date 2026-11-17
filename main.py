"""Tampereen Energia (omanrg) -> Home Assistant hourly consumption importer.

Tampereen Energia moved to asiakas.omanrg.fi / mittaus.omanrg.fi in Nov 2026.
The new flow needs no browser:

  1. POST api.asiakas.omanrg.fi/api/auth/login/credentials-authentication
  2. POST /api/auth/access/token/<userId>/v2  (sets `applications_token` cookie)
  3. GET  public.mittaus.omanrg.fi/api/gen/customer_data_with_token
         (Bearer applications_token)  -> short lived data token
  4. GET  public.mittaus.omanrg.fi/api/gen/meter_reading_yh
         (Bearer data token) -> hourly values for a whole year, in Wh

Everything newer than the last statistic in Home Assistant is imported, but
only whole local days (so a half-measured day is picked up on a later run).
"""

import os
import sys
import json
import time
import logging
import argparse
import schedule
import requests
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from websockets.sync.client import connect

# --------------------------------------------------
# LOGGING
# --------------------------------------------------

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(DATA_DIR, "scraper.log")),
        logging.StreamHandler(),
    ],
)

# --------------------------------------------------
# ENV
# --------------------------------------------------

USERNAME = os.getenv("TE_USERNAME")
PASSWORD = os.getenv("TE_PASSWORD")
# Optional: GSRN of the metering point (18 digits). Auto-discovered if empty.
GSRN_OVERRIDE = (os.getenv("TE_METERINGPOINT") or "").strip().strip('"')
HA_URL = os.getenv("HA_URL")
HA_TOKEN = os.getenv("HA_TOKEN")
RUN_TIME = os.getenv("RUN_TIME", "06:15").replace('"', "")

STATISTIC_ID = "tampereen_energia:imported_history"
HELSINKI = ZoneInfo("Europe/Helsinki")

API = "https://api.asiakas.omanrg.fi"
MITTAUS = "https://public.mittaus.omanrg.fi/api/gen"
TIMEOUT = 60

# --------------------------------------------------
# OMANRG CLIENT
# --------------------------------------------------


def omanrg_login():
    """Log in and return (session, userId, applications_token)."""
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (tampere_energy importer)"

    r = s.post(
        f"{API}/api/auth/login/credentials-authentication",
        json={"email": USERNAME, "password": PASSWORD},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Login failed with status {r.status_code}")

    csrf = {"X-CSRF-Token": s.cookies.get("csrf_token") or ""}

    r = s.get(f"{API}/api/customerships", timeout=TIMEOUT)
    r.raise_for_status()
    user = r.json()["data"]["user"]
    user_id = user["userId"]

    r = s.post(f"{API}/api/auth/access/token/{user_id}/v2", json={}, headers=csrf, timeout=TIMEOUT)
    r.raise_for_status()
    apps_token = next((c.value for c in s.cookies if c.name == "applications_token"), None)
    if not apps_token:
        raise RuntimeError("applications_token cookie missing after token request")

    return user, apps_token


def pick_metering_point(user):
    """Return (gsrn, customer_number) for the consumption metering point."""
    candidates = []
    for cust in user.get("customerData", []):
        for c in cust.get("meteringPointContracts", []):
            if c.get("meteringPointType") not in (None, "kulutus"):
                continue
            candidates.append(
                (c["gsrn"], c.get("customerNumber") or cust["customerNumber"], c.get("end") is None)
            )
    if GSRN_OVERRIDE:
        candidates = [c for c in candidates if c[0] == GSRN_OVERRIDE] or candidates
    if not candidates:
        raise RuntimeError("No consumption metering points found for this user.")
    # Prefer one with an open (current) contract
    candidates.sort(key=lambda c: not c[2])
    return candidates[0][0], candidates[0][1]


def fetch_hourly(years):
    """Return {utc_datetime: kWh} for the given years, hourly."""
    user, apps_token = omanrg_login()
    gsrn, customer = pick_metering_point(user)
    logging.info(f"Metering point {gsrn}, customer {customer}")

    r = requests.get(
        f"{MITTAUS}/customer_data_with_token",
        headers={"authorization": f"Bearer {apps_token}"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data_token = r.json()["token"]

    values = {}
    for year in years:
        r = requests.get(
            f"{MITTAUS}/meter_reading_yh",
            params={"gsrn": gsrn, "customer_ids": customer, "year": year, "timestep": "PT1H"},
            headers={"authorization": f"Bearer {data_token}"},
            timeout=TIMEOUT,
        )
        if r.status_code == 404:
            continue
        r.raise_for_status()
        for month in r.json().get("months", []):
            for row in month.get("hourly_values", []):
                if row.get("v") is None:
                    continue
                t = datetime.fromisoformat(row["t"].replace("Z", "+00:00")).astimezone(timezone.utc)
                values[t] = round(float(row["v"]) / 1000.0, 3)  # Wh -> kWh
    logging.info(f"Fetched {len(values)} hourly values")
    return values


def complete_days(values):
    """Group hourly values by local day; keep only days with every hour present.

    Returns a sorted list of (utc_start, kWh) for all hours of complete days.
    """
    by_day = defaultdict(dict)
    for t, v in values.items():
        by_day[t.astimezone(HELSINKI).date()][t] = v

    out = []
    for day, hours in sorted(by_day.items()):
        start = datetime(day.year, day.month, day.day, tzinfo=HELSINKI).astimezone(timezone.utc)
        nxt = day + timedelta(days=1)
        end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=HELSINKI).astimezone(timezone.utc)
        expected = int((end - start).total_seconds() // 3600)  # 23/24/25 on DST days
        if len(hours) == expected:
            out.extend(sorted(hours.items()))
        else:
            logging.info(f"{day}: only {len(hours)}/{expected} hours measured so far, skipping")
    return out


# --------------------------------------------------
# HOME ASSISTANT
# --------------------------------------------------


def wait_for_ws_message(ws, expected_id):
    """Ignore background HA messages and wait for a specific response."""
    while True:
        try:
            data = json.loads(ws.recv())
        except json.JSONDecodeError:
            continue
        if data.get("id") == expected_id:
            return data


def stat_start_to_dt(start):
    """HA returns `start` as epoch milliseconds (or ISO string on newer cores)."""
    if isinstance(start, (int, float)):
        return datetime.fromtimestamp(start / 1000, tz=timezone.utc)
    return datetime.fromisoformat(str(start).replace("Z", "+00:00")).astimezone(timezone.utc)


def ha_last_statistic(ws):
    """Return (last_start_utc or None, running_sum)."""
    start_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    ws.send(json.dumps({
        "id": 1,
        "type": "recorder/statistics_during_period",
        "start_time": start_time,
        "statistic_ids": [STATISTIC_ID],
        "period": "hour",
    }))
    history = wait_for_ws_message(ws, 1).get("result", {}).get(STATISTIC_ID, [])

    if history:
        last = history[-1]
        return stat_start_to_dt(last["start"]), float(last.get("sum") or 0)

    # Nothing within 60 days: only start from 0 if the statistic never existed,
    # otherwise we would reset the running sum.
    ws.send(json.dumps({
        "id": 2,
        "type": "recorder/get_statistics_metadata",
        "statistic_ids": [STATISTIC_ID],
    }))
    meta = wait_for_ws_message(ws, 2).get("result") or []
    if meta:
        raise RuntimeError(
            "Statistic exists in HA but has no data in the last 60 days; "
            "refusing to restart the running sum from 0."
        )
    return None, 0.0


def ha_authenticate(ws):
    ws.recv()  # auth_required
    ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
    if json.loads(ws.recv()).get("type") != "auth_ok":
        raise RuntimeError("HA authentication failed.")


def sync(dry_run=False):
    """Fetch new complete days from omanrg and import them into HA.

    Returns the number of hours imported.
    """
    if not HA_URL or not HA_TOKEN:
        raise RuntimeError("HA credentials missing.")

    with connect(HA_URL) as ws:
        ha_authenticate(ws)
        last_start, running_sum = ha_last_statistic(ws)
        logging.info(
            "HA last statistic: "
            + (f"{last_start.astimezone(HELSINKI):%Y-%m-%d %H:%M}, sum={running_sum:.3f}" if last_start else "none")
        )

        first_year = (last_start or datetime.now(timezone.utc) - timedelta(days=365)).astimezone(HELSINKI).year
        years = range(first_year, datetime.now(HELSINKI).year + 1)
        values = fetch_hourly(years)

        new = [(t, v) for t, v in complete_days(values) if last_start is None or t > last_start]
        if not new:
            logging.info("No new complete days to import.")
            return 0

        stats = []
        for t, v in new:
            running_sum += v
            stats.append({"start": t.isoformat(), "state": v, "sum": round(running_sum, 3)})

        first, last = new[0][0].astimezone(HELSINKI), new[-1][0].astimezone(HELSINKI)
        logging.info(
            f"Importing {len(stats)} hours ({first:%Y-%m-%d %H:%M} .. {last:%Y-%m-%d %H:%M}), "
            f"{sum(v for _, v in new):.3f} kWh, new sum {running_sum:.3f}"
        )
        if dry_run:
            logging.info("Dry run: nothing sent to HA.")
            return len(stats)

        ws.send(json.dumps({
            "id": 3,
            "type": "recorder/import_statistics",
            "metadata": {
                "has_mean": False,
                "has_sum": True,
                "name": "Tampereen Energia",
                "source": "tampereen_energia",
                "statistic_id": STATISTIC_ID,
                "unit_of_measurement": "kWh",
            },
            "stats": stats,
        }))
        resp = wait_for_ws_message(ws, 3)
        if not resp.get("success"):
            raise RuntimeError(f"Failed to inject data into HA: {resp}")
        logging.info("Successfully injected into Home Assistant.")
        return len(stats)


# --------------------------------------------------
# JOB WITH CONDITIONAL RETRY
# --------------------------------------------------


def job():
    logging.info("Starting import job...")
    try:
        imported = sync()
        if imported:
            schedule.clear("retry")
            logging.info("Import completed. Waiting until next scheduled daily run.")
            return
        logging.info("Nothing new yet.")
    except Exception as e:
        logging.error(f"Job encountered an error: {e}")

    if not schedule.get_jobs("retry"):
        logging.info("Scheduling a retry to run in 1 hour.")
        schedule.every(1).hours.do(job).tag("retry")


# --------------------------------------------------
# MAIN
# --------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one import and exit")
    parser.add_argument("--dry-run", action="store_true", help="with --once: do not write to HA")
    args = parser.parse_args()

    logging.info("=== Tampere Energia (omanrg) importer started ===")

    if not USERNAME or not PASSWORD:
        logging.error("Missing Tampere Energia credentials.")
        sys.exit(1)

    if args.once:
        try:
            sync(dry_run=args.dry_run)
        except Exception as e:
            logging.error(f"Error: {e}")
            sys.exit(1)
        sys.exit(0)

    try:
        h, m = RUN_TIME.strip().split(":")
        run_time = f"{int(h):02d}:{int(m):02d}"
    except Exception:
        logging.warning("Invalid RUN_TIME format. Falling back to 06:15")
        run_time = "06:15"

    schedule.every().day.at(run_time).do(job).tag("daily")
    logging.info(f"Scheduler started. Daily run time set to {run_time}.")

    job()  # run immediately on container start

    while True:
        schedule.run_pending()
        time.sleep(60)
