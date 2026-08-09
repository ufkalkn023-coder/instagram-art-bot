import pytest
import json
import os
import io
from unittest.mock import patch, MagicMock, mock_open
from botocore.exceptions import ClientError
from src import history_tracker, image_processor
from src.history_tracker import ConcurrentWriteError, CorruptedHistoryError

# ---------------------------------------------------------
# Tests for history_tracker.py
# ---------------------------------------------------------

@pytest.fixture
def mock_s3_env():
    with patch.dict(os.environ, {
        "CLOUDFLARE_R2_ACCOUNT_ID": "test",
        "CLOUDFLARE_R2_ACCESS_KEY_ID": "test",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY": "test",
        "CLOUDFLARE_R2_BUCKET_NAME": "test"
    }):
        yield

def test_malformed_r2_history(mock_s3_env):
    """Test that malformed JSON in R2 raises CorruptedHistoryError and does NOT overwrite."""
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {
        'Body': MagicMock(read=MagicMock(return_value=b"{ invalid_json }")),
        'ETag': '"etag123"'
    }
    
    with patch('src.history_tracker._get_s3_client', return_value=mock_s3):
        with pytest.raises(CorruptedHistoryError):
            history_tracker.load_history_with_etag()
        
        # Ensure that during reserve_artwork, the exception propagates and put_object is NEVER called.
        with pytest.raises(CorruptedHistoryError):
            history_tracker.reserve_artwork({"id": "123"})
            
        mock_s3.put_object.assert_not_called()

def test_empty_r2_history(mock_s3_env):
    """Test that NoSuchKey creates a fresh empty list safely."""
    mock_s3 = MagicMock()
    error_response = {'Error': {'Code': 'NoSuchKey'}}
    mock_s3.get_object.side_effect = ClientError(error_response, 'GetObject')
    
    with patch('src.history_tracker._get_s3_client', return_value=mock_s3):
        data, etag = history_tracker.load_history_with_etag()
        assert data == {"posted_artworks": []}
        assert etag is None

def test_r2_get_failure(mock_s3_env):
    """Test that generic R2 GET failures crash the bot."""
    mock_s3 = MagicMock()
    error_response = {'Error': {'Code': 'AccessDenied'}}
    mock_s3.get_object.side_effect = ClientError(error_response, 'GetObject')
    
    with patch('src.history_tracker._get_s3_client', return_value=mock_s3):
        with pytest.raises(ClientError):
            history_tracker.load_history_with_etag()

def test_r2_put_failure_reservation(mock_s3_env):
    """Test that generic PUT failures crash the bot during reservation."""
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {
        'Body': MagicMock(read=MagicMock(return_value=b'{"posted_artworks": []}')),
        'ETag': '"etag123"'
    }
    error_response = {'Error': {'Code': 'InternalError'}}
    mock_s3.put_object.side_effect = ClientError(error_response, 'PutObject')
    
    with patch('src.history_tracker._get_s3_client', return_value=mock_s3):
        with pytest.raises(ClientError):
            history_tracker.reserve_artwork({"id": "123"})

def test_concurrent_history_update(mock_s3_env):
    """Test that 412 Precondition Failed (ETag mismatch) raises ConcurrentWriteError."""
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {
        'Body': MagicMock(read=MagicMock(return_value=b'{"posted_artworks": []}')),
        'ETag': '"etag123"'
    }
    
    # Simulate conditional write failure
    error_response = {'Error': {'Code': '412'}}
    mock_s3.put_object.side_effect = ClientError(error_response, 'PutObject')
    
    with patch('src.history_tracker._get_s3_client', return_value=mock_s3):
        with pytest.raises(ConcurrentWriteError):
            history_tracker.reserve_artwork({"id": "123"})
            
        # Ensure it was called with IfMatch
        args, kwargs = mock_s3.put_object.call_args
        assert kwargs["IfMatch"] == "etag123"

def test_pending_and_published_records(mock_s3_env):
    """Test that reserved records have PENDING, reserved_at and UUID."""
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {
        'Body': MagicMock(read=MagicMock(return_value=b'{"posted_artworks": []}')),
        'ETag': '"etag123"'
    }
    
    with patch('src.history_tracker._get_s3_client', return_value=mock_s3):
        history_tracker.reserve_artwork({"id": "123", "title": "Art"})
        
        # Verify uploaded JSON structure
        args, kwargs = mock_s3.put_object.call_args
        uploaded_json = json.loads(kwargs["Body"].decode('utf-8'))
        
        record = uploaded_json["posted_artworks"][0]
        assert record["status"] == "PENDING"
        assert "reserved_at" in record
        assert "reservation_id" in record
        assert "Z" in record["reserved_at"] # Verify UTC
        
        # Verify Confirm updates to PUBLISHED
        mock_s3.get_object.return_value = {
            'Body': MagicMock(read=MagicMock(return_value=kwargs["Body"])),
            'ETag': '"etag456"'
        }
        history_tracker.confirm_artwork("123", "media123")
        args, kwargs = mock_s3.put_object.call_args
        uploaded_json = json.loads(kwargs["Body"].decode('utf-8'))
        
        record = uploaded_json["posted_artworks"][0]
        assert record["status"] == "PUBLISHED"
        assert record["media_id"] == "media123"
        assert "posted_at" in record
        assert "Z" in record["posted_at"] # Verify UTC

# ---------------------------------------------------------
# Tests for image_processor.py (Music Resilience)
# ---------------------------------------------------------

@patch("src.image_processor.requests.get")
def test_429_music_response_retry(mock_get):
    """Test that HTTP 429 triggers a retry, and eventually succeeds or fails."""
    # First call returns 429, second call returns 200
    mock_429 = MagicMock()
    mock_429.status_code = 429
    mock_429.headers = {"Retry-After": "1"}
    
    mock_200 = MagicMock()
    mock_200.status_code = 200
    mock_200.headers = {"Content-Type": "audio/ogg"}
    mock_200.content = b"fakeaudio"
    
    mock_get.side_effect = [mock_429, mock_200]
    
    with patch("src.image_processor.time.sleep") as mock_sleep:
        with patch("src.image_processor.mpy.AudioFileClip") as mock_clip:
            mock_clip.return_value.duration = 10
            with patch("builtins.open", mock_open()):
                path, track = image_processor.download_audio()
                assert path is not None
                assert mock_sleep.call_count == 1
                mock_sleep.assert_called_with(1)

@patch("src.image_processor.requests.get")
def test_all_audio_failure(mock_get):
    """Test that if all audio fails, it raises RuntimeError."""
    mock_fail = MagicMock()
    mock_fail.raise_for_status.side_effect = Exception("Failed")
    mock_get.return_value = mock_fail
    
    with pytest.raises(RuntimeError, match="All audio sources failed"):
        image_processor.download_audio()

@patch("src.image_processor.requests.get")
def test_invalid_audio_response(mock_get):
    """Test that if content-type is invalid or duration <=0, it skips track and tries next."""
    mock_invalid = MagicMock()
    mock_invalid.status_code = 200
    mock_invalid.headers = {"Content-Type": "text/html"} # Invalid!
    mock_invalid.content = b"<html></html>"
    
    mock_get.return_value = mock_invalid
    
    with pytest.raises(RuntimeError, match="All audio sources failed"):
        image_processor.download_audio()
