from typing import Optional
import time
import requests
import logging

import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def post_to_instagram_graph_api(media_url: str, caption: str, account_id: str, access_token: str,
                                alt_text: Optional[str] = None, media_type: str = "IMAGE") -> str:
    """
    Publishes a post (IMAGE or REELS) to Instagram using the official Graph API.
    Step 1: POST /{ig-user-id}/media (Create Container)
    Step 2: POST /{ig-user-id}/media_publish (Publish Container)
    """
    if not account_id or not access_token:
        raise ValueError("INSTAGRAM_ACCOUNT_ID and INSTAGRAM_ACCESS_TOKEN must be provided.")

    container_url = f"{config.GRAPH_API_BASE_URL}/{account_id}/media"
    payload = {
        "caption": caption,
        "access_token": access_token
    }

    if media_type == "REELS":
        payload["video_url"] = media_url
        payload["media_type"] = "REELS"
    else:
        payload["image_url"] = media_url

    if alt_text and media_type != "REELS":
        payload["alt_text_custom"] = alt_text
        logger.info(f"Setting custom Alt Text for SEO: '{alt_text}'")

    logger.info(f"Creating Instagram {media_type} media container...")
    res = requests.post(container_url, data=payload, timeout=30)
    res_data = res.json()

    if res.status_code != 200 or "id" not in res_data:
        error_msg = res_data.get("error", {}).get("message", res.text)
        raise RuntimeError(f"Failed to create media container: {error_msg}")

    container_id = res_data["id"]
    logger.info(f"Media container created successfully. Container ID: {container_id}")

    # Wait for Instagram to finish processing media container
    # Reels usually take longer than images to process
    wait_time = 30 if media_type == "REELS" else 10
    logger.info(f"Waiting {wait_time} seconds for Instagram to process the {media_type} container...")
    time.sleep(wait_time)

    # Publish Container
    publish_url = f"{config.GRAPH_API_BASE_URL}/{account_id}/media_publish"
    publish_payload = {
        "creation_id": container_id,
        "access_token": access_token
    }

    logger.info(f"Publishing {media_type} container to Instagram...")
    pub_res = requests.post(publish_url, data=publish_payload, timeout=30)
    pub_data = pub_res.json()

    if pub_res.status_code != 200 or "id" not in pub_data:
        error_msg = pub_data.get("error", {}).get("message", pub_res.text)
        raise RuntimeError(f"Failed to publish container: {error_msg}")

    media_id = pub_data["id"]
    logger.info(f"Successfully published {media_type} post to Instagram! Media ID: {media_id}")
    return media_id


def post_story_to_instagram_graph_api(image_url: str, account_id: str, access_token: str) -> str:
    """
    Publishes an image Story to Instagram using the official Graph API.
    Step 1: POST /{ig-user-id}/media with media_type="STORIES"
    Step 2: POST /{ig-user-id}/media_publish (Publish Container)
    """
    if not account_id or not access_token:
        raise ValueError("INSTAGRAM_ACCOUNT_ID and INSTAGRAM_ACCESS_TOKEN must be provided.")

    container_url = f"{config.GRAPH_API_BASE_URL}/{account_id}/media"
    payload = {
        "image_url": image_url,
        "media_type": "STORIES",
        "access_token": access_token
    }

    logger.info("Creating Instagram Story media container...")
    res = requests.post(container_url, data=payload, timeout=30)
    res_data = res.json()

    if res.status_code != 200 or "id" not in res_data:
        error_msg = res_data.get("error", {}).get("message", res.text)
        raise RuntimeError(f"Failed to create Story container: {error_msg}")

    container_id = res_data["id"]
    logger.info(f"Story container created successfully. Container ID: {container_id}")

    # Wait for Instagram to finish processing image container
    time.sleep(10)

    # Publish Container
    publish_url = f"{config.GRAPH_API_BASE_URL}/{account_id}/media_publish"
    publish_payload = {
        "creation_id": container_id,
        "access_token": access_token
    }

    logger.info("Publishing Story container to Instagram...")
    pub_res = requests.post(publish_url, data=publish_payload, timeout=30)
    pub_data = pub_res.json()

    if pub_res.status_code != 200 or "id" not in pub_data:
        error_msg = pub_data.get("error", {}).get("message", pub_res.text)
        raise RuntimeError(f"Failed to publish Story container: {error_msg}")

    story_id = pub_data["id"]
    logger.info(f"Successfully published Story to Instagram! Story ID: {story_id}")
    return story_id
