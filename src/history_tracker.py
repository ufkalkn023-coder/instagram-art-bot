import json
import os
import time
import logging
from typing import Set, Dict, Any
import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def _ensure_data_dir():
    if not os.path.exists(config.DATA_DIR):
        os.makedirs(config.DATA_DIR, exist_ok=True)

def load_history() -> Dict[str, Any]:
    """Loads posted history JSON file."""
    _ensure_data_dir()
    if not os.path.exists(config.HISTORY_FILE):
        return {"posted_artworks": []}

    try:
        with open(config.HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Could not read history file ({e}), starting fresh.")
        return {"posted_artworks": []}

def get_posted_ids() -> Set[str]:
    """Returns a set of artwork IDs that have already been posted."""
    history = load_history()
    posted_list = history.get("posted_artworks", [])
    return {item["id"] for item in posted_list if "id" in item}

def save_posted_artwork(artwork_data: Dict[str, Any], media_id: str = "dry_run_id"):
    """Appends a new artwork record to history and writes to JSON."""
    _ensure_data_dir()
    history = load_history()

    record = {
        "id": artwork_data["id"],
        "title": artwork_data.get("title"),
        "artist": artwork_data.get("artist"),
        "museum": artwork_data.get("museum"),
        "media_id": media_id,
        "posted_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }

    history.setdefault("posted_artworks", []).append(record)

    with open(config.HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    logger.info(f"Saved artwork {artwork_data['id']} to history.")
