from __future__ import annotations

from botocore.exceptions import ClientError, ConnectionClosedError
import re
import pytest

from src import r2_media


PUBLICATION_A = "publication-a"
PUBLICATION_B = "publication-b"


def _reel_key(publication_id=PUBLICATION_A, nonce="a"):
    return (
        f"reels/publications/{publication_id}/"
        f"20260911120000_{nonce * 32}.mp4"
    )


def _reel_upload(publication_id=PUBLICATION_A, nonce="a"):
    key = _reel_key(publication_id, nonce)
    return r2_media.TempReelUpload(
        key,
        f"https://media.example/{key}",
        publication_id,
    )


def _configure(monkeypatch):
    for name in (
        "CLOUDFLARE_R2_ACCOUNT_ID",
        "CLOUDFLARE_R2_ACCESS_KEY_ID",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
        "CLOUDFLARE_R2_BUCKET_NAME",
    ):
        monkeypatch.setenv(name, "configured")
    monkeypatch.setenv("CLOUDFLARE_R2_PUBLIC_URL", "https://media.example")
    monkeypatch.setattr(r2_media.time, "sleep", lambda _seconds: None)


def _client_error(code, status, operation="DeleteObject"):
    return ClientError(
        {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


def test_stage_reel_mp4_uses_reel_owned_key_video_content_type_and_public_handle(
    monkeypatch, tmp_path
):
    _configure(monkeypatch)
    source = tmp_path / "reel.mp4"
    source.write_bytes(b"not-decoded-by-r2")
    uploads = []

    class Client:
        def put_object(self, **kwargs):
            uploads.append(
                {
                    "Bucket": kwargs["Bucket"],
                    "Key": kwargs["Key"],
                    "ContentType": kwargs["ContentType"],
                    "body": kwargs["Body"].read(),
                }
            )

    class Healthy:
        status_code = 200
        headers = {"Content-Type": "video/mp4; charset=binary", "Content-Length": "42"}

    monkeypatch.setattr(r2_media.boto3, "client", lambda *args, **kwargs: Client())
    monkeypatch.setattr(r2_media.requests, "head", lambda *args, **kwargs: Healthy())
    monkeypatch.setattr(r2_media.uuid, "uuid4", lambda: type("Id", (), {"hex": "a" * 32})())

    upload = r2_media.stage_reel_mp4(str(source), PUBLICATION_A)

    assert upload.publication_id == PUBLICATION_A
    assert re.fullmatch(
        r"reels/publications/publication-a/[0-9]{14}_" + "a" * 32 + r"\.mp4",
        upload.object_key,
    )
    assert upload.public_url == f"https://media.example/{upload.object_key}"
    assert len(uploads) == 1
    assert uploads[0]["Bucket"] == "configured"
    assert uploads[0]["Key"] == upload.object_key
    assert uploads[0]["ContentType"] == "video/mp4"
    assert uploads[0]["body"] == b"not-decoded-by-r2"


def test_stage_reel_mp4_uploads_via_put_object_with_dedicated_reel_client(
    monkeypatch, tmp_path
):
    _configure(monkeypatch)
    source = tmp_path / "reel.mp4"
    source.write_bytes(b"not-decoded-by-r2")
    client_configs = []
    puts = []
    upload_file_calls = []

    class Client:
        def put_object(self, **kwargs):
            puts.append(
                {
                    "bucket": kwargs["Bucket"],
                    "key": kwargs["Key"],
                    "content_type": kwargs["ContentType"],
                    "body": kwargs["Body"].read(),
                }
            )
            if len(puts) == 1:
                # Consume the stream, then fail like the real staging failure:
                # the retry must reopen the MP4 from a fresh stream position.
                raise ConnectionClosedError(endpoint_url="https://r2.example")

        def upload_file(self, *args, **kwargs):
            upload_file_calls.append((args, kwargs))

    class Healthy:
        status_code = 200
        headers = {"Content-Type": "video/mp4; charset=binary", "Content-Length": "42"}

    def fake_client(*args, **kwargs):
        client_configs.append(kwargs.get("config"))
        return Client()

    monkeypatch.setattr(r2_media.boto3, "client", fake_client)
    monkeypatch.setattr(r2_media.requests, "head", lambda *args, **kwargs: Healthy())

    upload = r2_media.stage_reel_mp4(str(source), PUBLICATION_A)

    assert upload.publication_id == PUBLICATION_A
    assert re.fullmatch(
        r"reels/publications/publication-a/[0-9]{14}_[0-9a-f]{32}\.mp4",
        upload.object_key,
    )
    assert puts, "Reel staging must upload with put_object, not managed transfer"
    assert not upload_file_calls
    assert len(puts) == 2
    assert [call["bucket"] for call in puts] == ["configured", "configured"]
    assert [call["key"] for call in puts] == [upload.object_key, upload.object_key]
    assert [call["content_type"] for call in puts] == ["video/mp4", "video/mp4"]
    assert [call["body"] for call in puts] == [
        b"not-decoded-by-r2",
        b"not-decoded-by-r2",
    ]

    assert len(client_configs) == 1
    staging_config = client_configs[0]
    assert staging_config.connect_timeout == 10
    assert staging_config.read_timeout == 120
    assert staging_config.request_checksum_calculation == "when_required"
    assert staging_config.retries == {"total_max_attempts": 1, "mode": "standard"}

    client_configs.clear()
    r2_media._get_s3_client(r2_media._load_configuration(require_public_url=False))
    assert len(client_configs) == 1
    assert client_configs[0] is r2_media.R2_CLIENT_CONFIG
    assert r2_media.R2_CLIENT_CONFIG.connect_timeout == 10
    assert r2_media.R2_CLIENT_CONFIG.read_timeout == 30
    assert r2_media.R2_CLIENT_CONFIG.retries == {
        "total_max_attempts": 1,
        "mode": "standard",
    }


@pytest.mark.parametrize(
    "publication_id, object_key",
    (
        ("UPPERCASE", _reel_key()),
        (PUBLICATION_A, "images/publications/publication-a/20260911120000_" + "a" * 32 + ".jpg"),
        (PUBLICATION_A, "reels/publications/publication-a/20260911120000_" + "a" * 32 + ".mov"),
        (PUBLICATION_A, _reel_key(PUBLICATION_B)),
    ),
)
def test_reel_key_guard_fails_closed_for_invalid_owner_namespace_suffix_or_publication(
    publication_id, object_key
):
    with pytest.raises(ValueError):
        r2_media.validate_owned_reel_object_key(object_key, publication_id)


def test_stage_reel_mp4_rejects_non_mp4_source_before_upload(tmp_path):
    with pytest.raises(ValueError, match="MP4"):
        r2_media.stage_reel_mp4(str(tmp_path / "reel.mov"), PUBLICATION_A)


@pytest.mark.parametrize(
    "status, content_type, content_length",
    (
        (201, "video/mp4", "42"),
        (200, "video/webm", "42"),
        (200, "video/mp4", "0"),
    ),
)
def test_reel_public_health_check_rejects_non_200_empty_or_non_mp4_response(
    monkeypatch, tmp_path, status, content_type, content_length
):
    _configure(monkeypatch)
    source = tmp_path / "reel.mp4"
    source.write_bytes(b"not-decoded-by-r2")
    deleted = []

    class Client:
        def put_object(self, **kwargs):
            pass

        def delete_object(self, **kwargs):
            deleted.append(kwargs["Key"])

    class Unhealthy:
        status_code = status
        headers = {"Content-Type": content_type, "Content-Length": content_length}

    monkeypatch.setattr(r2_media.boto3, "client", lambda *args, **kwargs: Client())
    monkeypatch.setattr(r2_media.requests, "head", lambda *args, **kwargs: Unhealthy())

    with pytest.raises(RuntimeError, match="public health check failed"):
        r2_media.stage_reel_mp4(str(source), PUBLICATION_A)

    assert len(deleted) == 1
    assert deleted[0].startswith("reels/publications/publication-a/")


def test_exact_reel_cleanup_rejects_image_handle_and_deletes_one_reel_object(
    monkeypatch,
):
    _configure(monkeypatch)
    deleted = []

    class Client:
        def delete_object(self, **kwargs):
            deleted.append(kwargs["Key"])

    monkeypatch.setattr(r2_media.boto3, "client", lambda *args, **kwargs: Client())

    assert r2_media.cleanup_temp_reel_upload(_reel_upload(), reason="test")
    assert deleted == [_reel_key()]

    image_upload = r2_media.TempMediaUpload(
        "images/publications/publication-a/20260911120000_" + "a" * 32 + ".jpg",
        "https://media.example/image.jpg",
        PUBLICATION_A,
    )
    with pytest.raises(ValueError, match="Reel"):
        r2_media.cleanup_temp_reel_upload(image_upload, reason="test")


def test_reel_prefix_cleanup_lists_and_deletes_only_reel_owned_objects(monkeypatch):
    _configure(monkeypatch)
    listings = []
    deleted = []

    class Client:
        def list_objects_v2(self, **kwargs):
            listings.append(kwargs)
            return {"Contents": [{"Key": _reel_key()}], "IsTruncated": False}

        def delete_object(self, **kwargs):
            deleted.append(kwargs["Key"])

    monkeypatch.setattr(r2_media.boto3, "client", lambda *args, **kwargs: Client())

    summary = r2_media.cleanup_publication_reels(PUBLICATION_A, reason="expired")

    assert summary.complete
    assert deleted == [_reel_key()]
    assert {request["Prefix"] for request in listings} == {
        "reels/publications/publication-a/"
    }


@pytest.mark.parametrize(
    "unexpected_key",
    (
        "images/publications/publication-a/20260911120000_" + "a" * 32 + ".jpg",
        _reel_key(PUBLICATION_B),
    ),
)
def test_reel_prefix_cleanup_fails_closed_for_image_or_cross_publication_key(
    monkeypatch, unexpected_key
):
    _configure(monkeypatch)
    deleted = []

    class Client:
        def list_objects_v2(self, **kwargs):
            return {
                "Contents": [
                    {"Key": unexpected_key}
                ],
                "IsTruncated": False,
            }

        def delete_object(self, **kwargs):
            deleted.append(kwargs["Key"])

    monkeypatch.setattr(r2_media.boto3, "client", lambda *args, **kwargs: Client())

    assert not r2_media.cleanup_publication_reels(
        PUBLICATION_A, reason="expired"
    ).complete
    assert deleted == []
