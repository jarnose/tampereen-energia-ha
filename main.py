from playwright.sync_api import sync_playwright
from datetime import datetime, timedelta, timezone
import paho.mqtt.client as mqtt
import schedule
import time
import json
import os

# --- LOAD CONFIGURATION FROM DOCKER ENV ---
USERNAME = os.getenv("TE_USERNAME")
PASSWORD = os.getenv("TE_PASSWORD")
METERINGPOINT = os.getenv("TE_METERINGPOINT", "")

MQTT_BROKER = os.getenv("MQTT_BROKER")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASS")

def fetch_and_publish():
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting data fetch...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        extracted_data = {}

        # ---------------------------------------------------------
        # REQUEST INTERCEPTOR
        # ---------------------------------------------------------
        def modify_request(route):
            try:
                payload = route.request.post_data_json
                if payload and "screenData" in payload:
                    variables = payload["screenData"].get("variables", {})
                    if "FilterParameters" in variables:
                        target_date = datetime.now(timezone.utc) - timedelta(days=2)
                        custom_start = target_date.strftime('%Y-%m-%dT00:00:00.000Z')
                        custom_end = target_date.strftime('%Y-%m-%dT23:59:59.000Z')
                        
                        payload["screenData"]["variables"]["FilterParameters"]["StartDate"] = custom_start
                        payload["screenData"]["variables"]["FilterParameters"]["EndDate"] = custom_end
                        payload["screenData"]["variables"]["FilterParameters"]["PeriodId"] = 9
                        
                        if METERINGPOINT:
                            payload["screenData"]["variables"]["FilterParameters"]["MeteringPointId"] = METERINGPOINT
                        
                        route.continue_(post_data=json.dumps(payload))
                        return
                route.continue_()
            except Exception as e:
                print(f"   [Interceptor] Error: {e}")
                route.continue_()

        page.route("**/DataActionGetData*", modify_request)

        # ---------------------------------------------------------
        # RESPONSE SNIFFER
        # ---------------------------------------------------------
        def handle_response(response):
            if "DataActionGetData" in response.url:
                try:
                    resp_json = response.json()
                    dataset = resp_json.get("data", {}).get("Dataset", {})
                    data_list = dataset.get("Data", {}).get("List", [])
                    summary = dataset.get("Summary", {})
                    
                    if data_list and len(data_list) > 0:
                        hourly_consumptions = [float(item["Consumption"]) for item in data_list]
                        total_consumption = float(summary.get("TotalConsumptionSum", sum(hourly_consumptions)))
                        target_date = data_list[0].get("DateFrom", "")[:10]
                        
                        extracted_data["hourly_data"] = hourly_consumptions
                        extracted_data["total_kwh"] = round(total_consumption, 2)
                        extracted_data["date"] = target_date
                        extracted_data["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        
                        print(f"   [SUCCESS] Captured hourly data for {target_date}")
                except Exception:
                    pass

        page.on("response", handle_response)

        # --- NAVIGATION FLOW ---
        try:
            page.goto("https://kirjautuminen.tampereenenergia.fi/login")
            try:
                page.click("button:has-text('Hyväksy')", timeout=2000)
            except:
                pass

            page.fill("input[name='username']", USERNAME, force=True)
            page.fill("input[name='password']", PASSWORD, force=True)
            page.click("button[type='submit']")

            try:
                service_button = page.get_by_role("button", name="Siirry palveluun").first
                service_button.wait_for(state="visible", timeout=10000)
                service_button.click()
            except:
                pass

            page.wait_for_url("**/Home**", timeout=30000)
            page.goto("https://app.tampereenenergia.fi/PowerPlantDistributionPWA/Consumption")
            
            for _ in range(30):
                if extracted_data:
                    break 
                page.wait_for_timeout(1000)

        except Exception as e:
            print(f"   [ERROR] Script failed: {e}")
        
        browser.close()

        # ---------------------------------------------------------
        # MQTT PUBLISH
        # ---------------------------------------------------------
        if extracted_data:
            try:
                client = mqtt.Client()
                client.username_pw_set(MQTT_USER, MQTT_PASS)
                client.connect(MQTT_BROKER, MQTT_PORT, 60)
                client.loop_start()

                device_info = {
                    "identifiers": [f"te_{METERINGPOINT}" if METERINGPOINT else "te_default"],
                    "name": "Tampereen Energia",
                    "manufacturer": "Tampereen Energia",
                    "model": "Web Scraper"
                }

                # Publish Sensor Configs
                total_config = {
                    "name": "Yesterday Total Consumption",
                    "state_topic": "tampereen_energia/state",
                    "value_template": "{{ value_json.total_kwh }}",
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "total",
                    "unique_id": f"te_total_{METERINGPOINT}",
                    "device": device_info,
                    "json_attributes_topic": "tampereen_energia/state"
                }
                client.publish("homeassistant/sensor/tampereen_energia/total/config", json.dumps(total_config), retain=True)

                date_config = {
                    "name": "Consumption Data Date",
                    "state_topic": "tampereen_energia/state",
                    "value_template": "{{ value_json.date }}",
                    "icon": "mdi:calendar",
                    "unique_id": f"te_date_{METERINGPOINT}",
                    "device": device_info
                }
                client.publish("homeassistant/sensor/tampereen_energia/date/config", json.dumps(date_config), retain=True)

                # Publish the actual data
                time.sleep(1)
                client.publish("tampereen_energia/state", json.dumps(extracted_data), retain=True)
                
                client.loop_stop()
                client.disconnect()
                print("   [DONE] Data successfully sent to Home Assistant via MQTT.")
            except Exception as e:
                print(f"   [ERROR] Failed to send MQTT: {e}")
        else:
            print("   [FAILED] No data found.")

if __name__ == "__main__":
    # Run once immediately on startup
    fetch_and_publish()
    
    # Schedule the daily run
    run_time = os.getenv("RUN_TIME", "08:15")
    print(f"\n[INFO] Scheduler started. Next run scheduled for {run_time} every day.")
    
    schedule.every().day.at(run_time).do(fetch_and_publish)
    
    # Keep the container alive and check the schedule
    while True:
        schedule.run_pending()
        time.sleep(60)
