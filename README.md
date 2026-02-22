# Tampereen Energia to Home Assistant Scraper

An automated Python scraper that runs in a Docker container to fetch hourly electricity consumption data from the Tampereen Energia web portal. Because the utility does not provide an official API, this script uses Playwright to log in, navigate the Single Page Application (SPA), intercept the internal data requests, and replay them to fetch historical data.

The extracted 24-hour data points are saved to a local JSON backup and injected directly into Home Assistant's Long-Term Statistics database via WebSocket.

---

## 🚀 Features
* **Headless Browser Automation:** Uses Playwright to navigate the OutSystems-based portal.
* **Network Interception:** Captures API headers and replays requests cleanly.
* **Direct HA Integration:** Pushes data into Home Assistant's native statistics engine via WebSocket (no custom sensors or MQTT required).
* **Local Persistence:** Maintains a running sum (`ha_sync.json`) and an offline history backup (`history.json`).
* **Robust Scheduling:** Uses the `schedule` library to run automatically every day at a specified time.
* **History Import:** Separate python-script to import historical daily totals.

---

## 📁 Directory Structure
Ensure your project directory looks like this:

```text
/opt/tampere_energy/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── import_history.py
├── main.py
├── .env.example
├── data/           # Host-mapped volume (contains history.json, ha_sync.json and scraper.log)
```

**Crucial Permission Note:** Docker runs the Python script internally, but it needs permission to write to your host machine's `data` folder. Before running for the first time, ensure the folder exists and is writable:

```bash
mkdir -p data
sudo chmod -R 775 data
```

---

## ⚙️ Configuration

User credentials are handled via environment variables in your `.env` file:

| Variable | Description | Example |
| :--- | :--- | :--- |
| `TE_USERNAME` | Tampereen Energia login email | `user@example.com` |
| `TE_PASSWORD` | Tampereen Energia password | `MySecurePassword` |
| `TE_METERINGPOINT` | Your specific meter ID | `TSV_FI_TKSXX_XXXXXXX` |
| `HA_URL` | Home Assistant WebSocket URL | `ws://192.168.X.X:8123/api/websocket` |
| `HA_TOKEN` | Long-Lived Access Token from HA | `eyJhbGciOiJIUz...` |

Configurations handled in docker-compose.yaml

| Variable | Description | Example |
| :--- | :--- | :--- |
| `RUN_TIME` | Daily execution time (HH:MM) | `08:15` |
| `TZ` | Container timezone | `Europe/Helsinki` |

---

## 🛠️ Installation & Usage

**1. Edit credentials:**
```bash
cp .env.example .env
nano .env
```

Fill your username, password, HA-ipaddress and long lived token

**2. Build and start the container:**

```bash
docker compose up -d
```

**3. View real-time logs:**

```bash
docker logs -f tampere_energy
```

**4. Force a clean rebuild:**
If you ever update `main.py` or change `requirements.txt`, you *must* rebuild the image without using Docker's cache:

```bash
docker compose down
docker compose build --no-cache
docker compose up -d
```

---

## 📊 Viewing Data in Home Assistant

The data is injected natively into Home Assistant's statistics engine under the ID: `tampereen_energia:imported_history`.

**Important Timing Notes:**
1. **2-Day Offset:** The script fetches data from *2 days ago* to ensure the utility company has finalized the smart meter readings.
2. **Retry:** If the data for the fetched day is not full, try again after an hour.
3. **HA Processing Delay:** Home Assistant compiles its statistics database once per hour (usually at 12 minutes past the hour). Data injected at 10:15 will not appear in the dashboard until 11:12.

**How to display it:**
* **Energy Dashboard:** Go to *Settings > Dashboards > Energy*. Add a new grid consumption source and select `Tampereen Energia History`.
* **Standard Dashboard:** Add a `Statistics Graph` card, select `Tampereen Energia History` as the entity, and set "Days to show" to at least 3.

---

## 💾 Local Backups
Even if Home Assistant goes down, your data is safe. The container maintains files in your mapped `./data/` folder:
* `history.json`: A continuous array of all successfully fetched daily/hourly data.
* `ha_sync.json`: Tracks the total running sum required by Home Assistant's energy engine.
* `scraper.log`: A persistent log of the script's execution history.
