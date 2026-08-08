import random
import requests
import logging
from typing import Dict, Any, Optional, List
import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# Multiple search terms for diverse artwork results
MET_SEARCH_TERMS = [
    "painting", "oil painting", "portrait", "landscape", "impressionism",
    "renaissance", "baroque", "romanticism", "still life", "mythology",
    "watercolor", "classical", "neoclassicism", "realism", "symbolism"
]

CMA_TYPES = [
    "Painting", "Drawing", "Print", "Photograph"
]


def format_caption(title: str, artist: str, date: str, museum: str) -> str:
    """Formats artwork metadata into a clean Instagram caption starting on a fresh new line using Braille blank space."""
    clean_title = title.strip() if title else "Untitled"
    clean_artist = artist.strip() if artist else "Unknown Artist"
    clean_date = date.strip() if date else "Unknown Date"
    clean_museum = museum.strip() if museum else "Public Collection"

    # Instagram strips leading raw \n, so we use Braille blank character (⠀\n)
    # to force Instagram to start the artwork title on a fresh new line below the username.
    caption = (
        f"⠀\n"
        f"🎨 {clean_title}\n"
        f"👨‍🎨 {clean_artist}\n"
        f"🗓️ {clean_date}\n"
        f"🏛️ {clean_museum}"
    )
    return caption


# ──────────────────────────────────────────────────────────────────
# 1. THE METROPOLITAN MUSEUM OF ART (New York)
# ──────────────────────────────────────────────────────────────────
def fetch_met_artwork(posted_ids: set) -> Optional[Dict[str, Any]]:
    """Fetches a random public domain artwork from The Metropolitan Museum of Art."""
    try:
        search_term = random.choice(MET_SEARCH_TERMS)
        search_url = (
            f"{config.MET_API_BASE}/search"
            f"?hasImages=true&isPublicDomain=true&q={search_term}"
        )
        logger.info(f"[Met Museum] Searching with term: '{search_term}'")
        res = requests.get(search_url, headers=DEFAULT_HEADERS, timeout=20)
        if res.status_code != 200:
            return None

        object_ids = res.json().get("objectIDs", [])
        if not object_ids:
            return None

        logger.info(f"[Met Museum] Found {len(object_ids)} results for '{search_term}'.")
        unposted_ids = [oid for oid in object_ids if f"met_{oid}" not in posted_ids]
        if not unposted_ids:
            unposted_ids = object_ids

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

            image_url = detail.get("primaryImage") or detail.get("primaryImageSmall")
            if not image_url or not image_url.startswith("http"):
                continue

            title = detail.get("title") or "Untitled"
            artist = detail.get("artistDisplayName") or "Unknown Artist"
            date = detail.get("objectDate") or "Unknown Date"
            caption = format_caption(title, artist, date, "The Metropolitan Museum of Art")

            logger.info(f"[Met Museum] Selected: '{title}' by {artist}")
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
        logger.error(f"[Met Museum] Error: {e}")
    return None


# ──────────────────────────────────────────────────────────────────
# 2. CLEVELAND MUSEUM OF ART (Ohio)
#    No API key needed. Fully open access.
# ──────────────────────────────────────────────────────────────────
def fetch_cma_artwork(posted_ids: set) -> Optional[Dict[str, Any]]:
    """Fetches a random open-access artwork from Cleveland Museum of Art."""
    try:
        art_type = random.choice(CMA_TYPES)
        skip = random.randint(0, 500)
        url = (
            f"https://openaccess-api.clevelandart.org/api/artworks/"
            f"?has_image=1&limit=50&skip={skip}&type={art_type}"
        )
        logger.info(f"[Cleveland Museum] Searching type='{art_type}', skip={skip}")
        res = requests.get(url, headers=DEFAULT_HEADERS, timeout=20)
        if res.status_code != 200:
            return None

        artworks = res.json().get("data", [])
        if not artworks:
            return None

        random.shuffle(artworks)
        logger.info(f"[Cleveland Museum] Got {len(artworks)} artworks.")

        for item in artworks:
            cma_id = item.get("id")
            artwork_id = f"cma_{cma_id}"
            if artwork_id in posted_ids:
                continue

            images = item.get("images")
            if not images:
                continue

            # Prefer print quality, fallback to web quality
            image_url = None
            if isinstance(images, dict):
                print_img = images.get("print", {})
                web_img = images.get("web", {})
                image_url = print_img.get("url") or web_img.get("url")
            elif isinstance(images, list) and len(images) > 0:
                image_url = images[0].get("url")

            if not image_url or not image_url.startswith("http"):
                continue

            # Skip .tif files (too large, not web-friendly)
            if image_url.endswith(".tif"):
                web_img = images.get("web", {}) if isinstance(images, dict) else {}
                image_url = web_img.get("url")
                if not image_url:
                    continue

            title = item.get("title") or "Untitled"

            # Extract artist name from creators list
            creators = item.get("creators", [])
            if creators and isinstance(creators, list):
                first_creator = creators[0]
                # Extract clean artist name (remove lifespan and origin info)
                desc = first_creator.get("description", "")
                artist = desc.split("(")[0].strip() if "(" in desc else desc
            else:
                artist = "Unknown Artist"

            date = item.get("creation_date") or "Unknown Date"
            caption = format_caption(title, artist, date, "Cleveland Museum of Art")

            logger.info(f"[Cleveland Museum] Selected: '{title}' by {artist}")
            return {
                "id": artwork_id,
                "title": title,
                "artist": artist,
                "date": date,
                "museum": "Cleveland Museum of Art",
                "image_url": image_url,
                "caption": caption
            }
    except Exception as e:
        logger.error(f"[Cleveland Museum] Error: {e}")
    return None


# ──────────────────────────────────────────────────────────────────
# 3. RIJKSMUSEUM (Amsterdam, Netherlands)
#    Requires a free API key (set as RIJKSMUSEUM_API_KEY env var or in config).
#    If no key is available, this fetcher is silently skipped.
# ──────────────────────────────────────────────────────────────────
def fetch_rijks_artwork(posted_ids: set) -> Optional[Dict[str, Any]]:
    """Fetches a random public domain artwork from Rijksmuseum (requires API key)."""
    import os
    api_key = os.environ.get("RIJKSMUSEUM_API_KEY", getattr(config, "RIJKSMUSEUM_API_KEY", ""))
    if not api_key:
        logger.info("[Rijksmuseum] No API key configured, skipping.")
        return None

    try:
        page = random.randint(0, 100)
        url = (
            f"https://www.rijksmuseum.nl/api/en/collection"
            f"?key={api_key}&hasImage=true&type=painting&ps=50&p={page}&imgonly=true"
        )
        logger.info(f"[Rijksmuseum] Fetching page {page}...")
        res = requests.get(url, headers=DEFAULT_HEADERS, timeout=20)
        if res.status_code != 200:
            logger.warning(f"[Rijksmuseum] API returned {res.status_code}")
            return None

        artworks = res.json().get("artObjects", [])
        if not artworks:
            return None

        random.shuffle(artworks)
        logger.info(f"[Rijksmuseum] Got {len(artworks)} artworks.")

        for item in artworks:
            obj_number = item.get("objectNumber")
            artwork_id = f"rijks_{obj_number}"
            if artwork_id in posted_ids:
                continue

            web_image = item.get("webImage", {})
            image_url = web_image.get("url")
            if not image_url or not image_url.startswith("http"):
                continue

            title = item.get("title") or "Untitled"
            artist = item.get("principalOrFirstMaker") or "Unknown Artist"
            date = item.get("longTitle", "").split(",")[-1].strip() if item.get("longTitle") else "Unknown Date"
            caption = format_caption(title, artist, date, "Rijksmuseum, Amsterdam")

            logger.info(f"[Rijksmuseum] Selected: '{title}' by {artist}")
            return {
                "id": artwork_id,
                "title": title,
                "artist": artist,
                "date": date,
                "museum": "Rijksmuseum, Amsterdam",
                "image_url": image_url,
                "caption": caption
            }
    except Exception as e:
        logger.error(f"[Rijksmuseum] Error: {e}")
    return None


# ──────────────────────────────────────────────────────────────────
# MAIN ORCHESTRATOR
# ──────────────────────────────────────────────────────────────────
def fetch_random_artwork(posted_ids: set) -> Dict[str, Any]:
    """
    Fetches a random public domain artwork from a randomly chosen museum.
    Retries up to 3 times with different museums if needed.
    """
    fetchers = [
        fetch_met_artwork,
        fetch_cma_artwork,
        fetch_rijks_artwork,
    ]

    for attempt in range(1, 4):
        random.shuffle(fetchers)
        logger.info(f"Artwork fetch attempt {attempt}/3...")
        for fetcher in fetchers:
            art = fetcher(posted_ids)
            if art:
                return art
        logger.warning(f"Attempt {attempt} failed across all museums. Retrying...")

    raise RuntimeError(
        "Failed to fetch artwork after 3 attempts from all configured museum APIs!"
    )
