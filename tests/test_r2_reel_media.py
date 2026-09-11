from __future__ import annotations

from botocore.exceptions import ClientError
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
        def upload_file(self, file_path, bucket, object_key, ExtraArgs):
            uploads.append((file_path, bucket, object_key, ExtraArgs))

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
    assert uploads == [
        (str(source), "configured", upload.object_key, {"ContentType": "video/mp4"})
    ]


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
        def upload_file(self, *args, **kwargs):
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
