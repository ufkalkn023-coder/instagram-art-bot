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
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_ENABLED = True

# Instagram Graph API Version & Endpoints
INSTAGRAM_GRAPH_API_VERSION = "v22.0"
GRAPH_API_BASE_URL = f"https://graph.facebook.com/{INSTAGRAM_GRAPH_API_VERSION}"

# Public domain classical music tracks with specific drop points for Reels
PUBLIC_AUDIO_TRACKS = [
    {
        "title": "J.S. Bach – Cello Suite No. 1 in G Major",
        "artist": "Johann Sebastian Bach",
        "url": "https://upload.wikimedia.org/wikipedia/commons/a/a7/Bach_Cello_Suite_1_Prelude_%28BWV_1007%29_Played_by_Chris.ogg",
        "drop_start": 0.0
    },
    {
        "title": "Beethoven – Moonlight Sonata (1st movement)",
        "artist": "Ludwig van Beethoven",
        "url": "https://upload.wikimedia.org/wikipedia/commons/e/eb/Beethoven_Moonlight_1st_movement.ogg",
        "drop_start": 12.0
    },
    {
        "title": "Chopin – Nocturne Op. 9 No. 2",
        "artist": "Frédéric Chopin",
        "url": "https://upload.wikimedia.org/wikipedia/commons/f/fd/Chopin_Nocturne_Op_9_No_2.ogg",
        "drop_start": 4.0
    },
    {
        "title": "Vivaldi – Spring (1st movement)",
        "artist": "Antonio Vivaldi",
        "url": "https://upload.wikimedia.org/wikipedia/commons/b/b2/Vivaldi_-_Spring_mvt_1_Allegro_-_John_Harrison_violin.ogg",
        "drop_start": 0.0
    },
    {
        "title": "Tchaikovsky – Dance of the Sugar Plum Fairy",
        "artist": "Pyotr Ilyich Tchaikovsky",
        "url": "https://upload.wikimedia.org/wikipedia/commons/d/d3/Tchaikovsky_-_The_Nutcracker_-_Dance_of_the_Sugar_Plum_Fairy.ogg",
        "drop_start": 7.0
    },
    {
        "title": "Debussy – Clair de lune",
        "artist": "Claude Debussy",
        "url": "https://upload.wikimedia.org/wikipedia/commons/f/f6/Claude_Debussy_-_Clair_de_lune.ogg",
        "drop_start": 0.0
    },
    {
        "title": "Satie – Gymnopédie No. 1",
        "artist": "Erik Satie",
        "url": "https://upload.wikimedia.org/wikipedia/commons/b/b3/Erik_Satie_-_Gymnop%C3%A9die_No._1.ogg",
        "drop_start": 0.0
    },
    {
        "title": "Saint-Saëns – The Swan",
        "artist": "Camille Saint-Saëns",
        "url": "https://upload.wikimedia.org/wikipedia/commons/5/5e/Saint-Saens_The_Swan.ogg",
        "drop_start": 0.0
    }
]
