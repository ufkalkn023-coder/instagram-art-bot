import io
import socket

import pytest
from PIL import Image

from src import quality_filter


class FakeResponse:
    def __init__(self, status_code=200, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)
        self.closed = False
        self.iterated = False

    def iter_content(self, chunk_size):
        self.iterated = True
        yield from self._chunks

    def close(self):
        self.closed = True


def _image_bytes(image_format="JPEG", size=(100, 100)):
    image = Image.new("RGB", size, color="navy")
    stream = io.BytesIO()
    image.save(stream, format=image_format)
    return stream.getvalue()


def _public_dns(hostname, port, type):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def _install_public_dns(monkeypatch):
    monkeypatch.setattr(quality_filter.socket, "getaddrinfo", _public_dns)


def test_valid_public_https_jpeg_is_streamed_and_atomically_replaces_target(monkeypatch, tmp_path):
    _install_public_dns(monkeypatch)
    image_bytes = _image_bytes()
    response = FakeResponse(
        headers={"Content-Type": "image/jpeg", "Content-Length": str(len(image_bytes))},
        chunks=[image_bytes],
    )
    calls = []
    monkeypatch.setattr(
        quality_filter.requests,
        "get",
        lambda url, **kwargs: (calls.append((url, kwargs)) or response),
    )
    target = tmp_path / "artwork.jpg"
    target.write_bytes(b"previous-valid-image")

    assert quality_filter.validate_and_download_image("https://museum.example/artwork.jpg", str(target))
    assert target.read_bytes() == image_bytes
    assert response.closed
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["timeout"] == quality_filter.HTTP_TIMEOUT_SECONDS


def test_metadata_result_uses_dimensions_from_the_same_secure_download(monkeypatch, tmp_path):
    _install_public_dns(monkeypatch)
    image_bytes = _image_bytes("PNG", size=(843, 600))
    calls = []
    monkeypatch.setattr(
        quality_filter.requests,
        "get",
        lambda url, **kwargs: (
            calls.append(url)
            or FakeResponse(headers={"Content-Type": "image/png"}, chunks=[image_bytes])
        ),
    )

    result = quality_filter.validate_and_download_image_with_metadata(
        "https://museum.example/artwork.png", str(tmp_path / "artwork.png")
    )

    assert result.valid
    assert (result.width, result.height, result.image_format) == (843, 600, "PNG")
    assert calls == ["https://museum.example/artwork.png"]


def test_normal_1686_pixel_aic_derivative_is_accepted_with_its_dimensions(monkeypatch, tmp_path):
    _install_public_dns(monkeypatch)
    image_bytes = _image_bytes("JPEG", size=(1686, 1200))
    monkeypatch.setattr(
        quality_filter.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(headers={"Content-Type": "image/jpeg"}, chunks=[image_bytes]),
    )

    result = quality_filter.validate_and_download_image_with_metadata(
        "https://www.artic.edu/iiif/2/image-1/full/1686,/0/default.jpg",
        str(tmp_path / "artwork.jpg"),
    )

    assert result.valid
    assert (result.width, result.height) == (1686, 1200)


@pytest.mark.parametrize(
    "url",
    [
        "http://museum.example/artwork.jpg",
        "file:///tmp/artwork.jpg",
        "data:image/jpeg;base64,AAAA",
        "https:///missing-host.jpg",
        "https://localhost/artwork.jpg",
        "https://127.0.0.1/artwork.jpg",
        "https://10.0.0.1/artwork.jpg",
        "https://192.168.1.1/artwork.jpg",
        "https://172.16.0.1/artwork.jpg",
        "https://169.254.169.254/artwork.jpg",
        "https://[::1]/artwork.jpg",
        "https://[fd00::1]/artwork.jpg",
        "https://[fe80::1]/artwork.jpg",
    ],
)
def test_unsafe_or_unsupported_urls_are_rejected_before_http(monkeypatch, tmp_path, url):
    monkeypatch.setattr(quality_filter.requests, "get", lambda *args, **kwargs: pytest.fail("HTTP must not run"))

    assert not quality_filter.validate_and_download_image(url, str(tmp_path / "artwork.jpg"))


def test_dns_requires_every_address_to_be_public(monkeypatch):
    monkeypatch.setattr(quality_filter.socket, "getaddrinfo", _public_dns)
    assert quality_filter._is_safe_image_url("https://museum.example/artwork.jpg")

    def private_dns(hostname, port, type):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port))]

    monkeypatch.setattr(quality_filter.socket, "getaddrinfo", private_dns)
    assert not quality_filter._is_safe_image_url("https://museum.example/artwork.jpg")

    def mixed_dns(hostname, port, type):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", port)),
        ]

    monkeypatch.setattr(quality_filter.socket, "getaddrinfo", mixed_dns)
    assert not quality_filter._is_safe_image_url("https://museum.example/artwork.jpg")

    def failing_dns(hostname, port, type):
        raise socket.gaierror("resolution failed")

    monkeypatch.setattr(quality_filter.socket, "getaddrinfo", failing_dns)
    assert not quality_filter._is_safe_image_url("https://museum.example/artwork.jpg")


def test_safe_redirect_is_followed_only_after_validating_target(monkeypatch, tmp_path):
    _install_public_dns(monkeypatch)
    image_bytes = _image_bytes()
    responses = iter(
        [
            FakeResponse(302, {"Location": "https://cdn.example/artwork.jpg"}),
            FakeResponse(200, {"Content-Type": "image/jpeg"}, [image_bytes]),
        ]
    )
    requested_urls = []
    monkeypatch.setattr(
        quality_filter.requests,
        "get",
        lambda url, **kwargs: (requested_urls.append(url) or next(responses)),
    )

    assert quality_filter.validate_and_download_image("https://museum.example/artwork.jpg", str(tmp_path / "artwork.jpg"))
    assert requested_urls == ["https://museum.example/artwork.jpg", "https://cdn.example/artwork.jpg"]


def test_redirect_to_private_host_is_rejected_before_second_http_request(monkeypatch, tmp_path):
    _install_public_dns(monkeypatch)
    response = FakeResponse(302, {"Location": "https://127.0.0.1/internal"})
    requests_made = []
    monkeypatch.setattr(
        quality_filter.requests,
        "get",
        lambda url, **kwargs: (requests_made.append(url) or response),
    )

    assert not quality_filter.validate_and_download_image("https://museum.example/artwork.jpg", str(tmp_path / "artwork.jpg"))
    assert requests_made == ["https://museum.example/artwork.jpg"]


def test_redirect_loop_and_limit_are_rejected(monkeypatch, tmp_path):
    _install_public_dns(monkeypatch)
    loop_response = FakeResponse(302, {"Location": "https://museum.example/artwork.jpg"})
    loop_calls = []
    monkeypatch.setattr(
        quality_filter.requests,
        "get",
        lambda url, **kwargs: (loop_calls.append(url) or loop_response),
    )
    assert not quality_filter.validate_and_download_image("https://museum.example/artwork.jpg", str(tmp_path / "loop.jpg"))
    assert len(loop_calls) == 1

    responses = iter(
        [FakeResponse(302, {"Location": f"https://museum.example/{index}.jpg"}) for index in range(5)]
    )
    monkeypatch.setattr(quality_filter.requests, "get", lambda url, **kwargs: next(responses))
    assert not quality_filter.validate_and_download_image("https://museum.example/start.jpg", str(tmp_path / "limit.jpg"))


def test_content_length_limit_rejects_before_download_and_preserves_target(monkeypatch, tmp_path):
    _install_public_dns(monkeypatch)
    response = FakeResponse(
        headers={"Content-Type": "image/jpeg", "Content-Length": str(quality_filter.MAX_IMAGE_DOWNLOAD_BYTES + 1)}
    )
    monkeypatch.setattr(quality_filter.requests, "get", lambda *args, **kwargs: response)
    target = tmp_path / "artwork.jpg"
    target.write_bytes(b"previous-valid-image")

    assert not quality_filter.validate_and_download_image("https://museum.example/artwork.jpg", str(target))
    assert not response.iterated
    assert target.read_bytes() == b"previous-valid-image"
    assert not list(tmp_path.glob(".image-download-*"))


def test_streamed_limit_rejects_cleans_partial_file_and_preserves_target(monkeypatch, tmp_path):
    _install_public_dns(monkeypatch)
    monkeypatch.setattr(quality_filter, "MAX_IMAGE_DOWNLOAD_BYTES", 10)
    response = FakeResponse(headers={"Content-Type": "image/jpeg"}, chunks=[b"12345", b"678901"])
    monkeypatch.setattr(quality_filter.requests, "get", lambda *args, **kwargs: response)
    target = tmp_path / "artwork.jpg"
    target.write_bytes(b"previous-valid-image")

    assert not quality_filter.validate_and_download_image("https://museum.example/artwork.jpg", str(target))
    assert target.read_bytes() == b"previous-valid-image"
    assert not list(tmp_path.glob(".image-download-*"))


def test_exact_download_limit_is_allowed_when_image_is_valid(monkeypatch, tmp_path):
    _install_public_dns(monkeypatch)
    image_bytes = _image_bytes()
    monkeypatch.setattr(quality_filter, "MAX_IMAGE_DOWNLOAD_BYTES", len(image_bytes))
    response = FakeResponse(headers={"Content-Type": "image/jpeg"}, chunks=[image_bytes])
    monkeypatch.setattr(quality_filter.requests, "get", lambda *args, **kwargs: response)

    assert quality_filter.validate_and_download_image("https://museum.example/artwork.jpg", str(tmp_path / "artwork.jpg"))


@pytest.mark.parametrize("content_type", ["text/html", "application/json", "text/xml", "image/svg+xml"])
def test_explicit_non_image_content_types_are_rejected(monkeypatch, tmp_path, content_type):
    _install_public_dns(monkeypatch)
    response = FakeResponse(headers={"Content-Type": content_type}, chunks=[b"not an image"])
    monkeypatch.setattr(quality_filter.requests, "get", lambda *args, **kwargs: response)

    assert not quality_filter.validate_and_download_image("https://museum.example/artwork.jpg", str(tmp_path / "artwork.jpg"))
    assert not response.iterated


@pytest.mark.parametrize("payload", [b"not an image", _image_bytes()[:30]])
def test_fake_or_truncated_jpeg_is_rejected_by_pillow(monkeypatch, tmp_path, payload):
    _install_public_dns(monkeypatch)
    response = FakeResponse(headers={"Content-Type": "image/jpeg"}, chunks=[payload])
    monkeypatch.setattr(quality_filter.requests, "get", lambda *args, **kwargs: response)

    assert not quality_filter.validate_and_download_image("https://museum.example/fake.jpg", str(tmp_path / "fake.jpg"))
    assert not list(tmp_path.glob(".image-download-*"))


def test_png_is_an_accepted_raster_format(monkeypatch, tmp_path):
    _install_public_dns(monkeypatch)
    image_bytes = _image_bytes("PNG")
    response = FakeResponse(headers={"Content-Type": "application/octet-stream"}, chunks=[image_bytes])
    monkeypatch.setattr(quality_filter.requests, "get", lambda *args, **kwargs: response)

    assert quality_filter.validate_and_download_image("https://museum.example/artwork", str(tmp_path / "artwork.jpg"))


def test_pixel_limit_and_pillow_decompression_bomb_warning_are_hard_rejections(monkeypatch, tmp_path):
    _install_public_dns(monkeypatch)
    image_bytes = _image_bytes(size=(100, 100))
    monkeypatch.setattr(quality_filter, "MAX_IMAGE_PIXELS", 9_999)
    monkeypatch.setattr(
        quality_filter.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(headers={"Content-Type": "image/jpeg"}, chunks=[image_bytes]),
    )
    assert not quality_filter.validate_and_download_image("https://museum.example/large.jpg", str(tmp_path / "large.jpg"))

    monkeypatch.setattr(quality_filter, "MAX_IMAGE_PIXELS", 40_000_000)
    monkeypatch.setattr(quality_filter.Image, "MAX_IMAGE_PIXELS", 9_999)
    assert not quality_filter.validate_and_download_image("https://museum.example/bomb.jpg", str(tmp_path / "bomb.jpg"))


def test_logs_redact_query_string_credentials(monkeypatch, tmp_path, caplog):
    _install_public_dns(monkeypatch)
    monkeypatch.setattr(
        quality_filter.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(status_code=500),
    )

    assert not quality_filter.validate_and_download_image(
        "https://museum.example/artwork.jpg?api_key=super-secret-token",
        str(tmp_path / "artwork.jpg"),
    )
    assert "super-secret-token" not in caplog.text
    assert "user-secret" not in quality_filter._safe_url_for_log(
        "https://account:user-secret@museum.example/artwork.jpg?api_key=super-secret-token"
    )
