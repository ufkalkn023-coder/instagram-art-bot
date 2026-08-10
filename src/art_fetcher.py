import logging
from typing import Dict, Any, List
from src.museums import AICAdapter, ClevelandAdapter, MetAdapter, RijksmuseumAdapter
from src.quality_filter import calculate_quality_score, validate_and_download_image
from src import history_tracker
from src import content_diversity
import config

logger = logging.getLogger(__name__)

# Default weights if not specified in config
DEFAULT_WEIGHTS = {
    "aic": 15,
    "rijksmuseum": 15,
    "met": 15,
    "cleveland": 15
}

def fetch_random_artwork(posted_ids: set) -> Dict[str, Any]:
    """
    Orchestrates the new Image-First pipeline:
    1. Fetches candidates from all museums.
    2. Normalizes them.
    3. Filters out duplicates.
    4. Scores them based on quality.
    5. Validates the top candidate's image.
    6. Returns the best valid artwork as a dictionary compatible with the rest of the pipeline.
    """
    adapters = [
        AICAdapter(),
        ClevelandAdapter(),
        MetAdapter(),
        RijksmuseumAdapter()
    ]
    
    museum_weights = getattr(config, "MUSEUM_SOURCE_WEIGHTS", DEFAULT_WEIGHTS)
    min_score = getattr(config, "MIN_QUALITY_SCORE", 50)
    
    all_candidates = []
    
    # 1. Fetch from all adapters
    for adapter in adapters:
        logger.info(f"Fetching candidates from {adapter.source_id}...")
        candidates = adapter.fetch_candidates(limit=15)
        all_candidates.extend(candidates)
        
    logger.info(f"Total raw candidates fetched: {len(all_candidates)}")
    
    # 2. Filter duplicates
    new_candidates = [c for c in all_candidates if c.canonical_id not in posted_ids]
    logger.info(f"Candidates after duplicate filter: {len(new_candidates)}")
    
    if not new_candidates:
        raise RuntimeError("No new public domain artworks found across any museum!")
        
    # 3. Score candidates with Diversity Penalty
    recent_history = history_tracker.get_recent_history()
        
    scored_candidates = []
    for c in new_candidates:
        base_score = calculate_quality_score(c, museum_weights)
        
        # Calculate diversity penalties/bonuses
        museum_penalty = content_diversity.analyze_museum_diversity(c.museum_name, recent_history)
        features = content_diversity.get_candidate_metadata_features(c)
        visual_bonus = content_diversity.analyze_visual_diversity(features, recent_history)
        
        c.quality_score = base_score + museum_penalty + visual_bonus
        
        # Attach features for later use
        c._diversity_features = features
        
        if base_score >= min_score: # Apply min_score threshold to BASE score, so diversity doesn't rescue a bad image
            scored_candidates.append(c)
            
    # Sort by score descending
    scored_candidates.sort(key=lambda x: x.quality_score, reverse=True)
    logger.info(f"Candidates passing quality threshold ({min_score}): {len(scored_candidates)}")
    
    if not scored_candidates:
        raise RuntimeError(f"No candidates passed the quality threshold of {min_score}!")
        
    # 4. Image Validation Loop (Try best scoring artworks first)
    for best_candidate in scored_candidates:
        logger.info(f"Testing candidate: {best_candidate.canonical_id} | Score: {best_candidate.quality_score} | Title: {best_candidate.title}")
        
        is_valid = validate_and_download_image(best_candidate.image_url, config.OUTPUT_RAW_IMAGE_PATH)
        if is_valid:
            logger.info(f"✅ Image downloaded and validated successfully for {best_candidate.canonical_id}")
            
            # 5. Return dict format expected by history_tracker and Gemini
            features = getattr(best_candidate, "_diversity_features", {})
            return {
                "id": best_candidate.canonical_id, # Canonical ID for duplicate prevention
                "title": best_candidate.title,
                "artist": best_candidate.artist_name,
                "date": best_candidate.creation_date,
                "museum": best_candidate.museum_name,
                "image_url": best_candidate.image_url,
                "local_image_path": config.OUTPUT_RAW_IMAGE_PATH, # Pass the local path down
                "alt_text": f"{best_candidate.title} by {best_candidate.artist_name}, {best_candidate.creation_date}",
                # Passed down for prompt/metadata context
                "medium": best_candidate.medium,
                "classification": best_candidate.classification,
                "quality_score": best_candidate.quality_score,
                # Passed down to reserve_artwork
                "visual_category": features.get("visual_category", "other"),
                "period": features.get("period", "unknown"),
            }
        else:
            logger.warning(f"❌ Image validation failed for {best_candidate.canonical_id}. Trying next best candidate...")
            
    raise RuntimeError("All top candidates failed image validation (Hard Reject)!")
