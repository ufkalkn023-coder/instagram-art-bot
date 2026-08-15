import argparse
import os
import sys
import logging
import hashlib

import config
from src import art_fetcher, image_processor, instagram_poster, history_tracker, pinterest_poster, gemini_ai, content_diversity

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

def main():
    parser = argparse.ArgumentParser(description="Instagram Art Museum Automation Bot")
    parser.add_argument("--dry-run", action="store_true", help="Run bot locally without posting to Instagram")
    parser.add_argument("--image-url", type=str, help="Public HTTPS URL of the processed image (used in production)")
    args = parser.parse_args()

    logger.info("Starting Instagram Art Automation Bot...")

    # 1. Load history
    posted_ids = history_tracker.get_posted_ids()
    logger.info(f"Loaded {len(posted_ids)} previously posted artworks.")

    # 2. Fetch new public domain artwork
    logger.info("Fetching new artwork from museum APIs...")
    artwork = art_fetcher.fetch_random_artwork(posted_ids)
    logger.info(f"Selected Artwork: '{artwork['title']}' by {artwork['artist']} ({artwork['museum']})")
    logger.info(f"Alt Text (SEO): {artwork.get('alt_text')}")
    logger.info(f"Quality Score: {artwork.get('quality_score')}")

    # 2.5 Reserve artwork (PRE-WRITE) to prevent duplicates
    if not args.dry_run:
        logger.info("Reserving artwork in history (PRE-WRITE) to prevent duplicates...")
        history_tracker.reserve_artwork(artwork)

    # 3. Download and determine orientation
    raw_image_path, orientation = image_processor.prepare_local_image(artwork["local_image_path"])
    
    # 3.1 Content Type Selection
    recent_history = history_tracker.get_recent_history()
    content_type = content_diversity.select_content_type(recent_history)
    artwork["content_type"] = content_type
    
    # 3.5. ✨ Gemini AI Analysis ✨
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
    
    if ai_analysis:
        logger.info("Gemini analysis successful! Updating metadata...")
        # Keep the catalog number logic for internal logging
        ref_num = int(hashlib.md5(f"{clean_title}{clean_artist}".encode('utf-8')).hexdigest()[:8], 16) % 100000
        catalog_index = f"ARTFOLIO / REF-{ref_num:05d}"
        artwork["catalog_index"] = catalog_index  # Store internally, don't display
        
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
        if artwork["alt_text"]:
            logger.info(f"✨ SEO Alt-Text Generated: '{artwork['alt_text']}'")
    else:
        logger.info("Gemini analysis skipped or failed. Using fallback templates.")
        ref_num = int(hashlib.md5(f"{clean_title}{clean_artist}".encode('utf-8')).hexdigest()[:8], 16) % 100000
        catalog_index = f"ARTFOLIO / REF-{ref_num:05d}"
        artwork["catalog_index"] = catalog_index
        
        raw_desc = artwork.get('description', '')
        if not raw_desc:
            raw_desc = f"A classic piece titled '{clean_title}' by {clean_artist}, created in {artwork.get('date', 'unknown date')}."
            
        artist_hashtag = clean_artist.replace(" ", "").replace("-", "")
        hashtags = f"#Art #{artist_hashtag} #{artwork.get('museum', '').replace(' ', '')} #ClassicArt #ArtHistory"

        artwork["caption"] = (
            f"⠀\n"
            f"{clean_title}\n"
            f"\n"
            f"{clean_artist} - {artwork.get('date', 'Unknown')}\n"
            f"\n"
            f"{artwork.get('museum', 'Unknown')}\n"
            f"\n"
            f"{raw_desc}\n"
            f"\n"
            f"{hashtags}"
        )
        if not artwork.get("alt_text"):
            artwork["alt_text"] = f"Artwork: {clean_title} by {clean_artist}"
    
    media_type = "IMAGE"
    logger.info("Preparing FEED post...")
    output_media_path = image_processor.create_feed_post(
        raw_image_path, 
        artist_name=clean_artist, 
        artwork_title=clean_title
    )

    # 4. Post to Instagram or Dry-Run
    if args.dry_run:
        logger.info(f"[DRY-RUN MODE] Skipping actual Instagram {media_type} post upload.")
        logger.info("✅ Dry-run completed successfully!")
        return

    # Production Mode - Post to Graph API
    account_id = os.environ.get("INSTAGRAM_ACCOUNT_ID")
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    public_media_url = args.image_url or os.environ.get("PUBLIC_IMAGE_URL")

    logger.info(f"Checking environment variables: INSTAGRAM_ACCOUNT_ID={'[SET]' if account_id else '[MISSING]'}, INSTAGRAM_ACCESS_TOKEN={'[SET]' if access_token else '[MISSING]'}")

    if not account_id or not access_token:
        logger.error("❌ ERROR: Missing INSTAGRAM_ACCOUNT_ID or INSTAGRAM_ACCESS_TOKEN secrets! Please add them to your GitHub Repository Settings -> Secrets and variables -> Actions.")
        sys.exit(1)

    if not public_media_url:
        logger.info(f"PUBLIC_IMAGE_URL not specified. Automatically uploading image to temporary public HTTPS host...")
        public_media_url = image_processor.upload_temp_media(output_media_path)

    logger.info(f"Posting {media_type} to Instagram using public URL: {public_media_url}")
    media_id = instagram_poster.post_to_instagram_graph_api(
        media_url=public_media_url,
        caption=artwork["caption"],
        account_id=account_id,
        access_token=access_token,
        alt_text=artwork.get("alt_text"),
        media_type=media_type
    )

    # 5. Confirm to history (POST-WRITE)
    if not args.dry_run:
        history_tracker.confirm_artwork(artwork["id"], media_id)
        logger.info("🎉 Post completed and recorded to history successfully!")

    # 6. Post to Pinterest (Images only)
    if media_type == "IMAGE":
        logger.info("Triggering Pinterest cross-post...")
        # Clean title for Pinterest
        title = f"{artwork['title']} by {artwork['artist']}"
        desc = artwork['caption']
        if artwork.get("alt_text"):
            desc += f"\n\n(Alt: {artwork['alt_text']})"
            
        # Fetch direct permalink to the Instagram post
        permalink = instagram_poster.get_instagram_permalink(media_id, access_token)
        # Fallback to profile link if permalink fetch fails
        target_link = permalink if permalink else "https://instagram.com/ufkalkn023.db"
        
        logger.info(f"Target Pinterest link set to: {target_link}")
        pinterest_poster.post_to_pinterest(public_media_url, title, desc, target_link)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"❌ Bot failed with error: {e}")
        sys.exit(1)
    finally:
        logger.info("Cleaning up temporary files...")
        temp_files = [
            config.OUTPUT_IMAGE_PATH,
            config.OUTPUT_RAW_IMAGE_PATH
        ]
        for f in temp_files:
            if os.path.exists(f):
                try:
                    os.remove(f)
                    logger.info(f"Deleted {f}")
                except Exception as e:
                    logger.warning(f"Could not delete {f}: {e}")
