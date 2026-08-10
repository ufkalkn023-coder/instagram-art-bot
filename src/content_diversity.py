import logging
from typing import Dict, Any, List
import random
import re

logger = logging.getLogger(__name__)

CONTENT_TYPES = [
    "SINGLE_ARTWORK",
    "ARTIST_FOCUS",
    "MUSEUM_FOCUS",
    "PERIOD_FOCUS",
    "THEME_FOCUS",
    "HISTORICAL_CONTEXT",
    "DETAIL_FOCUS"
]

def _extract_century(date_str: str) -> str:
    """Attempts to extract a century string (e.g. '1800s') from a date string."""
    if not date_str or not isinstance(date_str, str):
        return "unknown"
    # Look for 4 digit years
    match = re.search(r'\b(1[0-9]|20)\d{2}\b', date_str)
    if match:
        year = int(match.group())
        century = (year // 100) * 100
        return f"{century}s"
    return "unknown"

def _infer_visual_category(title: str, classification: str) -> str:
    """Infers a broad visual category from title and classification."""
    title = (title or "").lower()
    classification = (classification or "").lower()
    combined = f"{title} {classification}"
    
    if any(w in combined for w in ["portrait", "self-portrait", "head of", "bust of", "man", "woman", "boy", "girl"]):
        return "portrait"
    if any(w in combined for w in ["landscape", "mountain", "river", "valley", "view of", "forest", "tree"]):
        return "landscape"
    if any(w in combined for w in ["still life", "flowers", "fruit", "vase"]):
        return "still_life"
    if any(w in combined for w in ["saint", "christ", "madonna", "virgin", "crucifixion", "church", "angel", "god"]):
        return "religious"
    if any(w in combined for w in ["architecture", "building", "cathedral", "palace", "street"]):
        return "architecture"
    if any(w in combined for w in ["horse", "dog", "cat", "bird", "lion", "animal"]):
        return "animal"
    if any(w in combined for w in ["myth", "venus", "apollo", "jupiter", "diana"]):
        return "mythology"
    if any(w in combined for w in ["sea", "ship", "boat", "coast", "ocean"]):
        return "seascape"
        
    return "other"

def _infer_medium_category(medium: str, classification: str) -> str:
    """Infers a broad medium category."""
    medium = (medium or "").lower()
    classification = (classification or "").lower()
    combined = f"{medium} {classification}"
    
    if "oil" in combined or "canvas" in combined or "panel" in combined or "painting" in combined:
        return "painting"
    if "watercolor" in combined or "watercolour" in combined:
        return "watercolor"
    if "drawing" in combined or "pencil" in combined or "charcoal" in combined or "ink" in combined:
        return "drawing"
    if "print" in combined or "etching" in combined or "lithograph" in combined or "engraving" in combined:
        return "print"
    if "sculpture" in combined or "bronze" in combined or "marble" in combined or "terracotta" in combined:
        return "sculpture"
    if "photograph" in combined:
        return "photograph"
        
    return "other"

def get_candidate_metadata_features(candidate) -> Dict[str, str]:
    """Extracts diversity features from a candidate object."""
    return {
        "museum_name": getattr(candidate, "museum_name", "unknown"),
        "artist_name": getattr(candidate, "artist_name", "unknown"),
        "period": _extract_century(getattr(candidate, "creation_date", "")),
        "visual_category": _infer_visual_category(getattr(candidate, "title", ""), getattr(candidate, "classification", "")),
        "medium": _infer_medium_category(getattr(candidate, "medium", ""), getattr(candidate, "classification", ""))
    }

def analyze_museum_diversity(candidate_museum: str, recent_history: List[Dict[str, Any]]) -> float:
    """
    Penalizes museums that have been posted frequently in the recent history.
    """
    if not candidate_museum or candidate_museum == "unknown":
        return 0.0
        
    penalty = 0.0
    history_limit = min(5, len(recent_history))
    if history_limit == 0:
        return 0.0
        
    # Check the last 5 posts
    recent_museums = [post.get("museum_name", "") for post in recent_history[-history_limit:]]
    count = recent_museums.count(candidate_museum)
    
    if count == 1:
        penalty -= 2.0
    elif count == 2:
        penalty -= 5.0
    elif count >= 3:
        penalty -= 10.0
        
    return penalty

def analyze_visual_diversity(candidate_features: Dict[str, str], recent_history: List[Dict[str, Any]]) -> float:
    """
    Scores visual diversity (category, artist, medium, period).
    """
    score = 0.0
    history_limit = min(8, len(recent_history))
    if history_limit == 0:
        return score
        
    recent_posts = recent_history[-history_limit:]
    
    # 1. Artist Diversity (Heavy penalty for same artist recently)
    recent_artists = [post.get("artist_name", "") for post in recent_posts if post.get("artist_name")]
    if candidate_features["artist_name"] != "unknown Artist" and candidate_features["artist_name"] != "unknown":
        if candidate_features["artist_name"] in recent_artists[-3:]:
            score -= 15.0 # Very heavy penalty if same artist in last 3 posts
        elif candidate_features["artist_name"] in recent_artists:
            score -= 5.0
            
    # 2. Visual Category
    recent_categories = [post.get("visual_category", "") for post in recent_posts]
    cat_count = recent_categories[-4:].count(candidate_features["visual_category"])
    if candidate_features["visual_category"] != "other":
        if cat_count >= 2:
            score -= 5.0
        elif cat_count == 0:
            score += 3.0 # Bonus for fresh category
            
    # 3. Period Diversity
    recent_periods = [post.get("period", "") for post in recent_posts]
    period_count = recent_periods[-4:].count(candidate_features["period"])
    if candidate_features["period"] != "unknown":
        if period_count >= 3:
            score -= 3.0
        elif period_count == 0:
            score += 2.0
            
    # 4. Medium Diversity
    recent_mediums = [post.get("medium", "") for post in recent_posts]
    med_count = recent_mediums[-4:].count(candidate_features["medium"])
    if candidate_features["medium"] != "other":
        if med_count >= 3:
            score -= 2.0
        elif med_count == 0:
            score += 2.0
            
    return score

def select_content_type(recent_history: List[Dict[str, Any]]) -> str:
    """
    Selects an editorial content type format, avoiding recently used ones.
    """
    if not recent_history:
        return random.choice(CONTENT_TYPES)
        
    history_limit = min(3, len(recent_history))
    recent_types = [post.get("content_type", "") for post in recent_history[-history_limit:]]
    
    available_types = [t for t in CONTENT_TYPES if t not in recent_types]
    if not available_types:
        available_types = CONTENT_TYPES
        
    selected = random.choice(available_types)
    logger.info(f"Selected Content Type: {selected} (Recent: {recent_types})")
    return selected
