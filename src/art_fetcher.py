import random
import requests
import logging
from typing import Dict, Any, Optional
import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Referer": "https://www.artic.edu/"
}

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

def fetch_artic_artwork(posted_ids: set) -> Optional[Dict[str, Any]]:
    """Fetches a random public domain artwork from Art Institute of Chicago."""
    try:
        page = random.randint(1, 100)
        url = f"{config.ARTIC_API_URL}?page={page}&limit=50&fields=id,title,artist_title,date_display,image_id,is_public_domain"
        response = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
        if response.status_code != 200:
            logger.warning(f"ArtIC API error status: {response.status_code}")
            return None

        data = response.json()
        artworks = data.get("data", [])
        iiif_url = data.get("config", {}).get("iiif_url", config.ARTIC_IIIF_URL)

        # Shuffle list to get random artwork
        random.shuffle(artworks)

        for item in artworks:
            artwork_id = f"artic_{item.get('id')}"
            if artwork_id in posted_ids:
                continue

            if not item.get("is_public_domain"):
                continue

            image_id = item.get("image_id")
            if not image_id:
                continue

            title = item.get("title") or "Untitled"
            artist = item.get("artist_title") or "Unknown Artist"
            date = item.get("date_display") or "Unknown Date"
            image_url = f"{iiif_url}/{image_id}/full/1600,/0/default.jpg"

            caption = format_caption(title, artist, date, "Art Institute of Chicago")

            return {
                "id": artwork_id,
                "title": title,
                "artist": artist,
                "date": date,
                "museum": "Art Institute of Chicago",
                "image_url": image_url,
                "caption": caption
            }
    except Exception as e:
        logger.error(f"Error fetching from ArtIC: {e}")
    return None

def fetch_met_artwork(posted_ids: set) -> Optional[Dict[str, Any]]:
    """Fetches a random public domain artwork from The Metropolitan Museum of Art."""
    try:
        search_url = f"{config.MET_API_BASE}/search?hasImages=true&isPublicDomain=true&q=painting"
        res = requests.get(search_url, timeout=15)
        if res.status_code != 200:
            logger.warning(f"MET API search error: {res.status_code}")
            return None

        object_ids = res.json().get("objectIDs", [])
        if not object_ids:
            return None

        # Sample up to 30 random IDs to check
        sample_ids = random.sample(object_ids, min(30, len(object_ids)))

        for obj_id in sample_ids:
            artwork_id = f"met_{obj_id}"
            if artwork_id in posted_ids:
                continue

            detail_url = f"{config.MET_API_BASE}/objects/{obj_id}"
            d_res = requests.get(detail_url, timeout=10)
            if d_res.status_code != 200:
                continue

            detail = d_res.json()
            if not detail.get("isPublicDomain"):
                continue

            image_url = detail.get("primaryImage") or detail.get("primaryImageSmall")
            if not image_url:
                continue

            title = detail.get("title") or "Untitled"
            artist = detail.get("artistDisplayName") or "Unknown Artist"
            date = detail.get("objectDate") or "Unknown Date"

            caption = format_caption(title, artist, date, "The Metropolitan Museum of Art")

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
    """Tries fetching artwork from ArtIC or Met Museum randomly."""
    fetchers = [fetch_artic_artwork, fetch_met_artwork]
    random.shuffle(fetchers)

    for fetcher in fetchers:
        art = fetcher(posted_ids)
        if art:
            return art

    raise RuntimeError("Failed to fetch artwork from all configured museum APIs!")
