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
                except Exception as e:
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
            wait_until="networkidle" # Let the SPA load completely
        )

        try:
            page.wait_for_selector(
                ".ppt_consumption_container, .chart-container",
                timeout=15000
            )
        except:
            logging.warning("Chart container not detected.")

        # Replaced the manual time.sleep loop with a proper wait for network idle
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except:
            logging.warning("Network did not reach idle state within timeout, proceeding anyway.")

        if not api_info["payload"]:
            browser.close()
            raise RuntimeError("Failed to capture API template. The interceptor didn't catch the request.")

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
            raise RuntimeError(f"Consumption API request failed with status {response.status}")

        resp_json = response.json()
        browser.close()

        dataset = resp_json.get("data", {}).get("Dataset", {})
        chart_data = dataset.get("ChartData", {}).get("List", [])

        if not chart_data:
            raise RuntimeError("No chart data found in the API response.")

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

def wait_for_ws_message(ws, expected_id):
    """Helper to safely ignore background HA state changes and wait for a specific response."""
    while True:
        try:
            message = ws.recv()
            data = json.loads(message)
            if data.get("id") == expected_id:
                return data
        except json.JSONDecodeError:
            continue

def send_to_ha(data):

    if not HA_URL or not HA_TOKEN:
        logging.error("HA credentials missing.")
        return False

    with connect(HA_URL) as ws:
        # 1. Authenticate
        ws.recv() # Read initial auth required message
        ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        
        auth_response = json.loads(ws.recv())
        if auth_response.get("type") != "auth_ok":
            logging.error("HA authentication failed.")
            return False

        # 2. Fetch History Lookback
        # Extended to 60 days to prevent running_sum reset if the scraper was offline for a while
        start_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()

        ws.send(json.dumps({
            "id": 1,
            "type": "recorder/statistics_during_period",
            "start_time": start_time,
            "statistic_ids": [STATISTIC_ID],
            "period": "hour"
        }))

        # Safely wait for ID 1
        history_response = wait_for_ws_message(ws, 1)
        history = history_response.get("result", {}).get(STATISTIC_ID, [])

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

        # 3. Prepare new stats
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

        # 4. Push stats to HA
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

        # Safely wait for ID 2
        import_response = wait_for_ws_message(ws, 2)
        if import_response.get("success"):
            logging.info("Successfully injected into Home Assistant.")
            return True
        else:
            logging.error(f"Failed to inject data into HA: {import_response}")

        return False

# --------------------------------------------------
# JOB WITH CONDITIONAL RETRY
# --------------------------------------------------

def job():
    logging.info("Starting scraper job...")
    try:
        data = fetch_consumption()

        if data:
            success = send_to_ha(data)

            if success:
                logging.info("Import completed successfully. Waiting until next scheduled daily run.")
                # Success! Clear any active hourly retry jobs so it stops retrying.
                schedule.clear('retry')
                return
        else:
            logging.info("Data not fully measured yet.")

    except Exception as e:
        logging.error(f"Job encountered an error: {e}")

    # If we reach this point, the data was None, HA push failed, or an exception occurred.
    # Check if a retry job is already scheduled. If not, create one.
    if not schedule.get_jobs('retry'):
        logging.info("Scheduling a retry to run in 1 hour.")
        schedule.every(1).hours.do(job).tag('retry')

# --------------------------------------------------
# MAIN LOOP
# --------------------------------------------------

if __name__ == "__main__":

    if not USERNAME or not PASSWORD:
        logging.error("Missing Tampere Energia credentials.")
        exit(1)

    # Clean and normalize RUN_TIME
    try:
        raw_time = RUN_TIME.strip().replace('"', '')
        h, m = raw_time.split(":")
        RUN_TIME_CLEAN = f"{int(h):02d}:{int(m):02d}"
    except Exception:
        logging.warning("Invalid RUN_TIME format. Falling back to 06:15")
        RUN_TIME_CLEAN = "06:15"

    # 1. Setup the permanent daily trigger
    schedule.every().day.at(RUN_TIME_CLEAN).do(job).tag('daily')
    logging.info(f"Scheduler started. Daily run time set to {RUN_TIME_CLEAN}.")

    # 2. Run immediately on container start
    job()

    # 3. Keep the script alive and check the schedule
    while True:
        schedule.run_pending()
        time.sleep(60)
