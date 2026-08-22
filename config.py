import os

# Target Image Dimensions (Instagram 4:5 vertical portrait)
TARGET_WIDTH = 1080
TARGET_HEIGHT = 1350
BLUR_RADIUS = 35

# Reels Video Settings (Instagram 9:16 vertical)
REELS_WIDTH = 1080
REELS_HEIGHT = 1920
REELS_DURATION = 15  # seconds
REELS_FPS = 24

# File Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
AUDIO_DIR = os.path.join(BASE_DIR, "assets", "audio")
HISTORY_FILE = os.path.join(DATA_DIR, "posted_history.json")
OUTPUT_IMAGE_PATH = os.path.join(DATA_DIR, "output_post.jpg")
OUTPUT_VIDEO_PATH = os.path.join(DATA_DIR, "output_reels.mp4")
OUTPUT_RAW_IMAGE_PATH = os.path.join(DATA_DIR, "raw_artwork.jpg")
TEMP_AUDIO_PATH = os.path.join(DATA_DIR, "temp_audio.ogg")

# API Base URLs - Metropolitan Museum of Art (no IP restrictions)
MET_API_BASE = "https://collectionapi.metmuseum.org/public/collection/v1"

# Gemini AI Settings
GEMINI_MODEL = "gemini-3.7-flash"
GEMINI_ENABLED = True

# Instagram Graph API Version & Endpoints
INSTAGRAM_GRAPH_API_VERSION = "v22.0"
GRAPH_API_BASE_URL = f"https://graph.facebook.com/{INSTAGRAM_GRAPH_API_VERSION}"

# Public domain classical music tracks with specific drop points for Reels
