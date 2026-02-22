import os
import json
import time
import logging
import schedule
from datetime import datetime, timedelta, timezone
from dateutil import tz

from playwright.sync_api import sync_playwright
from websockets.sync.client import connect

# --------------------------------------------------
# LOGGING
# --------------------------------------------------

DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(DATA_DIR, "scraper.log")),
        logging.StreamHandler()
    ],
)

logging.info("=== Tampere Energia Scraper Started ===")

# --------------------------------------------------
# ENV
# --------------------------------------------------

USERNAME = os.getenv("TE_USERNAME")
PASSWORD = os.getenv("TE_PASSWORD")
METERINGPOINT = os.getenv("TE_METERINGPOINT", "")
HA_URL = os.getenv("HA_URL")
HA_TOKEN = os.getenv("HA_TOKEN")
RUN_TIME = os.getenv("RUN_TIME", "06:15").replace('"', "")

STATISTIC_ID = "tampereen_energia:imported_history"

# --------------------------------------------------
# FETCH CONSUMPTION (MEASURED ONLY)
# --------------------------------------------------

def fetch_consumption():

    target_date = datetime.now(timezone.utc) - timedelta(days=2)
    date_str = target_date.strftime("%Y-%m-%d")

    logging.info(f"Fetching data for {date_str}")

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        api_info = {"payload": None, "url": "", "headers": {}}

        def intercept(route):
            if "DataActionGetData" in route.request.url and not api_info["payload"]:
                try:
                    payload = route.request.post_data_json
                    variables = payload.get("screenData", {}).get("variables", {})
                    if "FilterParameters" in variables:
                        api_info["payload"] = payload
                        api_info["url"] = route.request.url
                        api_info["headers"] = {
                            k: v for k, v in route.request.headers.items()
                            if k.lower() != "content-length"
                        }
                        logging.info("Captured API template.")
                except:
                    pass
            route.continue_()

        page.route("**/DataActionGetData*", intercept)

        # LOGIN
        page.goto("https://kirjautuminen.tampereenenergia.fi/login")

        try:
            page.click("button:has-text('Hyväksy')", timeout=3000)
        except:
            pass

        page.fill("input[name='username']", USERNAME, force=True)
        page.fill("input[name='password']", PASSWORD, force=True)
        page.click("button[type='submit']")

        try:
            service_button = page.get_by_role("button", name="Siirry palveluun").first
            service_button.wait_for(state="visible", timeout=15000)
            service_button.click()
        except:
            pass

        page.wait_for_url("**/Home**", timeout=30000)

        page.goto(
            "https://app.tampereenenergia.fi/PowerPlantDistributionPWA/Consumption",
            wait_until="networkidle"
        )

        try:
            page.wait_for_selector(
                ".ppt_consumption_container, .chart-container",
                timeout=15000
            )
        except:
            logging.warning("Chart container not detected.")

        page.mouse.wheel(0, 400)
        time.sleep(2)
        page.mouse.wheel(0, -400)

        for _ in range(45):
            if api_info["payload"]:
                break
            time.sleep(1)

        if not api_info["payload"]:
            browser.close()
            raise RuntimeError("Failed to capture API template")

        start_str = target_date.strftime('%Y-%m-%dT00:00:00.000Z')
        end_str = target_date.strftime('%Y-%m-%dT23:59:59.000Z')

        payload = api_info["payload"]
        variables = payload["screenData"]["variables"]
        filter_params = variables["FilterParameters"]

        filter_params["StartDate"] = start_str
        filter_params["EndDate"] = end_str
        filter_params["PeriodId"] = 9
        variables["IsHistoricaDataFetched"] = True

        if METERINGPOINT:
            filter_params["MeteringPointId"] = METERINGPOINT

        response = page.request.post(
            api_info["url"],
            data=payload,
            headers=api_info["headers"]
        )

        if not response.ok:
            browser.close()
            raise RuntimeError("Consumption API request failed")

        resp_json = response.json()
        browser.close()

        dataset = resp_json.get("data", {}).get("Dataset", {})
        chart_data = dataset.get("ChartData", {}).get("List", [])

        if not chart_data:
            raise RuntimeError("No chart data found")

        measured_rows = [
            row for row in chart_data
            if row.get("StatusDescriptionName") == "Mitattu"
        ]

        if len(measured_rows) != 24:
            logging.info(
                f"Not fully measured yet ({len(measured_rows)}/24 hours)."
            )
            return None

        hourly_values = [float(row["Consumption"]) for row in measured_rows]

        logging.info("Day fully measured ✔")

        return {
            "hourly_data": hourly_values,
            "date": date_str
        }

# --------------------------------------------------
# HOME ASSISTANT PUSH
# --------------------------------------------------

def send_to_ha(data):

    if not HA_URL or not HA_TOKEN:
        logging.error("HA credentials missing.")
        return

    with connect(HA_URL) as ws:

        ws.recv()
        ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        if json.loads(ws.recv()).get("type") != "auth_ok":
            logging.error("HA authentication failed.")
            return

        start_time = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()

        ws.send(json.dumps({
            "id": 1,
            "type": "recorder/statistics_during_period",
            "start_time": start_time,
            "statistic_ids": [STATISTIC_ID],
            "period": "hour"
        }))

        history = json.loads(ws.recv()).get("result", {}).get(STATISTIC_ID, [])

        running_sum = 0
        last_date = ""

        if history:
            last_stat = history[-1]
            running_sum = last_stat.get("sum", 0)

            last_start = last_stat.get("start", 0)
            if last_start:
                dt = datetime.fromtimestamp(last_start / 1000, tz=timezone.utc)
                dt = dt.astimezone(tz.gettz("Europe/Helsinki"))
                last_date = dt.strftime("%Y-%m-%d")

        if data["date"] == last_date:
            logging.info(f"{data['date']} already imported — skipping.")
            return True

        stats = []
        helsinki = tz.gettz("Europe/Helsinki")
        base_time = datetime.strptime(data["date"], "%Y-%m-%d").replace(tzinfo=helsinki)

        for i, value in enumerate(data["hourly_data"]):
            running_sum += float(value)
            stats.append({
                "start": (base_time + timedelta(hours=i)).isoformat(),
                "state": float(value),
                "sum": round(running_sum, 3)
            })

        ws.send(json.dumps({
            "id": 2,
            "type": "recorder/import_statistics",
            "metadata": {
                "has_mean": False,
                "has_sum": True,
                "name": "Tampereen Energia",
                "source": "tampereen_energia",
                "statistic_id": STATISTIC_ID,
                "unit_of_measurement": "kWh"
            },
            "stats": stats
        }))

        result = json.loads(ws.recv())
        if result.get("success"):
            logging.info("Successfully injected into Home Assistant.")
            return True

        return False

# --------------------------------------------------
# JOB WITH HOURLY RETRY
# --------------------------------------------------

def job():
    try:
        data = fetch_consumption()

        if not data:
            return

        success = send_to_ha(data)

        if success:
            logging.info("Import completed. Waiting until next day.")

    except Exception as e:
        logging.error(f"Job failed: {e}")

# --------------------------------------------------
# MAIN LOOP
# --------------------------------------------------

if __name__ == "__main__":

    if not USERNAME or not PASSWORD:
        logging.error("Missing Tampere Energia credentials.")
        exit(1)

    # Run immediately on start
    job()

    # Clean and normalize RUN_TIME
    try:
        raw_time = RUN_TIME.strip().replace('"', '')
        h, m = raw_time.split(":")
        RUN_TIME_CLEAN = f"{int(h):02d}:{int(m):02d}"
    except Exception:
        logging.warning("Invalid RUN_TIME format. Falling back to 06:15")
        RUN_TIME_CLEAN = "06:15"

    # Daily safety trigger
    schedule.every().day.at(RUN_TIME_CLEAN).do(job)

    # Hourly retry until measured
    schedule.every().hour.do(job)

    logging.info("Scheduler started (hourly retry + daily trigger).")

    while True:
        schedule.run_pending()
        time.sleep(60)
