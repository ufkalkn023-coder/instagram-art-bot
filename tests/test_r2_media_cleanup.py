from __future__ import annotations

from botocore.exceptions import ClientError
from PIL import Image
import pytest

from src import image_processor, r2_media


PUBLICATION_A = "publication-a"
PUBLICATION_B = "publication-b"


def _key(publication_id=PUBLICATION_A, nonce="a", suffix=".jpg"):
    return (
        f"images/publications/{publication_id}/"
        f"20260826120000_{nonce * 32}{suffix}"
    )


def _upload(publication_id=PUBLICATION_A, nonce="a"):
    key = _key(publication_id, nonce)
    return r2_media.TempMediaUpload(
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


@pytest.mark.parametrize(
    "publication_id",
    (
        "",
        " ",
        "UPPERCASE",
        "../posted_history",
        "publication/a",
        "publication.a",
        "-leading",
        "trailing-",
    ),
)
def test_publication_id_guard_rejects_non_normalized_ownership(publication_id):
    with pytest.raises(ValueError, match="Publication ID"):
        r2_media.publication_media_prefix(publication_id)


@pytest.mark.parametrize(
    "object_key",
    (
        "posted_history.json",
        "images/foo.jpg",
        "../posted_history.json",
        "images/publications/",
        "images/publications//foo.jpg",
        "arbitrary-user-prefix/file.jpg",
        "images/publications/publication-a/not-generated.jpg",
        "images/publications/publication-a/20260826120000_short.jpg",
        "images/publications/publication-a/99999999999999_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg",
        _key(PUBLICATION_B),
    ),
)
def test_exact_key_guard_rejects_unowned_and_cross_publication_keys(object_key):
    with pytest.raises(ValueError, match="Refusing"):
        r2_media.validate_owned_object_key(object_key, PUBLICATION_A)


def test_health_check_failure_deletes_only_the_exact_uploaded_object(
    monkeypatch, tmp_path
):
    _configure(monkeypatch)
    source = tmp_path / "source.jpg"
    Image.new("RGB", (20, 20), (20, 40, 60)).save(source, "JPEG")
    uploaded = []
    deleted = []

    class Client:
        def upload_file(self, file_path, bucket, object_key, ExtraArgs):
            uploaded.append(object_key)

        def delete_object(self, **kwargs):
            deleted.append(kwargs["Key"])

    class Unhealthy:
        status_code = 503
        headers = {}

    monkeypatch.setattr(r2_media.boto3, "client", lambda *args, **kwargs: Client())
    monkeypatch.setattr(r2_media.requests, "head", lambda *args, **kwargs: Unhealthy())

    with pytest.raises(RuntimeError, match="public health check failed"):
        image_processor.upload_temp_media(str(source), PUBLICATION_A)

    assert len(uploaded) == 1
    assert deleted == uploaded
    assert deleted[0].startswith(
        "images/publications/publication-a/"
    )


def test_upload_failure_before_known_object_creation_does_not_delete(monkeypatch):
    _configure(monkeypatch)
    delete_calls = []

    class Client:
        def upload_file(self, *args, **kwargs):
            raise _client_error("AccessDenied", 403, "PutObject")

        def delete_object(self, **kwargs):
            delete_calls.append(kwargs)

    monkeypatch.setattr(r2_media.boto3, "client", lambda *args, **kwargs: Client())

    with pytest.raises(RuntimeError, match="Failed to upload"):
        r2_media.stage_temp_media(
            "unused.jpg",
            publication_id=PUBLICATION_A,
            content_type="image/jpeg",
            file_suffix=".jpg",
        )

    assert delete_calls == []


def test_exact_delete_retries_transient_failures_and_treats_404_as_success(
    monkeypatch,
):
    _configure(monkeypatch)
    attempts = []

    class TransientClient:
        def delete_object(self, **kwargs):
            attempts.append(kwargs["Key"])
            if len(attempts) < 3:
                raise _client_error("ServiceUnavailable", 503)

    monkeypatch.setattr(
        r2_media.boto3, "client", lambda *args, **kwargs: TransientClient()
    )
    assert r2_media.cleanup_temp_media_upload(
        _upload(), reason="test"
    )
    assert attempts == [_key()] * 3

    class MissingClient:
        def delete_object(self, **kwargs):
            raise _client_error("NoSuchKey", 404)

    monkeypatch.setattr(
        r2_media.boto3, "client", lambda *args, **kwargs: MissingClient()
    )
    assert r2_media.cleanup_temp_media_upload(
        _upload(), reason="test"
    )


def test_exact_delete_does_not_retry_permanent_failure(monkeypatch):
    _configure(monkeypatch)
    attempts = []

    class Client:
        def delete_object(self, **kwargs):
            attempts.append(kwargs)
            raise _client_error("AccessDenied", 403)

    monkeypatch.setattr(r2_media.boto3, "client", lambda *args, **kwargs: Client())

    assert not r2_media.cleanup_temp_media_upload(
        _upload(), reason="test"
    )
    assert len(attempts) == 1


def test_publication_cleanup_uses_exact_prefix_and_bounded_pagination(monkeypatch):
    _configure(monkeypatch)
    listings = []
    deleted = []

    class Client:
        def list_objects_v2(self, **kwargs):
            listings.append(kwargs)
            if "ContinuationToken" not in kwargs:
                return {
                    "Contents": [{"Key": _key(nonce="a")}],
                    "IsTruncated": True,
                    "NextContinuationToken": "next",
                }
            return {
                "Contents": [{"Key": _key(nonce="b")}],
                "IsTruncated": False,
            }

        def delete_object(self, **kwargs):
            deleted.append(kwargs["Key"])

    monkeypatch.setattr(r2_media.boto3, "client", lambda *args, **kwargs: Client())

    summary = r2_media.cleanup_publication_media(
        PUBLICATION_A, reason="expired"
    )

    assert summary.complete
    assert summary.deleted == 2
    assert deleted == [_key(nonce="a"), _key(nonce="b")]
    assert {request["Prefix"] for request in listings} == {
        "images/publications/publication-a/"
    }
    assert all(
        request["MaxKeys"] <= r2_media.PUBLICATION_LIST_PAGE_SIZE
        for request in listings
    )


def test_unexpected_key_or_object_count_overflow_fails_before_any_delete(
    monkeypatch,
):
    _configure(monkeypatch)
    deleted = []

    class UnexpectedClient:
        def list_objects_v2(self, **kwargs):
            return {
                "Contents": [
                    {"Key": "images/publications/publication-a/unowned.jpg"}
                ],
                "IsTruncated": False,
            }

        def delete_object(self, **kwargs):
            deleted.append(kwargs)

    monkeypatch.setattr(
        r2_media.boto3, "client", lambda *args, **kwargs: UnexpectedClient()
    )
    assert not r2_media.cleanup_publication_media(
        PUBLICATION_A, reason="expired"
    ).complete
    assert deleted == []

    class OverflowClient(UnexpectedClient):
        def list_objects_v2(self, **kwargs):
            return {
                "Contents": [
                    {
                        "Key": (
                            "images/publications/publication-a/"
                            f"20260826120000_{index:032x}.jpg"
                        )
                    }
                    for index in range(r2_media.PUBLICATION_CLEANUP_MAX_OBJECTS + 1)
                ],
                "IsTruncated": False,
            }

    monkeypatch.setattr(
        r2_media.boto3, "client", lambda *args, **kwargs: OverflowClient()
    )
    assert not r2_media.cleanup_publication_media(
        PUBLICATION_A, reason="expired"
    ).complete
    assert deleted == []


def test_rollback_validates_every_handle_before_deleting_anything(monkeypatch):
    calls = []
    monkeypatch.setattr(
        r2_media,
        "cleanup_temp_media_upload",
        lambda upload, **kwargs: calls.append(upload.object_key) or True,
    )

    with pytest.raises(ValueError, match="cross-publication"):
        r2_media.rollback_temp_media_uploads(
            PUBLICATION_A,
            [_upload(PUBLICATION_A), _upload(PUBLICATION_B)],
        )

    assert calls == []
