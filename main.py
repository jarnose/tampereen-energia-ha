import os
import time
import json
import logging
import schedule
from datetime import datetime, timedelta, timezone
from dateutil import tz
from playwright.sync_api import sync_playwright
from websockets.sync.client import connect

# --- LOGGING SETUP ---
log_dir = "/app/data"
log_file = os.path.join(log_dir, "scraper.log")
if not os.path.exists(log_dir): os.makedirs(log_dir)

log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8', delay=False)
file_handler.setFormatter(log_formatter)
root_logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
root_logger.addHandler(console_handler)

logging.info("=== Scraper Service Initialized (Sync WebSocket Mode) ===")

# --- CONFIGURATION ---
USERNAME = os.getenv("TE_USERNAME")
PASSWORD = os.getenv("TE_PASSWORD")
METERINGPOINT = os.getenv("TE_METERINGPOINT", "")
HA_URL = os.getenv("HA_URL")
HA_TOKEN = os.getenv("HA_TOKEN")

# --- HOME ASSISTANT WEBSOCKET PUSH (SYNCHRONOUS) ---
def send_to_ha(extracted_data):
    if not HA_URL or not HA_TOKEN:
        logging.error("HA_URL or HA_TOKEN not configured. Skipping HA push.")
        return

    current_data_date = extracted_data["date"]
    sum_file = "/app/data/ha_sync.json"

    try:
        with connect(HA_URL) as ws:
            # 1. Authenticate
            ws.recv() # Wait for auth_required
            ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
            auth_res = json.loads(ws.recv())
            
            if auth_res.get("type") != "auth_ok":
                logging.error(f"HA Auth failed: {auth_res}")
                return
            
            # 2. Fetch the last known sum directly from Home Assistant
            # Checking back 14 days just to be safe in case the scraper was down for a week
            start_time = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
            fetch_msg = {
                "id": 101,
                "type": "recorder/statistics_during_period",
                "start_time": start_time,
                "statistic_ids": ["tampereen_energia:imported_history"],
                "period": "hour"
            }
            ws.send(json.dumps(fetch_msg))
            fetch_res = json.loads(ws.recv())
            
            running_sum = 0.0
            last_pushed_date = ""
            
            if fetch_res.get("success"):
                history_data = fetch_res.get("result", {}).get("tampereen_energia:imported_history", [])
                if history_data:
                    # Grab the very last hour of data HA knows about
                    last_stat = history_data[-1]
                    running_sum = last_stat.get("sum", 0.0)
                    
                    # HA returns 'start' as a unix timestamp in milliseconds
                    last_start_ms = last_stat.get("start", 0)
                    if last_start_ms:
                        last_start_dt = datetime.fromtimestamp(last_start_ms / 1000.0, tz=timezone.utc)
                        local_tz = tz.gettz("Europe/Helsinki")
                        last_start_dt = last_start_dt.astimezone(local_tz)
                        last_pushed_date = last_start_dt.strftime("%Y-%m-%d")
                        logging.info(f"HA reports last data was on {last_pushed_date} with a sum of {running_sum} kWh.")
            else:
                logging.warning(f"Failed to fetch history from HA: {fetch_res}")

            # 3. Prevent duplicate pushing based on HA's own database
            if current_data_date == last_pushed_date:
                logging.info(f"Data for {current_data_date} is already in HA. Skipping push to prevent negative spikes.")
                return

            # 4. Build the new statistics array, starting exactly where HA left off
            stats = []
            local_tz = tz.gettz("Europe/Helsinki")
            base_time = datetime.strptime(current_data_date, "%Y-%m-%d").replace(tzinfo=local_tz)

            for i, val in enumerate(extracted_data["hourly_data"]):
                running_sum += float(val)
                hour_time = base_time + timedelta(hours=i)
                stats.append({
                    "start": hour_time.isoformat(),
                    "state": float(val),
                    "sum": round(running_sum, 3)
                })

            # 5. Push the calculated stats to HA
            push_msg = {
                "id": 102,
                "type": "recorder/import_statistics",
                "metadata": {
                    "has_mean": False,
                    "has_sum": True,
                    "name": "Tampereen Energia History",
                    "source": "tampereen_energia",
                    "statistic_id": "tampereen_energia:imported_history",
                    "unit_of_measurement": "kWh"
                },
                "stats": stats
            }
            
            ws.send(json.dumps(push_msg))
            push_res = json.loads(ws.recv())
            
            if push_res.get("success"):
                logging.info(f"Successfully injected {len(stats)} hours into Home Assistant!")
                
                # Keep ha_sync.json purely for debugging visibility
                try:
                    with open(sum_file, "w") as f:
                        json.dump({
                            "running_sum": running_sum,
                            "last_pushed_date": current_data_date,
                            "note": "This file is for debugging. HA is the actual source of truth."
                        }, f, indent=4)
                except Exception as e:
                    logging.warning(f"Could not update debug ha_sync.json: {e}")
            else:
                logging.error(f"HA Import failed: {push_res}")
                
    except Exception as e:
        logging.error(f"WebSocket Error: {e}")

# --- PLAYWRIGHT SCRAPER ---
def fetch_and_publish():
    logging.info("Starting consumption fetch sequence...")
    
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
                        api_info["headers"] = {k: v for k, v in route.request.headers.items() if k.lower() != "content-length"}
                        logging.info("Successfully captured API Data template.")
                except: pass
            route.continue_()

        page.route("**/DataActionGetData*", intercept)

        extracted_data = None

        try:
            logging.info("Logging in to Tampereen Energia...")
            page.goto("https://kirjautuminen.tampereenenergia.fi/login")
            try: page.click("button:has-text('Hyväksy')", timeout=3000)
            except: pass

            page.fill("input[name='username']", USERNAME, force=True)
            page.fill("input[name='password']", PASSWORD, force=True)
            page.click("button[type='submit']")

            try:
                service_button = page.get_by_role("button", name="Siirry palveluun").first
                service_button.wait_for(state="visible", timeout=15000)
                service_button.click()
            except Exception: pass

            page.wait_for_url("**/Home**", timeout=30000)
            page.goto("https://app.tampereenenergia.fi/PowerPlantDistributionPWA/Consumption", wait_until="networkidle")
            
            try: page.wait_for_selector(".ppt_consumption_container, .chart-container", timeout=10000)
            except: pass
            
            page.mouse.wheel(0, 400)
            time.sleep(2)
            page.mouse.wheel(0, -400)

            for i in range(45):
                if api_info["payload"]: break
                time.sleep(1)

            if api_info["payload"]:
                target_date = datetime.now(timezone.utc) - timedelta(days=2)
                start_str = target_date.strftime('%Y-%m-%dT00:00:00.000Z')
                end_str = target_date.strftime('%Y-%m-%dT23:59:59.000Z')

                payload = api_info["payload"]
                vars = payload["screenData"]["variables"]
                vars["FilterParameters"]["StartDate"] = start_str
                vars["FilterParameters"]["EndDate"] = end_str
                vars["FilterParameters"]["PeriodId"] = 9
                vars["IsHistoricaDataFetched"] = True
                
                if METERINGPOINT: vars["FilterParameters"]["MeteringPointId"] = METERINGPOINT

                response = page.request.post(api_info["url"], data=payload, headers=api_info["headers"])
                
                if response.ok:
                    resp_json = response.json()
                    data_list = resp_json.get("data", {}).get("Dataset", {}).get("Data", {}).get("List", [])
                    
                    if data_list:
                        hourly_values = [float(item["Consumption"]) for item in data_list]
                        extracted_data = {
                            "hourly_data": hourly_values,
                            "total_kwh": round(sum(hourly_values), 2),
                            "date": start_str[:10],
                            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        }
                        logging.info(f"Success! Fetched {len(hourly_values)} hourly points for {start_str[:10]}.")
        except Exception as e:
            logging.error(f"Scraper error: {e}")
        
        browser.close()

        # --- PROCESS DATA ---
        if extracted_data:
            # 1. Save to local JSON history
            history_file = "/app/data/history.json"
            all_history = []
            if os.path.exists(history_file):
                try:
                    with open(history_file, 'r') as f: all_history = json.load(f)
                except: pass
            
            # Prevent duplicate entries in local history file
            if not any(entry['date'] == extracted_data['date'] for entry in all_history):
                all_history.append(extracted_data)
                all_history = sorted(all_history, key=lambda x: x['date'])
                with open(history_file, 'w') as f:
                    json.dump(all_history, f, indent=4)
                logging.info(f"Saved {extracted_data['date']} to local history.json")
            
            # 2. Push to HA via Synchronous WebSocket
            send_to_ha(extracted_data)

if __name__ == "__main__":
    fetch_and_publish()
    
    rt = os.getenv("RUN_TIME", "08:15").replace('"', '')
    try:
        h, m = rt.split(":")
        clean_time = f"{int(h):02d}:{int(m):02d}"
        logging.info(f"Background scheduler set for {clean_time}")
        schedule.every().day.at(clean_time).do(fetch_and_publish)
    except Exception as e:
        schedule.every().day.at("08:15").do(fetch_and_publish)

    while True:
        schedule.run_pending()
        time.sleep(60)
