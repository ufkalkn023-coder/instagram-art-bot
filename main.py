import argparse
import os
import sys
import logging

import config
from src import art_fetcher, image_processor, instagram_poster, history_tracker

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

    # 3. Process feed image (1080x1350, Instagram 4:5 portrait)
    logger.info("Processing feed artwork image with random frame style...")
    output_image_path = image_processor.process_artwork_image(artwork["image_url"])
    logger.info(f"Feed image successfully processed: {output_image_path}")

    # 4. Post to Instagram or Dry-Run
    if args.dry_run:
        logger.info("[DRY-RUN MODE] Skipping actual Instagram post upload.")
        history_tracker.save_posted_artwork(artwork, media_id="dry_run_id")
        logger.info("✅ Dry-run completed successfully!")
        return

    # Production Mode - Post to Graph API
    account_id = os.environ.get("INSTAGRAM_ACCOUNT_ID")
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    public_image_url = args.image_url or os.environ.get("PUBLIC_IMAGE_URL")

    logger.info(f"Checking environment variables: INSTAGRAM_ACCOUNT_ID={'[SET]' if account_id else '[MISSING]'}, INSTAGRAM_ACCESS_TOKEN={'[SET]' if access_token else '[MISSING]'}")

    if not account_id or not access_token:
        logger.error("❌ ERROR: Missing INSTAGRAM_ACCOUNT_ID or INSTAGRAM_ACCESS_TOKEN secrets! Please add them to your GitHub Repository Settings -> Secrets and variables -> Actions.")
        sys.exit(1)

    if not public_image_url:
        logger.info("PUBLIC_IMAGE_URL not specified. Automatically uploading feed image to temporary public HTTPS host...")
        public_image_url = image_processor.upload_temp_image(output_image_path)

    logger.info(f"Posting Feed post to Instagram using public image URL: {public_image_url}")
    media_id = instagram_poster.post_to_instagram_graph_api(
        image_url=public_image_url,
        caption=artwork["caption"],
        account_id=account_id,
        access_token=access_token,
        alt_text=artwork.get("alt_text")
    )

    # 5. Record to history
    history_tracker.save_posted_artwork(artwork, media_id=media_id)
    logger.info("🎉 Post completed and recorded to history successfully!")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"❌ Bot failed with error: {e}")
        sys.exit(1)
