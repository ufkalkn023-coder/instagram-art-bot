import os
import sys
import mimetypes
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load .env explicitly to ensure credentials are found
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(env_path)

def get_r2_client():
    account_id = os.environ.get("CLOUDFLARE_R2_ACCOUNT_ID")
    access_key = os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY")
    
    if not all([account_id, access_key, secret_key]):
        raise ValueError("Missing R2 credentials in .env")
        
    return boto3.client(
        service_name="s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto" # Cloudflare R2 requires region 'auto'
    )

def main():
    bucket_name = os.environ.get("CLOUDFLARE_R2_BUCKET_NAME")
    if not bucket_name:
        raise ValueError("Missing CLOUDFLARE_R2_BUCKET_NAME in .env")
        
    client = get_r2_client()
    
    audio_dir = os.path.join(os.path.dirname(__file__), "assets", "audio")
    if not os.path.isdir(audio_dir):
        logger.error(f"Directory not found: {audio_dir}")
        sys.exit(1)
        
    valid_exts = (".mp3", ".ogg", ".wav", ".m4a", ".aac", ".flac")
    files_to_upload = [f for f in os.listdir(audio_dir) if f.lower().endswith(valid_exts)]
    
    if not files_to_upload:
        logger.info("No audio files found to upload.")
        return
        
    logger.info(f"Found {len(files_to_upload)} audio files. Starting upload to bucket '{bucket_name}'...")
    
    success_count = 0
    skipped_count = 0
    error_count = 0
    
    for filename in files_to_upload:
        local_path = os.path.join(audio_dir, filename)
        object_key = f"audio/{filename}"
        
        # Check if exists
        try:
            client.head_object(Bucket=bucket_name, Key=object_key)
            logger.info(f"⏭️ Skipped (already exists): {object_key}")
            skipped_count += 1
            continue
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                pass # Proceed with upload
            else:
                logger.error(f"Error checking {object_key}: {e}")
                error_count += 1
                continue
                
        content_type, _ = mimetypes.guess_type(local_path)
        if not content_type:
            content_type = "audio/mpeg" # fallback
            
        logger.info(f"⬆️ Uploading: {object_key} ({os.path.getsize(local_path) / (1024*1024):.2f} MB)...")
        try:
            client.upload_file(
                Filename=local_path,
                Bucket=bucket_name,
                Key=object_key,
                ExtraArgs={"ContentType": content_type}
            )
            logger.info(f"✅ Success: {object_key}")
            success_count += 1
        except Exception as e:
            logger.error(f"❌ Failed to upload {object_key}: {e}")
            error_count += 1
            
    logger.info(f"Upload Summary: {success_count} uploaded, {skipped_count} skipped, {error_count} failed.")

if __name__ == "__main__":
    main()
