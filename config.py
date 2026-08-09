import os

# Target Image Dimensions (Instagram 4:5 vertical portrait)
TARGET_WIDTH = 1080
TARGET_HEIGHT = 1350
BLUR_RADIUS = 35

# Reels Video Settings (Instagram 9:16 vertical)
REELS_WIDTH = 1080
REELS_HEIGHT = 1920
REELS_DURATION = 6  # seconds
REELS_FPS = 24

# File Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_FILE = os.path.join(DATA_DIR, "posted_history.json")
OUTPUT_IMAGE_PATH = os.path.join(DATA_DIR, "output_post.jpg")
OUTPUT_VIDEO_PATH = os.path.join(DATA_DIR, "output_reels.mp4")
OUTPUT_RAW_IMAGE_PATH = os.path.join(DATA_DIR, "raw_artwork.jpg")
TEMP_AUDIO_PATH = os.path.join(DATA_DIR, "temp_audio.mp3")

# API Base URLs - Metropolitan Museum of Art (no IP restrictions)
MET_API_BASE = "https://collectionapi.metmuseum.org/public/collection/v1"

# Instagram Graph API Version & Endpoints
INSTAGRAM_GRAPH_API_VERSION = "v22.0"
GRAPH_API_BASE_URL = f"https://graph.facebook.com/{INSTAGRAM_GRAPH_API_VERSION}"

# Public domain classical music URLs (.mp3) for Reels
PUBLIC_AUDIO_URLS = [
    # Vivaldi - Spring
    "https://upload.wikimedia.org/wikipedia/commons/b/b5/Vivaldi_-_Spring_mvt_1_Allegro_-_John_Harrison_violin.mp3",
    # Debussy - Clair de lune
    "https://upload.wikimedia.org/wikipedia/commons/e/eb/Clair_de_lune_%28Claude_Debussy%29_Suite_bergamasque.mp3",
    # Chopin - Nocturne Op 9 No 2
    "https://upload.wikimedia.org/wikipedia/commons/c/ce/Chopin_Nocturne_Op_9_No_2_-_1990_recording_by_Alonso_Costas.mp3",
    # Archive.org fallbacks
    "https://archive.org/download/MoonlightSonata_755/Beethoven-MoonlightSonata.mp3",
    "https://archive.org/download/chopin_nocturnes_0808_librivox/chopin_nocturne_9_2_kb.mp3"
]
