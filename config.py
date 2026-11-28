import os

# Reels Video Settings (Instagram 9:16 vertical)
REELS_WIDTH = 1080

# File Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_FILE = os.path.join(DATA_DIR, "posted_history.json")
OUTPUT_IMAGE_PATH = os.path.join(DATA_DIR, "output_post.jpg")
OUTPUT_RAW_IMAGE_PATH = os.path.join(DATA_DIR, "raw_artwork.jpg")

# API Base URLs - Metropolitan Museum of Art (no IP restrictions).
# Object details remain on v1; search is independently versioned and paginated.
MET_API_BASE = "https://collectionapi.metmuseum.org/public/collection/v1"
MET_SEARCH_API_BASE = "https://collectionapi.metmuseum.org/public/collection/v1.1"

# Gemini AI Settings
GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_ENABLED = True

# Instagram Graph API Version & Endpoints. Keep one version source for publish
# and read-only analytics; deployments can pin a newer tested version.
INSTAGRAM_GRAPH_API_VERSION = os.environ.get("INSTAGRAM_GRAPH_API_VERSION", "v22.0").strip() or "v22.0"
GRAPH_API_BASE_URL = f"https://graph.facebook.com/{INSTAGRAM_GRAPH_API_VERSION}"
