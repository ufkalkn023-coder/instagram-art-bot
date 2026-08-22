import argparse
import os
import sys
import logging
import hashlib
from datetime import datetime, timezone
import random

import config
from src import art_fetcher, image_processor, instagram_poster, history_tracker, pinterest_poster, gemini_ai, content_diversity

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _get_grid_color_tone_for_run(dry_run: bool) -> str:
    """Read grid state without allowing dry-run to create a new row."""
    if dry_run:
        return history_tracker.get_grid_color_tone(read_only=True)
    return history_tracker.get_grid_color_tone()


def _log_dry_run_success(mode: str, artworks: list[dict], artifact_paths: list[str]) -> None:
    """Report the local artifacts produced without invoking publish mutations."""
    selected_ids = ",".join(artwork["id"] for artwork in artworks)
    quality_summary = ",".join(
        f"{artwork['id']}:quality={artwork.get('quality_score')} selection={artwork.get('selection_score')}"
        for artwork in artworks
    )
    logger.info(
        "DRY RUN SUCCESS mode=%s selected_ids=%s local_artifacts=%s scores=%s "
        "history_mutation=skipped media_upload=skipped instagram_publish=skipped pinterest_publish=skipped",
        mode,
        selected_ids,
        ",".join(artifact_paths),
        quality_summary,
    )


def run_single_post(args):
    logger.info("Running single post logic...")
    posted_ids = history_tracker.get_posted_ids()
    color_tone = _get_grid_color_tone_for_run(args.dry_run)
    
    try:
        artworks = art_fetcher.fetch_themed_artworks(posted_ids, "", 1, color_tone)
        artwork = artworks[0]
    except Exception as e:
        logger.error(f"Failed to fetch themed artwork: {e}. Falling back to random...")
        artwork = art_fetcher.fetch_random_artwork(posted_ids)

    if args.dry_run:
        logger.info("[DRY-RUN MODE] Skipping history reservation.")
    else:
        logger.info("Reserving artwork in history (PRE-WRITE)...")
        history_tracker.reserve_artwork(artwork)
    
    raw_image_path = image_processor.prepare_local_image(artwork["local_image_path"])[0]
    
    recent_history = history_tracker.get_recent_history()
    content_type = content_diversity.select_content_type(recent_history)
    artwork["content_type"] = content_type
    
    logger.info(f"Analyzing artwork with Google Gemini AI (Content Type: {content_type})...")
    ai_analysis = gemini_ai.analyze_artwork(
        raw_image_path, 
        artwork["title"], 
        artwork["artist"], 
        artwork["date"], 
        artwork["museum"],
        artwork.get("medium", ""),
        artwork.get("classification", ""),
        content_type=content_type
    )
    
    clean_title = artwork['title'].strip() if artwork.get('title') else "Untitled"
    clean_artist = artwork['artist'].strip() if artwork.get('artist') else "Unknown Artist"
    
    base_font_size = 46
    if ai_analysis:
        logger.info("Gemini analysis successful! Updating metadata...")
        base_font_size = ai_analysis.get("recommended_font_size", 46)
        
        ref_num = int(hashlib.md5(f"{clean_title}{clean_artist}".encode('utf-8')).hexdigest()[:8], 16) % 100000
        catalog_index = f"ARTFOLIO / REF-{ref_num:05d}"
        artwork["catalog_index"] = catalog_index
        
        artwork["caption"] = (
            f"⠀\n"
            f"{clean_title}\n"
            f"\n"
            f"{clean_artist} - {artwork.get('date', 'Unknown')}\n"
            f"\n"
            f"{artwork.get('museum', 'Unknown')}\n"
            f"\n"
            f"{ai_analysis.get('caption', '')}\n"
            f"\n"
            f"{ai_analysis.get('hashtags', '')}"
        )
        artwork["alt_text"] = ai_analysis.get("alt_text", artwork.get("alt_text", ""))
    else:
        logger.info("Gemini analysis skipped or failed. Using fallback templates.")
        raw_desc = artwork.get('description', '')
        if not raw_desc:
            raw_desc = f"A classic piece titled '{clean_title}' by {clean_artist}, created in {artwork.get('date', 'unknown date')}."
        artist_hashtag = clean_artist.replace(" ", "").replace("-", "")
        hashtags = f"#Art #{artist_hashtag} #{artwork.get('museum', '').replace(' ', '')} #ClassicArt #ArtHistory"
        artwork["caption"] = (f"⠀\n{clean_title}\n\n{clean_artist} - {artwork.get('date', 'Unknown')}\n\n{artwork.get('museum', 'Unknown')}\n\n{raw_desc}\n\n{hashtags}")
        artwork["alt_text"] = f"Artwork: {clean_title} by {clean_artist}"
    
    output_media_path = image_processor.create_feed_post(
        raw_image_path, 
        artist_name=clean_artist, 
        artwork_title=clean_title,
        base_font_size=base_font_size
    )

    if args.dry_run:
        _log_dry_run_success("single", [artwork], [output_media_path])
        return

    account_id = os.environ.get("INSTAGRAM_ACCOUNT_ID")
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    public_media_url = args.image_url or os.environ.get("PUBLIC_IMAGE_URL")

    if not public_media_url:
        public_media_url = image_processor.upload_temp_media(output_media_path)

    try:
        # Durable non-expiring lock: if this R2 write fails, do not cross the
        # Instagram publish boundary.
        history_tracker.mark_artworks_publishing([artwork["id"]])
        media_id = instagram_poster.post_to_instagram_graph_api(
            media_url=public_media_url,
            caption=artwork["caption"],
            account_id=account_id,
            access_token=access_token,
            alt_text=artwork.get("alt_text"),
            media_type="IMAGE"
        )
    except instagram_poster.InstagramPublishAmbiguousError:
        logger.error("Instagram publish result is ambiguous; preserving the duplicate lock.")
        try:
            history_tracker.mark_artwork_ambiguous(artwork["id"])
        except Exception:
            logger.exception("Failed to preserve the ambiguous single-post reservation.")
            raise
        raise
    except Exception:
        # Definite failures remain retryable when the durable rollback works.
        # If it does not, PUBLISHING remains the safe source-of-truth fallback.
        try:
            history_tracker.mark_artworks_pending([artwork["id"]])
        except Exception:
            logger.exception("Failed to roll back single-post publish lock; preserving PUBLISHING state.")
        raise

    history_tracker.confirm_artwork(artwork["id"], media_id)
    
    if args.pinterest:
        logger.info("Triggering Pinterest cross-post...")
        title = f"{artwork['title']} by {artwork['artist']}"
        pinterest_poster.post_to_pinterest(
            image_url=public_media_url,
            title=title[:100],
            description=artwork["caption"],
            link=public_media_url
        )


def run_carousel_post(args):
    logger.info("Running carousel post logic...")
    posted_ids = history_tracker.get_posted_ids()
    color_tone = _get_grid_color_tone_for_run(args.dry_run)
    
    themes = ["cat", "landscape", "portrait", "flower", "dog", "sea", "mountain", "horse", "angel", "battle", "winter", "ship", "bridge"]
    theme = random.choice(themes)
    logger.info(f"Selected Carousel Theme: {theme}")
    
    artworks = art_fetcher.fetch_themed_artworks(posted_ids, theme, count=8, color_tone=color_tone)
    logger.info(f"Fetched {len(artworks)} artworks for the carousel.")
    
    if args.dry_run:
        logger.info("[DRY-RUN MODE] Skipping history reservations.")
    else:
        for art in artworks:
            history_tracker.reserve_artwork(art)
        
    ai_analysis = gemini_ai.analyze_carousel(theme, artworks)
    base_font_size = ai_analysis.get("recommended_font_size", 46) if ai_analysis else 46
    caption = ai_analysis.get("caption", f"A curated collection of {theme} artworks.") if ai_analysis else f"Curated {theme}."
    hashtags = ai_analysis.get("hashtags", f"#Art #{theme} #ClassicArt") if ai_analysis else f"#{theme}"
    theme_title = ai_analysis.get("theme_title", theme.title()) if ai_analysis else theme.title()
    
    # Sırasıyla eser listesini oluştur
    artwork_list_text = ""
    for i, art in enumerate(artworks, 1):
        clean_title = art.get('title', 'Untitled').strip()
        clean_artist = art.get('artist', 'Unknown Artist').strip()
        date = art.get('date', 'Unknown')
        artwork_list_text += f"{i}. {clean_title} - {clean_artist} ({date})\n"
    
    final_caption = f"⠀\n{theme_title}\n\n{caption}\n\nFeatured Artworks (In Order):\n{artwork_list_text}\n{hashtags}"
    
    public_urls = []
    output_media_paths = []
    
    for art in artworks:
        raw_image_path = image_processor.prepare_local_image(art["local_image_path"])[0]
        clean_title = art['title'].strip() if art.get('title') else "Untitled"
        clean_artist = art['artist'].strip() if art.get('artist') else "Unknown Artist"
        
        output_media_path = image_processor.create_feed_post(
            raw_image_path, 
            artist_name=clean_artist, 
            artwork_title=clean_title,
            base_font_size=base_font_size
        )
        output_media_paths.append(output_media_path)
        
        if not args.dry_run:
            url = image_processor.upload_temp_media(output_media_path)
            public_urls.append(url)
            
    if args.dry_run:
        _log_dry_run_success("carousel", artworks, output_media_paths)
        return
        
    account_id = os.environ.get("INSTAGRAM_ACCOUNT_ID")
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    
    try:
        # Protect every carousel child in one conditional R2 update before the
        # parent media_publish request is allowed to run.
        history_tracker.mark_artworks_publishing(art["id"] for art in artworks)
        carousel_id = instagram_poster.post_carousel_to_instagram_graph_api(
            media_urls=public_urls,
            caption=final_caption,
            account_id=account_id,
            access_token=access_token
        )
    except instagram_poster.InstagramPublishAmbiguousError:
        logger.error("Instagram carousel publish result is ambiguous; preserving duplicate locks.")
        try:
            history_tracker.mark_artworks_ambiguous(art["id"] for art in artworks)
        except Exception:
            logger.exception("Failed to preserve ambiguous carousel reservations.")
            raise
        raise
    except Exception:
        try:
            history_tracker.mark_artworks_pending(art["id"] for art in artworks)
        except Exception:
            logger.exception("Failed to roll back carousel publish locks; preserving PUBLISHING state.")
        raise
    
    for art in artworks:
        history_tracker.confirm_artwork(art["id"], carousel_id)


def main():
    parser = argparse.ArgumentParser(description="Instagram Art Museum Automation Bot")
    parser.add_argument("--dry-run", action="store_true", help="Run bot locally without posting to Instagram")
    parser.add_argument("--force-carousel", action="store_true", help="Force the bot to post a carousel")
    parser.add_argument("--image-url", type=str, help="Skip museum fetch and use this specific public image URL instead")
    parser.add_argument("--pinterest", action="store_true", help="Also cross-post to Pinterest")
    args = parser.parse_args()

    logger.info("Starting Instagram Art Automation Bot...")
    
    current_hour = datetime.now(timezone.utc).hour
    is_carousel_time = current_hour in [12, 21]
    
    try:
        if args.dry_run:
            logger.info("[DRY-RUN MODE] Skipping stale reservation recovery.")
        else:
            history_tracker.recover_stale_reservations()
        if args.force_carousel or is_carousel_time:
            run_carousel_post(args)
        else:
            run_single_post(args)
    except Exception as e:
        logger.error(f"❌ Bot failed with error: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
