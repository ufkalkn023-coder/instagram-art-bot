import os
import logging
import requests
import base64

logger = logging.getLogger(__name__)

def get_access_token(app_id: str, app_secret: str, refresh_token: str) -> str:
    """Generates a fresh access token using the 1-year refresh token."""
    logger.info("Fetching fresh Pinterest access token using refresh token...")
    auth_str = base64.b64encode(f"{app_id}:{app_secret}".encode('utf-8')).decode('utf-8')
    headers = {
        "Authorization": f"Basic {auth_str}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }
    
    res = requests.post("https://api.pinterest.com/v5/oauth/token", headers=headers, data=data, timeout=30)
    
    if res.status_code != 200:
        logger.error(f"Failed to refresh Pinterest token: {res.text}")
        res.raise_for_status()
        
    return res.json().get("access_token")


def post_to_pinterest(image_url: str, title: str, description: str, link: str = None) -> bool:
    """
    Posts an image to Pinterest using API v5.
    Returns True if successful, False otherwise.
    """
    app_id = os.environ.get("PINTEREST_APP_ID", "").strip()
    app_secret = os.environ.get("PINTEREST_APP_SECRET", "").strip()
    refresh_token = os.environ.get("PINTEREST_REFRESH_TOKEN", "").strip()
    board_id = os.environ.get("PINTEREST_BOARD_ID", "").strip()
    
    if not all([app_id, app_secret, refresh_token, board_id]):
        logger.info("Pinterest credentials not fully configured. Skipping Pinterest upload.")
        return False
        
    try:
        # 1. Get fresh access token
        access_token = get_access_token(app_id, app_secret, refresh_token)
        
        # 2. Create the Pin
        logger.info(f"Creating Pinterest Pin on board {board_id}...")
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        # Trim title and description to Pinterest limits (Title: 100 max, Desc: 500 max)
        clean_title = title[:100]
        clean_desc = description[:500]
        
        payload = {
            "board_id": board_id,
            "title": clean_title,
            "description": clean_desc,
            "media_source": {
                "source_type": "image_url",
                "url": image_url
            }
        }
        
        if link:
            payload["link"] = link
            
        res = requests.post("https://api.pinterest.com/v5/pins", headers=headers, json=payload, timeout=60)
        
        if res.status_code in [201, 200]:
            pin_data = res.json()
            pin_id = pin_data.get("id")
            logger.info(f"Successfully posted to Pinterest! Pin ID: {pin_id}")
            return True
        else:
            logger.error(f"Failed to create Pin. Status: {res.status_code}, Response: {res.text}")
            return False
            
    except Exception as e:
        logger.error(f"Pinterest posting exception: {e}")
        return False
