# Tampereen Energia - Home Assistant Integration

## NOTE! A lot of Ai has been used to make this!

This project provides a two-part solution for integrating your **Tampereen Energia** electricity consumption data into **Home Assistant**:
1. **History Importer**: A one-time script to backfill years of daily history into the HA Energy Dashboard.
Python-based tool to scrape historical electricity consumption data from the Tampereen Energia customer portal and inject it directly into the Home Assistant long-term statistics database.

Unlike standard sensors, this script uses the Home Assistant WebSocket API to "backfill" data, allowing you to see years of history in your Energy Dashboard instantly.
Features:
- Granular Data: Fetches daily consumption totals for any custom date range.
- Smart Meter Sync: Uses Playwright to navigate the OutSystems-based portal and bypasses anti-forgery tokens (CSRF).
- Direct Injection: Uses recorder/import_statistics via WebSockets to ensure data is permanent and visible in the Energy Dashboard.
- Configurable: Easily adjust start/end dates, metering points, and credentials.

2. **Daily Scraper (Docker)**: A containerized background service that fetches hourly data every morning and sends it to HA via MQTT.

---

## 🛠 Prerequisites

* **Linux/Debian Host:** (Tested on Debian).
* **Python 3.9+** (For the history importer).
* **Docker & Docker Compose** (For the daily scraper).
* **Home Assistant:**
  * [Long-Lived Access Token](https://www.home-assistant.io/docs/authentication/#long-lived-access-token).

---

## 📅 Part 1: History Importer

Use this to populate your Energy Dashboard with data from previous years.

### 1. Setup Environment

```bash
git clone https://github.com/jarnose/tampereen-energia-ha.git
cd tampereen-energia-ha
python3 -m venv venv
source venv/bin/activate
pip install playwright websockets python-dateutil
playwright install chromium
playwright install-deps
```

### 2. Configure & Run

Open import_history_v2.py and set your credentials, HA_TOKEN, and the desired START_DATE / END_DATE.

```bash
python import_history_v2.py
```

### 3. Add to Energy Dashboard

In HA, go to Settings > Dashboards > Energy and add tampereen_energia:imported_history as a Grid Consumption source.

## 🐳 Part 2: Daily Scraper (Docker)

This service runs 24/7 and fetches the previous day's hourly data every morning.
### 1. Configuration

Update the environment section in docker-compose.yml with your:

    - TE_USERNAME / TE_PASSWORD
      Credential for Tampereen Energia website

    - TE_METERINGPOINT
      If you have several metering points, put the name here. Something like TSV_FI_TKS***_*******

    - HA_URL
      URL of your Home Assistant instance

    - HA_TOKEN
      Long lived token for you HA https://www.home-assistant.io/docs/authentication/

### 2. Launch

```bash
docker-compose up -d
```

### 3. Dashboard Visualization

The daily scraper updates the data so you can view it in you Energy-dashboard. Just add the tampereen_energia:imported_history to your energy-dashboard.

## 📦 Dependencies

Ensure your requirements.txt contains:

```Plaintext
playwright
schedule
websockets
python-dateutil
```

## 💾 Local Data Access
The Docker container maintains a local JSON backup on the host machine:
* **Logs**: `./logs/scraper.log`
* **Historical Data**: `./data/history.json`
* **HA daily total**: `./data/ha_sync.json`

This data is persisted even if the container is rebuilt or deleted.

## 📜 License

Personal use only. Not affiliated with Tampereen Energia Oy.
