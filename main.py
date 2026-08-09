import argparse
import os
import sys
import logging

import config
from src import art_fetcher, image_processor, instagram_poster, history_tracker, pinterest_poster

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
    logger.info(f"Caption:\n---\n{artwork['caption']}\n---")

    # 3. Download and determine orientation
    raw_image_path, orientation = image_processor.download_raw_image(artwork["image_url"])
    
    media_type = "IMAGE"
    if orientation == "horizontal":
        logger.info("Horizontal image detected. Preparing REELS video...")
        output_media_path = image_processor.create_reels_video(raw_image_path)
        media_type = "REELS"
    else:
        logger.info("Vertical/Square image detected. Preparing FEED post...")
        output_media_path = image_processor.create_feed_post(raw_image_path)

    # 4. Post to Instagram or Dry-Run
    if args.dry_run:
        logger.info(f"[DRY-RUN MODE] Skipping actual Instagram {media_type} post upload.")
        history_tracker.save_posted_artwork(artwork, media_id="dry_run_id")
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
        logger.info(f"PUBLIC_IMAGE_URL not specified. Automatically uploading {media_type} to temporary public HTTPS host...")
        upload_type = "video" if media_type == "REELS" else "image"
        public_media_url = image_processor.upload_temp_media(output_media_path, media_type=upload_type)

    logger.info(f"Posting {media_type} to Instagram using public URL: {public_media_url}")
    media_id = instagram_poster.post_to_instagram_graph_api(
        media_url=public_media_url,
        caption=artwork["caption"],
        account_id=account_id,
        access_token=access_token,
        alt_text=artwork.get("alt_text"),
        media_type=media_type
    )

    # 5. Record to history
    history_tracker.save_posted_artwork(artwork, media_id=media_id)
    logger.info("🎉 Post completed and recorded to history successfully!")

    # 6. Post to Pinterest (Images only)
    if media_type == "IMAGE":
        logger.info("Triggering Pinterest cross-post...")
        # Clean title for Pinterest
        title = f"{artwork['title']} by {artwork['artist']}"
        desc = artwork['caption']
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
            config.OUTPUT_VIDEO_PATH,
            config.OUTPUT_RAW_IMAGE_PATH,
            config.TEMP_AUDIO_PATH
        ]
        for f in temp_files:
            if os.path.exists(f):
                try:
                    os.remove(f)
                    logger.info(f"Deleted {f}")
                except Exception as e:
                    logger.warning(f"Could not delete {f}: {e}")
