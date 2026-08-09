import random
import re
import requests
import logging
from typing import Dict, Any, Optional, List
import hashlib
import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# Search terms specifically targeted at paintings
MET_SEARCH_TERMS = [
    "painting", "oil painting", "impressionism painting", "renaissance painting",
    "baroque painting", "portrait painting", "landscape painting", "still life painting",
    "watercolor painting", "realism painting", "romanticism painting", "symbolism painting"
]

CMA_TYPES = [
    "Painting"
]

# Keywords to strictly exclude non-painting items (vases, armors, sculptures, ceramics, fragments, etc.)
NON_PAINTING_KEYWORDS = {
    "vase", "sculpture", "armor", "armour", "fragment", "stucco", "ceramic", "porcelain",
    "coin", "medal", "glass", "furniture", "weapon", "sword", "dagger", "textile", "rug",
    "costume", "jewelry", "jewellery", "tapestry", "pottery", "bowl", "plate", "cup",
    "statue", "clock", "reliquary", "breastplate", "helm", "helmet", "jar", "jug", "pitcher"
}

# ──────────────────────────────────────────────────────────────────
# HASHTAG GENERATION
# ──────────────────────────────────────────────────────────────────

# Museum name → hashtag mapping
MUSEUM_HASHTAGS = {
    "The Metropolitan Museum of Art": ["#MetMuseum", "#TheMetNYC", "#MetropolitanMuseum"],
    "Cleveland Museum of Art": ["#ClevelandMuseumOfArt", "#CMAart", "#ClevelandArt"],
    "Rijksmuseum, Amsterdam": ["#Rijksmuseum", "#RijksmuseumAmsterdam", "#DutchArt"],
}

# Core art hashtags that always appear (a pool to sample from)
CORE_ART_HASHTAGS = [
    "#Art", "#FineArt", "#ClassicArt", "#ArtHistory", "#Painting",
    "#Museum", "#ArtOfTheDay", "#MasterPiece", "#ArtLovers", "#InstaArt",
    "#ArtGallery", "#TimelessArt", "#ArtAppreciation", "#DailyArt",
    "#ArtisticLegacy", "#WorldOfArt", "#ArtInspiration",
]

# Keyword → thematic hashtags (matched against title, date, medium, etc.)
THEMATIC_HASHTAG_MAP = {
    "portrait": ["#Portrait", "#PortraitPainting", "#PortraitArt"],
    "landscape": ["#Landscape", "#LandscapePainting", "#LandscapeArt"],
    "still life": ["#StillLife", "#StillLifePainting", "#StillLifeArt"],
    "mythology": ["#Mythology", "#MythologicalArt", "#MythArt"],
    "impressionism": ["#Impressionism", "#ImpressionistArt"],
    "renaissance": ["#Renaissance", "#RenaissanceArt"],
    "baroque": ["#Baroque", "#BaroqueArt"],
    "romanticism": ["#Romanticism", "#RomanticArt"],
    "neoclassicism": ["#Neoclassicism", "#NeoclassicalArt"],
    "realism": ["#Realism", "#RealistArt"],
    "symbolism": ["#Symbolism", "#SymbolistArt"],
    "watercolor": ["#Watercolor", "#WatercolorArt", "#WatercolorPainting"],
    "oil": ["#OilPainting", "#OilOnCanvas"],
    "drawing": ["#Drawing", "#DrawingArt", "#Sketch"],
    "print": ["#Printmaking", "#PrintArt"],
    "photograph": ["#Photography", "#VintagePhotography", "#ArtPhotography"],
    "religious": ["#ReligiousArt", "#SacredArt"],
    "sculpture": ["#Sculpture", "#SculptureArt"],
    "abstract": ["#AbstractArt", "#Abstract"],
    "nature": ["#NatureInArt", "#NaturePainting"],
    "flower": ["#FlowerPainting", "#FloralArt", "#BotanicalArt"],
    "sea": ["#Seascape", "#MarineArt"],
    "war": ["#WarArt", "#BattlePainting"],
    "classical": ["#ClassicalArt", "#AncientArt"],
}


def _sanitize_hashtag(text: str) -> str:
    """Converts a text string into a valid hashtag (letters/digits only, CamelCase)."""
    # Remove parenthetical info like "(American, 1832-1910)"
    text = re.sub(r"\(.*?\)", "", text).strip()
    # Split into words, capitalize each, remove non-alphanumeric
    words = text.split()
    cleaned = "".join(re.sub(r"[^A-Za-z0-9]", "", w).capitalize() for w in words)
    return f"#{cleaned}" if cleaned else ""


def generate_hashtags(title: str, artist: str, date: str, museum: str,
                      medium: str = "", search_term: str = "") -> str:
    """
    Generates a block of relevant hashtags for an artwork post.
    Returns a string of hashtags separated by spaces.
    """
    hashtags: list[str] = []

    # 1. Artist hashtag (e.g., #VanGogh, #ClaudeMonet)
    if artist and artist != "Unknown Artist":
        artist_tag = _sanitize_hashtag(artist)
        if artist_tag and len(artist_tag) > 1:
            hashtags.append(artist_tag)

    # 2. Museum-specific hashtags
    museum_tags = MUSEUM_HASHTAGS.get(museum, [])
    if museum_tags:
        hashtags.append(museum_tags[0])  # Take primary museum tag

    # 3. Thematic hashtags based on title, medium, and search term
    searchable_text = f"{title} {medium} {search_term} {date}".lower()
    matched_thematic: list[str] = []
    for keyword, tags in THEMATIC_HASHTAG_MAP.items():
        if keyword in searchable_text:
            matched_thematic.extend(tags)
    # Deduplicate and limit thematic tags
    matched_thematic = list(dict.fromkeys(matched_thematic))
    hashtags.extend(matched_thematic[:2])

    # 4. Core art hashtags — fill up to 7-8 total hashtags
    target_count = 8
    remaining_slots = max(0, target_count - len(hashtags))
    if remaining_slots > 0:
        core_sample = random.sample(CORE_ART_HASHTAGS, min(remaining_slots, len(CORE_ART_HASHTAGS)))
        hashtags.extend(core_sample)

    # Deduplicate while preserving order, cap at 8
    seen: set[str] = set()
    unique_hashtags: list[str] = []
    for tag in hashtags:
        tag_lower = tag.lower()
        if tag_lower not in seen:
            seen.add(tag_lower)
            unique_hashtags.append(tag)
    unique_hashtags = unique_hashtags[:8]

    return " ".join(unique_hashtags)


def format_alt_text(title: str, artist: str, date: str) -> str:
    """Formats artwork metadata into an accessibility Alt Text for Instagram SEO."""
    clean_title = title.strip() if title else "Untitled"
    clean_artist = artist.strip() if artist else "Unknown Artist"
    clean_date = date.strip() if date else "Unknown Date"
    return f"{clean_title} by {clean_artist}, {clean_date} fine art"


def format_caption(title: str, artist: str, date: str, museum: str,
                   medium: str = "", search_term: str = "") -> str:
    """Formats artwork metadata into a clean Instagram caption with hashtags."""
    clean_title = title.strip() if title else "Untitled"
    clean_artist = artist.strip() if artist else "Unknown Artist"
    clean_date = date.strip() if date else "Unknown Date"
    clean_museum = museum.strip() if museum else "Public Collection"

    # Generate relevant hashtags
    hashtags = generate_hashtags(clean_title, clean_artist, clean_date,
                                clean_museum, medium, search_term)

    # Generate DB index (Catalog Number)
    ref_num = int(hashlib.md5(f"{clean_title}{clean_artist}".encode('utf-8')).hexdigest()[:8], 16) % 100000
    catalog_index = f"ARTFOLIO / REF-{ref_num:05d}"

    # Instagram strips leading raw \n, so we use Braille blank character (⠀\n)
    # to force Instagram to start the artwork title on a fresh new line below the username.
    caption = (
        f"⠀\n"
        f"🎨 {clean_title}\n"
        f"👨‍🎨 {clean_artist}\n"
        f"🗓️ {clean_date}\n"
        f"🏛️ {clean_museum}\n"
        f"🗃️ {catalog_index}\n"
        f"\n"
        f"This post was automatically fetched and published by a bot.\n"
        f"\n"
        f"⠀\n"
        f"{hashtags}"
    )
    return caption


def _is_painting(title: str, object_name: str = "", classification: str = "", medium: str = "") -> bool:
    """Verifies that an artwork is actually a painting and not a vase, armor, sculpture, or 3D object."""
    combined_text = f"{title} {object_name} {classification} {medium}".lower()

    for keyword in NON_PAINTING_KEYWORDS:
        if re.search(rf"\b{keyword}s?\b", combined_text):
            return False

    return True


# ──────────────────────────────────────────────────────────────────
# 1. THE METROPOLITAN MUSEUM OF ART (New York)
# ──────────────────────────────────────────────────────────────────
def fetch_met_artwork(posted_ids: set) -> Optional[Dict[str, Any]]:
    """Fetches a random public domain painting from The Metropolitan Museum of Art."""
    try:
        search_term = random.choice(MET_SEARCH_TERMS)
        search_url = (
            f"{config.MET_API_BASE}/search"
            f"?hasImages=true&isPublicDomain=true&medium=Paintings&q={search_term}"
        )
        logger.info(f"[Met Museum] Searching paintings with term: '{search_term}'")
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

            title = detail.get("title") or "Untitled"
            object_name = detail.get("objectName") or ""
            classification = detail.get("classification") or ""
            medium = detail.get("medium") or ""

            # Strict check to ensure it's a painting and not a 3D object / vase / armor
            if not _is_painting(title, object_name, classification, medium):
                logger.info(f"[Met Museum] Skipping non-painting item: '{title}' ({object_name}/{classification})")
                continue

            image_url = detail.get("primaryImage") or detail.get("primaryImageSmall")
            if not image_url or not image_url.startswith("http"):
                continue

            artist = detail.get("artistDisplayName") or "Unknown Artist"
            date = detail.get("objectDate") or "Unknown Date"
            caption = format_caption(title, artist, date, "The Metropolitan Museum of Art",
                                    medium=medium, search_term=search_term)
            alt_text = format_alt_text(title, artist, date)

            logger.info(f"[Met Museum] Selected painting: '{title}' by {artist}")
            return {
                "id": artwork_id,
                "title": title,
                "artist": artist,
                "date": date,
                "museum": "The Metropolitan Museum of Art",
                "image_url": image_url,
                "caption": caption,
                "alt_text": alt_text
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
            medium = item.get("technique") or item.get("type") or ""

            if not _is_painting(title, medium=medium):
                logger.info(f"[Cleveland Museum] Skipping non-painting item: '{title}' ({medium})")
                continue

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
            caption = format_caption(title, artist, date, "Cleveland Museum of Art",
                                    medium=medium, search_term=art_type)
            alt_text = format_alt_text(title, artist, date)

            logger.info(f"[Cleveland Museum] Selected painting: '{title}' by {artist}")
            return {
                "id": artwork_id,
                "title": title,
                "artist": artist,
                "date": date,
                "museum": "Cleveland Museum of Art",
                "image_url": image_url,
                "caption": caption,
                "alt_text": alt_text
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
            if not _is_painting(title, medium="painting"):
                continue
            artist = item.get("principalOrFirstMaker") or "Unknown Artist"
            date = item.get("longTitle", "").split(",")[-1].strip() if item.get("longTitle") else "Unknown Date"
            caption = format_caption(title, artist, date, "Rijksmuseum, Amsterdam",
                                    medium="painting", search_term="painting")
            alt_text = format_alt_text(title, artist, date)

            logger.info(f"[Rijksmuseum] Selected: '{title}' by {artist}")
            return {
                "id": artwork_id,
                "title": title,
                "artist": artist,
                "date": date,
                "museum": "Rijksmuseum, Amsterdam",
                "image_url": image_url,
                "caption": caption,
                "alt_text": alt_text
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
