from playwright.sync_api import sync_playwright
from datetime import datetime, timedelta, timezone
import dateutil.relativedelta
import websockets
import asyncio
import time
import json

# --- CONFIGURATION ---
USERNAME = "your username here"
PASSWORD = "your password here"
METERINGPOINT = "your metering point here" #You can leave this empty if you have only one metering point

# Define the historical date range to fetch (Format: YYYY-MM-DD)
START_DATE = "YYYY-MM-DD"
END_DATE = "YYYY-MM-DD"

# Home Assistant WebSocket Details
HA_URL = "ws://192.168.X.X:8123/api/websocket" # Change to your HA IP
HA_TOKEN = "YOUR_LONG_LIVED_ACCESS_TOKEN" # Create a long lived access token in HA in your profile-page security tab and paste the token here

# Name of the sensor that will appear in the Energy Dashboard
STATISTIC_ID = "tampereen_energia:imported_history"


def fetch_historical_data():
    historical_data = []
    
    with sync_playwright() as p:
        print("Launching browser...")
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        payload_template = {}
        api_url = ""
        api_headers = {}

        # 1. Steal the API Template AND Security Headers
        def intercept(route):
            nonlocal payload_template, api_url, api_headers
            if "DataActionGetData" in route.request.url and not payload_template:
                try:
                    data = route.request.post_data_json
                    if data and "screenData" in data and "FilterParameters" in data["screenData"].get("variables", {}):
                        payload_template = data
                        api_url = route.request.url
                        
                        # Steal the headers (CSRF tokens, etc.)
                        api_headers = {k: v for k, v in route.request.headers.items() if k.lower() != "content-length"}
                        
                        print("   [INFO] Successfully intercepted API payload and security headers.")
                except:
                    pass
            route.continue_()

        page.route("**/DataActionGetData*", intercept)

        print("Logging in to Tampereen Energia...")
        page.goto("https://kirjautuminen.tampereenenergia.fi/login")
        try: page.click("button:has-text('Hyväksy')", timeout=2000)
        except: pass

        page.fill("input[name='username']", USERNAME, force=True)
        page.fill("input[name='password']", PASSWORD, force=True)
        page.click("button[type='submit']")

        try:
            service_button = page.get_by_role("button", name="Siirry palveluun").first
            service_button.wait_for(state="visible", timeout=10000)
            service_button.click()
        except: pass

        page.wait_for_url("**/Home**", timeout=30000)
        
        print("Triggering consumption graph to steal tokens...")
        page.goto("https://app.tampereenenergia.fi/PowerPlantDistributionPWA/Consumption")
        
        for _ in range(15):
            if payload_template:
                break
            page.wait_for_timeout(1000)

        if not payload_template:
            print("Failed to capture API template. Exiting.")
            browser.close()
            return []

        # 2. Fetch Data MONTH-BY-MONTH to force Daily resolution
        print(f"\nStarting Month-by-Month bulk fetch ({START_DATE} to {END_DATE})...")
        
        # Parse the start and end dates from the configuration
        current_date = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        final_end_date = datetime.strptime(END_DATE, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)

        while current_date <= final_end_date:
            month_start = current_date
            next_month = month_start + dateutil.relativedelta.relativedelta(months=1)
            month_end = next_month - timedelta(seconds=1)
            
            # Cap the final month exactly at the target end date
            if month_end > final_end_date:
                month_end = final_end_date

            start_str = month_start.strftime('%Y-%m-%dT00:00:00.000Z')
            end_str = month_end.strftime('%Y-%m-%dT23:59:59.000Z')
            
            print(f" -> Fetching Daily Data for {start_str[:7]}...")

            payload_template["screenData"]["variables"]["FilterParameters"]["StartDate"] = start_str
            payload_template["screenData"]["variables"]["FilterParameters"]["EndDate"] = end_str
            payload_template["screenData"]["variables"]["FilterParameters"]["PeriodId"] = 9 
            
            # Ensure the server unlocks old archived data
            payload_template["screenData"]["variables"]["IsHistoricaDataFetched"] = True
            
            if METERINGPOINT:
                payload_template["screenData"]["variables"]["FilterParameters"]["MeteringPointId"] = METERINGPOINT

            response = page.request.post(api_url, data=payload_template, headers=api_headers)
            
            if response.ok:
                resp_json = response.json()
                data_list = resp_json.get("data", {}).get("Dataset", {}).get("Data", {}).get("List", [])
                
                print(f"    Got {len(data_list)} days of data.")
                for item in data_list:
                    historical_data.append({
                        "start": item["DateFrom"],
                        "state": float(item["Consumption"])
                    })
            else:
                print(f"    HTTP Error {response.status}: {response.status_text}")

            current_date = next_month
            time.sleep(1) # Politeness delay

        browser.close()
        return historical_data


async def inject_to_home_assistant(raw_data):
    if not raw_data:
        print("No data to inject.")
        return

    print(f"\nProcessing {len(raw_data)} days of data for Home Assistant...")
    
    # Sort data chronologically to ensure the sum increases properly
    raw_data.sort(key=lambda x: x["start"])

    stats = []
    running_sum = 0.0

    for item in raw_data:
        running_sum += item["state"]
        stats.append({
            "start": item["start"],
            "state": item["state"],
            "sum": running_sum
        })

    ws_payload = {
        "id": 1, 
        "type": "recorder/import_statistics",
        "metadata": {
            "has_mean": False,
            "has_sum": True,
            "name": "Tampereen Energia History",
            "source": "tampereen_energia", 
            "statistic_id": STATISTIC_ID,
            "unit_of_measurement": "kWh"
        },
        "stats": stats
    }

    print(f"Connecting to Home Assistant at {HA_URL}...")
    try:
        async with websockets.connect(HA_URL) as websocket:
            auth_req = await websocket.recv()
            await websocket.send(json.dumps({
                "type": "auth",
                "access_token": HA_TOKEN
            }))
            auth_res = await websocket.recv()
            
            if json.loads(auth_res).get("type") != "auth_ok":
                print("HA Authentication failed. Check your Long-Lived Access Token.")
                return

            print("Authentication: auth_ok")
            print("Injecting statistics...")
            
            await websocket.send(json.dumps(ws_payload))
            
            result_str = await websocket.recv()
            result = json.loads(result_str)
            print("Result:", result)
            
            if result.get("success"):
                print("\n[SUCCESS] Historical daily data imported into Home Assistant!")
            else:
                print(f"\n[FAILED] Home Assistant rejected the data: {result.get('error')}")
            
    except Exception as e:
        print(f"Failed to connect or send data to HA: {e}")

if __name__ == "__main__":
    # Note: Requires 'dateutil'. If not installed: pip install python-dateutil
    data = fetch_historical_data()
    if data:
        asyncio.run(inject_to_home_assistant(data))
