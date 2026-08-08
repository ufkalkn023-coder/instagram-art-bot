import os

# Target Image Dimensions (Instagram 4:5 vertical portrait)
TARGET_WIDTH = 1080
TARGET_HEIGHT = 1350
BLUR_RADIUS = 35

# File Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_FILE = os.path.join(DATA_DIR, "posted_history.json")
OUTPUT_IMAGE_PATH = os.path.join(DATA_DIR, "output_post.jpg")

# API Base URLs
ARTIC_API_URL = "https://api.artic.edu/api/v1/artworks"
ARTIC_IIIF_URL = "https://www.artic.edu/iiif/2"

MET_API_BASE = "https://collectionapi.metmuseum.org/public/collection/v1"

# Instagram Graph API Version & Endpoints
INSTAGRAM_GRAPH_API_VERSION = "v19.0"
GRAPH_API_BASE_URL = f"https://graph.facebook.com/{INSTAGRAM_GRAPH_API_VERSION}"
