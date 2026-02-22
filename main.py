import os
import json
import time
import logging
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta, timezone

import schedule

# ==========================================
# Constants
# ==========================================

RETRY_FILE = "retry_state.json"
STATE_FILE = "import_state.json"
HISTORY_FILE = "history.json"


# ==========================================
# Logging
# ==========================================

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )


logger = logging.getLogger("tampere-energia-ha")


# ==========================================
# Configuration
# ==========================================

@dataclass
class Config:
    run_time: str

    @staticmethod
    def load():
        run_time = os.getenv("RUN_TIME", "06:00")
        return Config(run_time=run_time)


# ==========================================
# State Management (Dedup + Retry)
# ==========================================

def load_last_imported_date() -> Optional[datetime]:
    if not os.path.exists(STATE_FILE):
        return None

    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            return datetime.fromisoformat(data["last_imported"])
    except Exception:
        logger.exception("Failed to load import state.")
        return None


def save_last_imported_date(date: datetime):
    with open(STATE_FILE, "w") as f:
        json.dump({"last_imported": date.isoformat()}, f)


def save_retry_state(retry_time: datetime):
    with open(RETRY_FILE, "w") as f:
        json.dump({"retry_at": retry_time.isoformat()}, f)


def load_retry_state() -> Optional[datetime]:
    if not os.path.exists(RETRY_FILE):
        return None
    try:
        with open(RETRY_FILE, "r") as f:
            data = json.load(f)
            return datetime.fromisoformat(data["retry_at"])
    except Exception:
        return None


def clear_retry_state():
    if os.path.exists(RETRY_FILE):
        os.remove(RETRY_FILE)


# ==========================================
# Business Logic
# ==========================================

def filter_completed_days(data):
    """
    Only include entries up to day before yesterday (UTC).
    This avoids incomplete or delayed data from the portal.
    """
    if not data:
        return []

    today = datetime.now(timezone.utc).date()
    cutoff_date = today - timedelta(days=2)

    filtered = [
        item for item in data
        if datetime.fromisoformat(item["start"]).date() <= cutoff_date
    ]

    logger.info(
        f"Cutoff date: {cutoff_date}. "
        f"Keeping {len(filtered)} of {len(data)} entries."
    )

    return filtered

def filter_new_data(
    data: List[Dict[str, Any]],
    last_imported: Optional[datetime]
) -> List[Dict[str, Any]]:
    """
    Deduplicate: only include entries strictly newer
    than last successfully imported date.
    """
    if not last_imported:
        return data

    return [
        item for item in data
        if datetime.fromisoformat(item["start"]) > last_imported
    ]


def schedule_retry():
    retry_time = datetime.now(timezone.utc) + timedelta(hours=2)
    logger.warning(f"Scheduling retry at {retry_time.isoformat()}")
    save_retry_state(retry_time)


# ==========================================
# Placeholder Functions (Already Exist)
# ==========================================

def scrape_data(config: Config) -> List[Dict[str, Any]]:
    """
    Existing scraping logic.
    Must return:
    [
        {"start": ISO_STRING, "state": float},
        ...
    ]
    """
    raise NotImplementedError


def push_to_home_assistant(config: Config, data: List[Dict[str, Any]]) -> bool:
    """
    Existing HA push logic.
    Must return True on success.
    """
    raise NotImplementedError


def save_history(data: List[Dict[str, Any]]):
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                history = json.load(f)
        else:
            history = []

        history.extend(data)

        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except Exception:
        logger.exception("Failed to save history.")


# ==========================================
# Main Job
# ==========================================

def run_job(config: Config):
    logger.info("Job triggered.")

    try:
        raw_data = scrape_data(config)

        if not raw_data:
            logger.warning("No data scraped.")
            schedule_retry()
            return

        # Step 1: Only completed days
        completed = filter_completed_days(raw_data)

        if not completed:
            logger.warning("No completed days available yet.")
            schedule_retry()
            return

        # Step 2: Deduplicate by last imported date
        last_imported = load_last_imported_date()
        new_data = filter_new_data(completed, last_imported)

        if not new_data:
            logger.info("No new data to import.")
            clear_retry_state()
            return

        # Step 3: Push to HA
        success = push_to_home_assistant(config, new_data)

        if not success:
            logger.error("Push failed.")
            schedule_retry()
            return

        # Step 4: Save state + history
        newest_date = max(
            datetime.fromisoformat(item["start"])
            for item in new_data
        )

        save_last_imported_date(newest_date)
        save_history(new_data)

        clear_retry_state()

        logger.info(
            f"Successfully imported {len(new_data)} entries. "
            f"Last imported date: {newest_date.date()}"
        )

    except Exception:
        logger.exception("Fatal error during job.")
        schedule_retry()


# ==========================================
# Main Entry
# ==========================================

def main():
    setup_logging()
    config = Config.load()

    logger.info("Starting Tampere Energia importer.")

    retry_time = load_retry_state()
    now = datetime.now(timezone.utc)

    if retry_time:
        if now >= retry_time:
            logger.info("Retry time reached. Running immediately.")
            run_job(config)
        else:
            delay = (retry_time - now).total_seconds()
            logger.info(f"Retry scheduled in {int(delay)} seconds.")
            schedule.every(delay).seconds.do(
                lambda: run_job(config)
            ).tag("retry")

    # Daily schedule
    schedule.every().day.at(config.run_time).do(
        lambda: run_job(config)
    )

    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except KeyboardInterrupt:
            logger.info("Shutting down.")
            break
        except Exception:
            logger.exception("Scheduler loop error.")
            time.sleep(10)


if __name__ == "__main__":
    main()
