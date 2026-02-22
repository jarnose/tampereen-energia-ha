import os
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Any

import requests

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

TE_API_URL = os.getenv("TE_API_URL")
HA_URL = os.getenv("HA_URL")
HA_TOKEN = os.getenv("HA_TOKEN")

STATE_FILE = Path("import_state.json")
VALID_STATUSES = {"Mitattu"}  # Add "Korjattu" here if needed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# -----------------------------------------------------------------------------
# State handling
# -----------------------------------------------------------------------------

def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# -----------------------------------------------------------------------------
# Tampere Energia data handling
# -----------------------------------------------------------------------------

def fetch_data() -> Dict[str, Any]:
    logging.info("Fetching data from Tampere Energia API")
    response = requests.get(TE_API_URL, timeout=30)
    response.raise_for_status()
    return response.json()


def extract_measured_entries(chart_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extract only rows marked as 'Mitattu' and convert to structured entries.
    """
    entries = []

    for item in chart_data.get("List", []):
        status = item.get("StatusDescriptionName")

        if status not in VALID_STATUSES:
            continue

        try:
            start = datetime.fromisoformat(item["DateFrom"].replace("Z", "+00:00"))
            value = float(item["Consumption"])
        except (KeyError, ValueError):
            continue

        entries.append({
            "timestamp": start,
            "value": value,
        })

    return entries


# -----------------------------------------------------------------------------
# Filtering logic
# -----------------------------------------------------------------------------

def filter_completed_days(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Only keep data up to day before yesterday (local time).
    """
    today = datetime.now(timezone.utc).date()
    cutoff_date = today - timedelta(days=2)

    return [
        e for e in entries
        if e["timestamp"].date() <= cutoff_date
    ]


def filter_new_entries(entries: List[Dict[str, Any]], state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Deduplicate based on last imported timestamp.
    """
    last_imported_str = state.get("last_imported")

    if not last_imported_str:
        return entries

    last_imported = datetime.fromisoformat(last_imported_str)

    return [
        e for e in entries
        if e["timestamp"] > last_imported
    ]


# -----------------------------------------------------------------------------
# Home Assistant integration
# -----------------------------------------------------------------------------

def send_to_home_assistant(entries: List[Dict[str, Any]]) -> None:
    if not entries:
        logging.info("No new entries to send.")
        return

    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

    for entry in entries:
        payload = {
            "state": entry["value"],
            "attributes": {
                "device_class": "energy",
                "unit_of_measurement": "kWh",
            }
        }

        url = f"{HA_URL}/api/states/sensor.tampereen_energia_hourly"

        response = requests.post(url, headers=headers, json=payload, timeout=30)

        if response.status_code not in (200, 201):
            logging.error("Failed to push to HA: %s", response.text)
            response.raise_for_status()

    logging.info("Pushed %d entries to Home Assistant", len(entries))


# -----------------------------------------------------------------------------
# Main flow
# -----------------------------------------------------------------------------

def main():
    if not all([TE_API_URL, HA_URL, HA_TOKEN]):
        raise RuntimeError("Missing required environment variables")

    state = load_state()

    raw_data = fetch_data()
    entries = extract_measured_entries(raw_data)

    entries = filter_completed_days(entries)
    entries = filter_new_entries(entries, state)

    if not entries:
        logging.info("Nothing new to import.")
        return

    entries.sort(key=lambda x: x["timestamp"])

    send_to_home_assistant(entries)

    # Persist last imported timestamp
    state["last_imported"] = entries[-1]["timestamp"].isoformat()
    save_state(state)

    logging.info("Import completed successfully.")


if __name__ == "__main__":
    main()
