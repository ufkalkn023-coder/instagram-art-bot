from __future__ import annotations

from botocore.exceptions import ClientError, EndpointConnectionError
from PIL import Image
import pytest
import requests

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

        def close(self):
            pass

    monkeypatch.setattr(r2_media.boto3, "client", lambda *args, **kwargs: Client())
    monkeypatch.setattr(r2_media.requests, "head", lambda *args, **kwargs: Unhealthy())
    monkeypatch.setattr(r2_media.requests, "get", lambda *args, **kwargs: Unhealthy())

    with pytest.raises(RuntimeError, match="public health check failed"):
        image_processor.upload_temp_media(str(source), PUBLICATION_A)

    assert len(uploaded) == 1
    assert deleted == uploaded
    assert deleted[0].startswith(
        "images/publications/publication-a/"
    )


def test_upload_failure_before_known_object_creation_has_safe_diagnostics(monkeypatch):
    _configure(monkeypatch)
    delete_calls = []

    class Client:
        def upload_file(self, *args, **kwargs):
            raise _client_error("AccessDenied", 403, "PutObject")

        def delete_object(self, **kwargs):
            delete_calls.append(kwargs)

    monkeypatch.setattr(r2_media.boto3, "client", lambda *args, **kwargs: Client())

    with pytest.raises(RuntimeError) as caught:
        r2_media.stage_temp_media(
            "unused.jpg",
            publication_id=PUBLICATION_A,
            content_type="image/jpeg",
            file_suffix=".jpg",
        )

    assert delete_calls == []
    message = str(caught.value)
    assert "operation=UploadFile" in message
    assert "code=AccessDenied" in message
    assert "http_status=403" in message
    assert "bucket=configured" in message
    assert "key=images/publications/publication-a/" in message


def test_upload_client_initialization_failure_is_sanitized(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        r2_media,
        "_get_s3_client",
        lambda *_args: (_ for _ in ()).throw(
            EndpointConnectionError(endpoint_url="https://secret-must-not-appear")
        ),
    )

    with pytest.raises(RuntimeError) as caught:
        r2_media.stage_temp_media(
            "prepared.jpg",
            publication_id=PUBLICATION_A,
            content_type="image/jpeg",
            file_suffix=".jpg",
        )
    message = str(caught.value)
    assert "operation=CreateClient" in message
    assert "bucket=configured" in message
    assert "key=images/publications/publication-a/" in message
    assert "secret-must-not-appear" not in message


def test_successful_head_does_not_hide_unreadable_public_get(monkeypatch):
    _configure(monkeypatch)
    uploaded = []
    deleted = []

    class Client:
        def upload_file(self, _path, _bucket, key, ExtraArgs):
            uploaded.append(key)

        def delete_object(self, **kwargs):
            deleted.append(kwargs["Key"])

    class HeadResponse:
        status_code = 200
        headers = {"Content-Type": "image/jpeg", "Content-Length": "100"}

    class GetResponse:
        status_code = 403
        headers = {}
        url = "https://media.example/denied"

        def close(self):
            pass

    monkeypatch.setattr(r2_media.boto3, "client", lambda *args, **kwargs: Client())
    monkeypatch.setattr(r2_media.requests, "head", lambda *args, **kwargs: HeadResponse())
    monkeypatch.setattr(r2_media.requests, "get", lambda *args, **kwargs: GetResponse())

    with pytest.raises(RuntimeError, match="public health check failed"):
        r2_media.stage_temp_media(
            "prepared.jpg",
            publication_id=PUBLICATION_A,
            content_type="image/jpeg",
            file_suffix=".jpg",
        )

    assert deleted == uploaded


@pytest.mark.parametrize("head_status", [200, 403, 405, None])
def test_public_get_can_validate_media_when_head_metadata_is_missing_or_unsupported(
    monkeypatch, head_status
):
    _configure(monkeypatch)

    class Client:
        def upload_file(self, *_args, **_kwargs):
            pass

    class HeadResponse:
        status_code = head_status
        headers = {}

    class GetResponse:
        status_code = 200
        headers = {"Content-Type": "image/jpeg"}
        url = "https://media.example/image.jpg"

        def iter_content(self, chunk_size):
            assert chunk_size == 1
            yield b"\xff"

        def close(self):
            pass

    monkeypatch.setattr(r2_media.boto3, "client", lambda *args, **kwargs: Client())
    def head(*_args, **_kwargs):
        if head_status is None:
            raise requests.Timeout("HEAD unavailable")
        return HeadResponse()

    monkeypatch.setattr(r2_media.requests, "head", head)
    monkeypatch.setattr(r2_media.requests, "get", lambda *args, **kwargs: GetResponse())

    upload = r2_media.stage_temp_media(
        "prepared.jpg",
        publication_id=PUBLICATION_A,
        content_type="image/jpeg",
        file_suffix=".jpg",
    )

    assert upload.public_url.startswith("https://media.example/images/publications/")


def test_exact_delete_retries_transient_failures_and_treats_missing_key_as_success(
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


@pytest.mark.parametrize("code", ["NoSuchBucket", "NotFound", "404"])
def test_exact_delete_does_not_treat_ambiguous_404_as_absent_object(
    monkeypatch, caplog, code
):
    _configure(monkeypatch)

    class Client:
        def delete_object(self, **_kwargs):
            raise _client_error(code, 404)

    monkeypatch.setattr(r2_media.boto3, "client", lambda *args, **kwargs: Client())

    assert not r2_media.cleanup_temp_media_upload(_upload(), reason="test")
    assert "operation=DeleteObject" in caplog.text
    assert f"code={code}" in caplog.text
    assert "http_status=404" in caplog.text


def test_publication_cleanup_list_access_denied_has_safe_diagnostics(monkeypatch, caplog):
    _configure(monkeypatch)

    class Client:
        def list_objects_v2(self, **_kwargs):
            raise _client_error("AccessDenied", 403, "ListObjectsV2")

    monkeypatch.setattr(r2_media.boto3, "client", lambda *args, **kwargs: Client())

    assert not r2_media.cleanup_publication_media(PUBLICATION_A, reason="expired").complete
    assert "operation=ListObjectsV2" in caplog.text
    assert "code=AccessDenied" in caplog.text
    assert "http_status=403" in caplog.text
    assert "bucket=configured" in caplog.text
    assert "key=images/publications/publication-a/" in caplog.text


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
