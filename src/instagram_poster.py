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
        error_info = res_data.get("error", {})
        error_msg = error_info.get("message", res.text)
        error_code = error_info.get("code")
        
        if error_code == 190:
            raise RuntimeError(f"❌ Instagram Access Token is expired or invalid (Code 190). Please run get_long_lived_token.py to generate a new permanent token. Detail: {error_msg}")
            
        raise RuntimeError(f"Failed to create media container: {error_msg}")

    container_id = res_data["id"]
    logger.info(f"Media container created successfully. Container ID: {container_id}")

    # Wait for Instagram to finish processing media container
    status_url = f"{config.GRAPH_API_BASE_URL}/{container_id}?fields=status_code&access_token={access_token}"
    import time
    for _ in range(12): # Poll for up to 60 seconds
        time.sleep(5)
        try:
            status_res = requests.get(status_url, timeout=10)
            if status_res.status_code == 200:
                status = status_res.json().get("status_code", "")
                if status == "FINISHED":
                    break
                elif status == "ERROR":
                    logger.error("Instagram processing failed for container.")
                    break
        except Exception as e:
            logger.warning(f"Error checking container status: {e}")
            pass
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
    status_url = f"{config.GRAPH_API_BASE_URL}/{container_id}?fields=status_code&access_token={access_token}"
    import time
    for _ in range(6): # Poll for up to 30 seconds for stories
        time.sleep(5)
        try:
            status_res = requests.get(status_url, timeout=10)
            if status_res.status_code == 200:
                status = status_res.json().get("status_code", "")
                if status == "FINISHED":
                    break
                elif status == "ERROR":
                    break
        except Exception:
            pass

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

def get_instagram_permalink(media_id: str, access_token: str) -> str:
    """
    Fetches the direct permalink (URL) for an Instagram post using its media ID.
    Returns the URL string (e.g., https://www.instagram.com/p/CODE/), or None if failed.
    """
    if not media_id or not access_token:
        return None
        
    url = f"{config.GRAPH_API_BASE_URL}/{media_id}"
    params = {
        "fields": "permalink",
        "access_token": access_token
    }
    
    try:
        res = requests.get(url, params=params, timeout=10)
        if res.status_code == 200:
            data = res.json()
            return data.get("permalink")
        else:
            logger.warning(f"Failed to fetch permalink for media {media_id}. Status: {res.status_code}")
            return None
    except Exception as e:
        logger.error(f"Exception while fetching permalink: {e}")
        return None

def post_carousel_to_instagram_graph_api(media_urls: list, caption: str, account_id: str, access_token: str) -> str:
    """
    Publishes a carousel (multiple images) to Instagram using Graph API.
    """
    if not account_id or not access_token:
        raise ValueError("INSTAGRAM_ACCOUNT_ID and INSTAGRAM_ACCESS_TOKEN must be provided.")
        
    import time
    
    # 1. Create Item Containers
    item_container_ids = []
    for i, url in enumerate(media_urls):
        container_url = f"{config.GRAPH_API_BASE_URL}/{account_id}/media"
        payload = {
            "image_url": url,
            "is_carousel_item": "true",
            "access_token": access_token
        }
        logger.info(f"Creating carousel item {i+1}/{len(media_urls)}...")
        res = requests.post(container_url, data=payload, timeout=30)
        res_data = res.json()
        
        if res.status_code != 200 or "id" not in res_data:
            error_msg = res_data.get("error", {}).get("message", res.text)
            raise RuntimeError(f"Failed to create carousel item {i+1}: {error_msg}")
            
        item_id = res_data["id"]
        item_container_ids.append(item_id)
        
    # Wait for all item containers to be finished
    logger.info("Waiting for all carousel items to be processed...")
    for item_id in item_container_ids:
        status_url = f"{config.GRAPH_API_BASE_URL}/{item_id}?fields=status_code&access_token={access_token}"
        for _ in range(12):
            try:
                status_res = requests.get(status_url, timeout=10)
                if status_res.status_code == 200:
                    status = status_res.json().get("status_code", "")
                    if status == "FINISHED":
                        break
            except Exception:
                pass
            time.sleep(5)
            
    # 2. Create Carousel Container
    carousel_url = f"{config.GRAPH_API_BASE_URL}/{account_id}/media"
    carousel_payload = {
        "media_type": "CAROUSEL",
        "children": ",".join(item_container_ids),
        "caption": caption,
        "access_token": access_token
    }
    
    logger.info("Creating Carousel Container...")
    c_res = requests.post(carousel_url, data=carousel_payload, timeout=30)
    c_res_data = c_res.json()
    
    if c_res.status_code != 200 or "id" not in c_res_data:
        error_msg = c_res_data.get("error", {}).get("message", c_res.text)
        raise RuntimeError(f"Failed to create carousel container: {error_msg}")
        
    carousel_id = c_res_data["id"]
    
    # Wait for Carousel Container
    status_url = f"{config.GRAPH_API_BASE_URL}/{carousel_id}?fields=status_code&access_token={access_token}"
    for _ in range(12):
        time.sleep(5)
        try:
            status_res = requests.get(status_url, timeout=10)
            if status_res.status_code == 200:
                if status_res.json().get("status_code", "") == "FINISHED":
                    break
        except Exception:
            pass
            
    # 3. Publish Carousel Container
    publish_url = f"{config.GRAPH_API_BASE_URL}/{account_id}/media_publish"
    publish_payload = {
        "creation_id": carousel_id,
        "access_token": access_token
    }
    logger.info("Publishing Carousel to Instagram...")
    pub_res = requests.post(publish_url, data=publish_payload, timeout=30)
    pub_data = pub_res.json()
    
    if pub_res.status_code != 200 or "id" not in pub_data:
        error_msg = pub_data.get("error", {}).get("message", pub_res.text)
        raise RuntimeError(f"Failed to publish carousel: {error_msg}")
        
    logger.info(f"Successfully published Carousel! Media ID: {pub_data['id']}")
    return pub_data["id"]
