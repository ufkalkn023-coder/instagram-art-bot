import random
import requests
import logging
from typing import Dict, Any, Optional
import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# Multiple search terms for diverse artwork results from Met Museum
MET_SEARCH_TERMS = [
    "painting", "oil painting", "portrait", "landscape", "impressionism",
    "renaissance", "baroque", "romanticism", "still life", "mythology"
]

def format_caption(title: str, artist: str, date: str, museum: str) -> str:
    """Formats artwork metadata into a clean Instagram caption without hashtags or extra text."""
    clean_title = title.strip() if title else "Untitled"
    clean_artist = artist.strip() if artist else "Unknown Artist"
    clean_date = date.strip() if date else "Unknown Date"
    clean_museum = museum.strip() if museum else "Public Collection"

    caption = (
        f"🎨 {clean_title}\n"
        f"👨‍🎨 {clean_artist}\n"
        f"🗓️ {clean_date}\n"
        f"🏛️ {clean_museum}"
    )
    return caption

def fetch_met_artwork(posted_ids: set) -> Optional[Dict[str, Any]]:
    """
    Fetches a random public domain painting from The Metropolitan Museum of Art.
    Uses multiple search terms for variety and samples a wider pool to find new artworks.
    """
    try:
        # Pick a random search term for diversity
        search_term = random.choice(MET_SEARCH_TERMS)
        search_url = (
            f"{config.MET_API_BASE}/search"
            f"?hasImages=true&isPublicDomain=true&q={search_term}"
        )
        logger.info(f"Searching Met Museum with term: '{search_term}'")
        res = requests.get(search_url, headers=DEFAULT_HEADERS, timeout=20)
        if res.status_code != 200:
            logger.warning(f"MET API search error: {res.status_code}")
            return None

        object_ids = res.json().get("objectIDs", [])
        if not object_ids:
            logger.warning("No object IDs returned from Met Museum search.")
            return None

        logger.info(f"Met Museum returned {len(object_ids)} results for '{search_term}'.")

        # Filter out already-posted IDs first
        unposted_ids = [oid for oid in object_ids if f"met_{oid}" not in posted_ids]
        if not unposted_ids:
            unposted_ids = object_ids  # fallback: ignore history if all posted

        # Sample a larger pool to increase chances of finding a valid image
        sample_ids = random.sample(unposted_ids, min(50, len(unposted_ids)))

        for obj_id in sample_ids:
            artwork_id = f"met_{obj_id}"

            detail_url = f"{config.MET_API_BASE}/objects/{obj_id}"
            d_res = requests.get(detail_url, headers=DEFAULT_HEADERS, timeout=15)
            if d_res.status_code != 200:
                continue

            detail = d_res.json()

            if not detail.get("isPublicDomain"):
                continue

            # Prefer high-res primaryImage, fallback to small version
            image_url = detail.get("primaryImage") or detail.get("primaryImageSmall")
            if not image_url:
                continue

            # Skip if image URL looks invalid
            if not image_url.startswith("http"):
                continue

            title = detail.get("title") or "Untitled"
            artist = detail.get("artistDisplayName") or "Unknown Artist"
            date = detail.get("objectDate") or "Unknown Date"

            caption = format_caption(title, artist, date, "The Metropolitan Museum of Art")

            logger.info(f"Found artwork: '{title}' by {artist} ({date})")
            return {
                "id": artwork_id,
                "title": title,
                "artist": artist,
                "date": date,
                "museum": "The Metropolitan Museum of Art",
                "image_url": image_url,
                "caption": caption
            }

    except Exception as e:
        logger.error(f"Error fetching from Met Museum: {e}")
    return None

def fetch_random_artwork(posted_ids: set) -> Dict[str, Any]:
    """
    Fetches a random public domain artwork. Retries up to 3 times if needed.
    Exclusively uses The Metropolitan Museum of Art API (no IP blocking).
    """
    for attempt in range(1, 4):
        logger.info(f"Artwork fetch attempt {attempt}/3...")
        art = fetch_met_artwork(posted_ids)
        if art:
            return art
        logger.warning(f"Attempt {attempt} failed. Retrying...")

    raise RuntimeError(
        "Failed to fetch artwork after 3 attempts from the Metropolitan Museum of Art API!"
    )
