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

    sum_file = "/app/data/ha_sync.json"
    running_sum = 0.0
    last_pushed_date = ""

    # Load existing sum AND the last pushed date
    if os.path.exists(sum_file):
        try:
            with open(sum_file, "r") as f:
                sync_data = json.load(f)
                running_sum = sync_data.get("running_sum", 0.0)
                last_pushed_date = sync_data.get("last_pushed_date", "")
        except: pass

    # Prevent duplicate pushing on container restart
    current_data_date = extracted_data["date"]
    if current_data_date == last_pushed_date:
        logging.info(f"Data for {current_data_date} already sent to HA. Skipping push to prevent negative spikes.")
        return

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

    message = {
        "id": int(time.time()),
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

    try:
        with connect(HA_URL) as ws:
            ws.recv() # Wait for auth_required
            ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
            auth_res = json.loads(ws.recv())
            
            if auth_res.get("type") != "auth_ok":
                logging.error(f"HA Auth failed: {auth_res}")
                return
            
            ws.send(json.dumps(message))
            res = json.loads(ws.recv())
            
            if res.get("success"):
                logging.info(f"Successfully injected {len(stats)} hours into Home Assistant!")
                # Save both the new sum AND the date we just pushed
                with open(sum_file, "w") as f:
                    json.dump({
                        "running_sum": running_sum,
                        "last_pushed_date": current_data_date
                    }, f)
            else:
                logging.error(f"HA Import failed: {res}")
                
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
            
            # Prevent duplicate entries in local history file too
            if not any(entry['date'] == extracted_data['date'] for entry in all_history):
                all_history.append(extracted_data)
                # Sort by date to keep it clean
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
