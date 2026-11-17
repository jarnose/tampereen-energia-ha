# Tampereen Energia to Home Assistant

A small Python service that runs in Docker, fetches your hourly electricity consumption from the Tampereen Energia customer portal (**OmaNRG**, `asiakas.omanrg.fi`) and injects it into Home Assistant's long-term statistics over the WebSocket API. No custom sensors, MQTT or HACS integrations are needed. The data can be used directly in the Energy dashboard.

> **Note (Nov 2026):** Tampereen Energia moved from `app.tampereenenergia.fi` to OmaNRG. The previous Playwright-based scraper no longer works; this version talks to the new service's JSON API directly and needs no browser.

---

## How it works

1. Logs in to `api.asiakas.omanrg.fi` with your portal e-mail and password.
2. Finds your consumption metering point (GSRN) and customer number from your customer data.
3. Exchanges the session for the short-lived tokens used by `mittaus.omanrg.fi` (the "measurement data" page).
4. Fetches the hourly readings (`meter_reading_yh`, returned in Wh and converted to kWh).
5. Reads the last statistic already stored in Home Assistant and imports every **complete local day** after it, continuing the running `sum`.

Because it always continues from what is stored in Home Assistant, a missed run, a restart or a long outage is simply caught up on the next run: Home Assistant is the single source of truth, and no local state is needed.

---

## Features
* **Plain HTTP, no browser:** a tiny image based on `python:3.12-slim`.
* **Automatic catch-up:** gaps (e.g. when the container was offline) are backfilled from the portal.
* **Whole days only:** a day is imported only when all its hours are available (23/24/25 hours on DST days), so half-measured days are never stored. If nothing new is available, it retries every hour.
* **Safe running sum:** if the statistic exists in Home Assistant but has no recent data, the script stops with an error instead of restarting the sum from zero (which would show up as a negative spike in the Energy dashboard).
* **Auto-detected metering point:** `TE_METERINGPOINT` is optional.

---

## Setup

**1. Create the configuration**

```bash
cp .env.example .env
nano .env
```

| Variable | Description | Example |
| :--- | :--- | :--- |
| `TE_USERNAME` | OmaNRG (asiakas.omanrg.fi) login e-mail | `user@example.com` |
| `TE_PASSWORD` | OmaNRG password | `MySecurePassword` |
| `TE_METERINGPOINT` | Optional. 18-digit GSRN of the metering point; auto-detected if empty | `643007XXXXXXXXXXXX` |
| `HA_URL` | Home Assistant WebSocket URL | `ws://192.168.X.X:8123/api/websocket` |
| `HA_TOKEN` | Long-lived access token (HA profile → Security) | `eyJhbGciOiJIUz...` |

Set in `docker-compose.yaml`:

| Variable | Description | Default |
| :--- | :--- | :--- |
| `RUN_TIME` | Daily run time (HH:MM, container time) | `06:15` |
| `TZ` | Container timezone | `Europe/Helsinki` |

`.env` is listed in `.gitignore`, as is the `data/` folder (which holds the log). Never commit either.

**2. Create the data folder and start**

```bash
mkdir -p data && chmod 775 data
docker compose up -d
```

**3. Follow the logs**

```bash
docker logs -f tampere_energy
```

**4. Rebuild after changing the code**

```bash
docker compose up -d --build
```

### Manual runs

```bash
# Show what would be imported, without writing to Home Assistant
docker compose run --rm --no-deps tampere_energy_scraper python main.py --once --dry-run

# Import now and exit
docker compose run --rm --no-deps tampere_energy_scraper python main.py --once
```

### Importing older history

Nothing extra is needed for the history. On the first run, when the statistic does not yet exist, the script imports every complete day the portal returns for the previous and the current calendar year. If the statistic already exists, it continues from its last entry.

---

## Viewing data in Home Assistant

The data is stored under the statistic ID `tampereen_energia:imported_history`.

* **Energy dashboard:** *Settings → Dashboards → Energy → Add consumption* and pick `Tampereen Energia`.
* **Statistics graph card:** add a *Statistics graph* card for the same statistic and set "Days to show" to at least 3.

**Timing notes**
1. The portal publishes readings with about a day's delay, so the newest day available is usually yesterday or the day before. That is why the script only imports complete days.
2. Home Assistant compiles statistics once per hour (about 12 minutes past), so imported data may take up to an hour to show on dashboards.

---

## Files

```text
├── docker-compose.yaml
├── Dockerfile
├── requirements.txt
├── main.py
├── .env.example
└── data/        # created at runtime: scraper.log (git-ignored)
```

---

## Disclaimer
This is an unofficial tool that uses the same endpoints as the OmaNRG web pages. It is not affiliated with Tampereen Energia, and the endpoints may change without notice.
