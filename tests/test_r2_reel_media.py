from __future__ import annotations

from botocore.exceptions import ClientError, ConnectionClosedError
from pathlib import Path
import re
import subprocess
import tempfile

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


def _install_multipart_client(
    monkeypatch,
    tmp_path,
    *,
    curl_failure_indexes=(),
    complete_error=None,
    health_status=200,
    health_headers=None,
    presigned_url_overrides=None,
):
    """Install a presigned-URL/curl staging double; return one recorder dict."""
    recorder = {
        "client_configs": [],
        "created": [],
        "completed": [],
        "aborted": [],
        "deleted": [],
        "presigned": [],
        "upload_part_calls": [],
        "put_object_calls": [],
        "upload_file_calls": [],
        "curl_calls": [],
        "uploaded": [],
        "temp_files": [],
        "head_calls": [],
    }
    presigned_by_url = {}
    curl_call_count = {"count": 0}
    failing_indexes = set(curl_failure_indexes)
    real_mkstemp = tempfile.mkstemp

    class Client:
        def create_multipart_upload(self, **kwargs):
            recorder["created"].append(kwargs)
            return {"UploadId": "upload-id-1"}

        def generate_presigned_url(self, operation, Params):
            recorder["presigned"].append({"operation": operation, **Params})
            part_number = Params["PartNumber"]
            url = (presigned_url_overrides or {}).get(
                part_number,
                f"https://presigned.example/part-{part_number}",
            )
            presigned_by_url[url] = part_number
            return url

        def upload_part(self, **kwargs):
            recorder["upload_part_calls"].append(kwargs)

        def complete_multipart_upload(self, **kwargs):
            recorder["completed"].append(kwargs)
            if complete_error is not None:
                raise complete_error
            return {}

        def abort_multipart_upload(self, **kwargs):
            recorder["aborted"].append(kwargs)

        def delete_object(self, **kwargs):
            recorder["deleted"].append(kwargs["Key"])

        def put_object(self, **kwargs):
            recorder["put_object_calls"].append(kwargs)

        def upload_file(self, *args, **kwargs):
            recorder["upload_file_calls"].append((args, kwargs))

    def fake_client(*args, **kwargs):
        recorder["client_configs"].append(kwargs.get("config"))
        return Client()

    monkeypatch.setattr(r2_media.boto3, "client", fake_client)

    def fake_run(cmd, **kwargs):
        curl_call_count["count"] += 1
        recorder["curl_calls"].append(list(cmd))
        upload_path = cmd[cmd.index("--upload-file") + 1]
        part_number = presigned_by_url[cmd[-1]]
        recorder["uploaded"].append((part_number, Path(upload_path).read_bytes()))
        if curl_call_count["count"] in failing_indexes:
            return subprocess.CompletedProcess(cmd, 56, "", "curl: (56) failure")
        header_path = cmd[cmd.index("--dump-header") + 1]
        Path(header_path).write_text(
            f'HTTP/1.1 200 OK\r\nETag: "etag-{part_number}"\r\n', encoding="utf-8"
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(r2_media.subprocess, "run", fake_run)

    def fake_mkstemp(*args, **kwargs):
        kwargs.pop("dir", None)
        fd, path = real_mkstemp(dir=str(tmp_path), **kwargs)
        recorder["temp_files"].append(path)
        return fd, path

    monkeypatch.setattr(r2_media.tempfile, "mkstemp", fake_mkstemp)

    def fake_head(*args, **kwargs):
        recorder["head_calls"].append(kwargs)
        if health_status == 200 and health_headers is None:
            return type(
                "Healthy",
                (),
                {
                    "status_code": 200,
                    "headers": {
                        "Content-Type": "video/mp4; charset=binary",
                        "Content-Length": "42",
                    },
                },
            )()
        return type(
            "Unhealthy",
            (),
            {"status_code": health_status, "headers": health_headers},
        )()

    monkeypatch.setattr(r2_media.requests, "head", fake_head)
    return recorder


def test_stage_reel_mp4_uses_reel_owned_key_video_content_type_and_public_handle(
    monkeypatch, tmp_path
):
    _configure(monkeypatch)
    source = tmp_path / "reel.mp4"
    source.write_bytes(b"not-decoded-by-r2")
    monkeypatch.setattr(r2_media.uuid, "uuid4", lambda: type("Id", (), {"hex": "a" * 32})())

    recorder = _install_multipart_client(monkeypatch, tmp_path)

    upload = r2_media.stage_reel_mp4(str(source), PUBLICATION_A)

    assert upload.publication_id == PUBLICATION_A
    assert re.fullmatch(
        r"reels/publications/publication-a/[0-9]{14}_" + "a" * 32 + r"\.mp4",
        upload.object_key,
    )
    assert upload.public_url == f"https://media.example/{upload.object_key}"
    assert recorder["created"] == [
        {
            "Bucket": "configured",
            "Key": upload.object_key,
            "ContentType": "video/mp4",
        }
    ]
    assert recorder["uploaded"] == [(1, b"not-decoded-by-r2")]
    assert recorder["completed"][0]["MultipartUpload"] == {
        "Parts": [{"PartNumber": 1, "ETag": '"etag-1"'}]
    }
    assert recorder["aborted"] == []
    assert all(not Path(path).exists() for path in recorder["temp_files"])


def test_stage_reel_mp4_uploads_parts_via_presigned_urls_and_curl(
    monkeypatch, tmp_path
):
    _configure(monkeypatch)
    source = tmp_path / "reel.mp4"
    source.write_bytes(b"not-decoded-by-r2")

    recorder = _install_multipart_client(monkeypatch, tmp_path)

    upload = r2_media.stage_reel_mp4(str(source), PUBLICATION_A)

    assert upload.publication_id == PUBLICATION_A
    assert recorder["upload_part_calls"] == []
    assert recorder["put_object_calls"] == []
    assert recorder["upload_file_calls"] == []
    assert recorder["presigned"] == [
        {
            "operation": "upload_part",
            "Bucket": "configured",
            "Key": upload.object_key,
            "UploadId": "upload-id-1",
            "PartNumber": 1,
        }
    ]
    curl_command = recorder["curl_calls"][0]
    assert curl_command[0] == "curl"
    assert "--http1.1" in curl_command
    assert "--silent" in curl_command
    assert "--show-error" in curl_command
    assert curl_command[curl_command.index("--connect-timeout") + 1] == "10"
    assert curl_command[curl_command.index("--max-time") + 1] == "180"
    assert curl_command[curl_command.index("--upload-file") + 1]
    assert curl_command[-1].startswith("https://presigned.example/part-1")
    assert recorder["completed"][0]["MultipartUpload"] == {
        "Parts": [{"PartNumber": 1, "ETag": '"etag-1"'}]
    }
    assert recorder["aborted"] == []
    assert len(recorder["head_calls"]) == 1
    assert all(not Path(path).exists() for path in recorder["temp_files"])

    client_configs = recorder["client_configs"]
    assert len(client_configs) == 1
    staging_config = client_configs[0]
    assert staging_config.connect_timeout == 10
    assert staging_config.read_timeout == 120
    assert staging_config.request_checksum_calculation == "when_required"
    assert staging_config.retries == {"total_max_attempts": 1, "mode": "standard"}

    r2_media._get_s3_client(r2_media._load_configuration(require_public_url=False))
    assert len(recorder["client_configs"]) == 2
    assert recorder["client_configs"][1] is r2_media.R2_CLIENT_CONFIG
    assert r2_media.R2_CLIENT_CONFIG.connect_timeout == 10
    assert r2_media.R2_CLIENT_CONFIG.read_timeout == 30
    assert r2_media.R2_CLIENT_CONFIG.retries == {
        "total_max_attempts": 1,
        "mode": "standard",
    }


def test_stage_reel_mp4_splits_real_reel_size_into_ordered_five_mib_parts(
    monkeypatch, tmp_path
):
    _configure(monkeypatch)
    source = tmp_path / "reel.mp4"
    payload = (bytes(range(256)) * (22_464_053 // 256)) + bytes(range(22_464_053 % 256))
    assert len(payload) == 22_464_053
    source.write_bytes(payload)

    recorder = _install_multipart_client(monkeypatch, tmp_path)

    upload = r2_media.stage_reel_mp4(str(source), PUBLICATION_A)

    assert upload.publication_id == PUBLICATION_A
    part_size = 5 * 1024 * 1024
    assert [part_number for part_number, _ in recorder["uploaded"]] == [1, 2, 3, 4, 5]
    assert [len(body) for _, body in recorder["uploaded"]] == [
        part_size,
        part_size,
        part_size,
        part_size,
        22_464_053 - 4 * part_size,
    ]
    for index, (_, body) in enumerate(recorder["uploaded"]):
        offset = index * part_size
        assert body == payload[offset : offset + part_size]
    assert recorder["completed"][0]["MultipartUpload"] == {
        "Parts": [
            {"PartNumber": part_number, "ETag": f'"etag-{part_number}"'}
            for part_number in (1, 2, 3, 4, 5)
        ]
    }
    assert recorder["aborted"] == []


def test_stage_reel_mp4_retries_only_failed_part_with_fresh_exact_bytes(
    monkeypatch, tmp_path
):
    _configure(monkeypatch)
    source = tmp_path / "reel.mp4"
    part_size = 5 * 1024 * 1024
    payload = (b"\xab" * part_size) + (b"\xcd" * part_size) + b"\xef" * 1024
    source.write_bytes(payload)

    recorder = _install_multipart_client(
        monkeypatch, tmp_path, curl_failure_indexes={2}
    )

    upload = r2_media.stage_reel_mp4(str(source), PUBLICATION_A)

    assert upload.publication_id == PUBLICATION_A
    assert [part_number for part_number, _ in recorder["uploaded"]] == [1, 2, 2, 3]
    assert len(recorder["curl_calls"]) == 4
    part_one_uploads = [body for number, body in recorder["uploaded"] if number == 1]
    assert part_one_uploads == [payload[:part_size]]
    part_two_uploads = [body for number, body in recorder["uploaded"] if number == 2]
    assert part_two_uploads == [payload[part_size : 2 * part_size]] * 2
    assert recorder["completed"][0]["MultipartUpload"] == {
        "Parts": [
            {"PartNumber": 1, "ETag": '"etag-1"'},
            {"PartNumber": 2, "ETag": '"etag-2"'},
            {"PartNumber": 3, "ETag": '"etag-3"'},
        ]
    }
    assert recorder["aborted"] == []


def test_stage_reel_mp4_aborts_when_a_part_fails_permanently(monkeypatch, tmp_path):
    _configure(monkeypatch)
    source = tmp_path / "reel.mp4"
    part_size = 5 * 1024 * 1024
    source.write_bytes(b"\xab" * (part_size + 1))

    recorder = _install_multipart_client(
        monkeypatch, tmp_path, curl_failure_indexes={2, 3, 4}
    )

    with pytest.raises(RuntimeError, match="Failed to upload Reel MP4"):
        r2_media.stage_reel_mp4(str(source), PUBLICATION_A)

    assert [part_number for part_number, _ in recorder["uploaded"]] == [1, 2, 2, 2]
    assert len(recorder["curl_calls"]) == 4
    assert recorder["completed"] == []
    assert recorder["aborted"] == [
        {
            "Bucket": "configured",
            "Key": recorder["created"][0]["Key"],
            "UploadId": "upload-id-1",
        }
    ]
    assert recorder["head_calls"] == []
    assert all(not Path(path).exists() for path in recorder["temp_files"])


def test_stage_reel_mp4_fails_safely_when_curl_is_unavailable(monkeypatch, tmp_path):
    _configure(monkeypatch)
    source = tmp_path / "reel.mp4"
    source.write_bytes(b"not-decoded-by-r2")

    recorder = _install_multipart_client(monkeypatch, tmp_path)
    monkeypatch.setattr(r2_media.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="curl is required"):
        r2_media.stage_reel_mp4(str(source), PUBLICATION_A)

    assert recorder["created"] == []
    assert recorder["completed"] == []
    assert recorder["aborted"] == []
    assert recorder["curl_calls"] == []
    assert recorder["head_calls"] == []


@pytest.mark.parametrize(
    "unsafe_url",
    (
        "http://presigned.example/part-1",
        "https://localhost/part-1",
        "https://127.0.0.1/part-1",
        "https://10.0.0.5/part-1",
    ),
)
def test_stage_reel_mp4_rejects_unsafe_presigned_part_urls(
    monkeypatch, tmp_path, unsafe_url
):
    _configure(monkeypatch)
    source = tmp_path / "reel.mp4"
    source.write_bytes(b"not-decoded-by-r2")

    recorder = _install_multipart_client(
        monkeypatch, tmp_path, presigned_url_overrides={1: unsafe_url}
    )

    with pytest.raises(RuntimeError, match="Failed to upload Reel MP4") as excinfo:
        r2_media.stage_reel_mp4(str(source), PUBLICATION_A)

    assert "Presigned Reel part URL" in str(excinfo.value.__cause__)
    assert recorder["curl_calls"] == []
    assert recorder["completed"] == []
    assert recorder["aborted"] == [
        {
            "Bucket": "configured",
            "Key": recorder["created"][0]["Key"],
            "UploadId": "upload-id-1",
        }
    ]


def test_stage_reel_mp4_removes_temp_files_on_success_and_failure(
    monkeypatch, tmp_path
):
    _configure(monkeypatch)
    source = tmp_path / "reel.mp4"
    source.write_bytes(b"not-decoded-by-r2")

    recorder = _install_multipart_client(monkeypatch, tmp_path)
    r2_media.stage_reel_mp4(str(source), PUBLICATION_A)

    assert recorder["temp_files"]
    assert all(not Path(path).exists() for path in recorder["temp_files"])

    part_size = 5 * 1024 * 1024
    failing_source = tmp_path / "reel-large.mp4"
    failing_source.write_bytes(b"\xab" * (part_size + 1))
    recorder = _install_multipart_client(
        monkeypatch, tmp_path, curl_failure_indexes={2, 3, 4}
    )
    with pytest.raises(RuntimeError, match="Failed to upload Reel MP4"):
        r2_media.stage_reel_mp4(str(failing_source), PUBLICATION_A)

    assert recorder["temp_files"]
    assert all(not Path(path).exists() for path in recorder["temp_files"])


def test_stage_reel_mp4_aborts_and_skips_health_check_when_complete_fails(
    monkeypatch, tmp_path
):
    _configure(monkeypatch)
    source = tmp_path / "reel.mp4"
    source.write_bytes(b"not-decoded-by-r2")

    recorder = _install_multipart_client(
        monkeypatch,
        tmp_path,
        complete_error=ConnectionClosedError(endpoint_url="https://r2.example"),
    )

    with pytest.raises(RuntimeError, match="Failed to upload Reel MP4"):
        r2_media.stage_reel_mp4(str(source), PUBLICATION_A)

    assert len(recorder["completed"]) == 1
    assert recorder["aborted"] == [
        {
            "Bucket": "configured",
            "Key": recorder["created"][0]["Key"],
            "UploadId": "upload-id-1",
        }
    ]
    assert recorder["head_calls"] == []


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

    recorder = _install_multipart_client(
        monkeypatch,
        tmp_path,
        health_status=status,
        health_headers={
            "Content-Type": content_type,
            "Content-Length": content_length,
        },
    )

    with pytest.raises(RuntimeError, match="public health check failed"):
        r2_media.stage_reel_mp4(str(source), PUBLICATION_A)

    assert len(recorder["completed"]) == 1
    assert recorder["deleted"] == [recorder["created"][0]["Key"]]
    assert recorder["deleted"][0].startswith("reels/publications/publication-a/")
    assert recorder["aborted"] == []


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
