import socket
import threading
import time

from src import quality_filter
from src.aic_image_policy import (
    AICImageRequestPolicy,
    AIC_USER_AGENT,
    ImageDownloadPurpose,
    aic_image_url_for_purpose,
    aic_request_headers,
    reset_aic_image_request_policy_for_tests,
)


AIC_1686 = "https://www.artic.edu/iiif/2/image-1/full/1686,/0/default.jpg"
AIC_843 = "https://www.artic.edu/iiif/2/image-1/full/843,/0/default.jpg"


class Response:
    def __init__(self, status_code=403):
        self.status_code = status_code
        self.headers = {}

    def iter_content(self, chunk_size):
        return iter(())

    def close(self):
        pass


def test_image_purpose_selects_explicit_aic_derivative():
    assert (
        aic_image_url_for_purpose(AIC_1686, ImageDownloadPurpose.IMAGE_ANALYSIS)
        == AIC_843
    )
    assert (
        aic_image_url_for_purpose(AIC_1686, ImageDownloadPurpose.FINAL_RENDER)
        == AIC_1686
    )


def test_aic_requests_are_paced_but_non_aic_requests_are_not():
    now = [10.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    policy = AICImageRequestPolicy(
        interval_seconds=1.0, monotonic=lambda: now[0], sleep=sleep
    )
    policy.request(AIC_843, ImageDownloadPurpose.IMAGE_ANALYSIS, lambda: Response(200))
    policy.request(AIC_843, ImageDownloadPurpose.IMAGE_ANALYSIS, lambda: Response(200))
    policy.request(
        "https://museum.example/image.jpg",
        ImageDownloadPurpose.FINAL_RENDER,
        lambda: Response(200),
    )

    assert sleeps == [1.0]


def test_two_aic_downloads_cannot_issue_simultaneous_requests():
    policy = AICImageRequestPolicy(interval_seconds=0)
    active = 0
    maximum_active = 0
    state_lock = threading.Lock()
    first_started = threading.Event()
    release_first = threading.Event()

    def request():
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
            is_first = not first_started.is_set()
            first_started.set()
        if is_first:
            release_first.wait(timeout=1)
        with state_lock:
            active -= 1
        return Response(200)

    first = threading.Thread(
        target=lambda: policy.request(
            AIC_843, ImageDownloadPurpose.IMAGE_ANALYSIS, request
        )
    )
    second = threading.Thread(
        target=lambda: policy.request(
            AIC_843, ImageDownloadPurpose.IMAGE_ANALYSIS, request
        )
    )
    first.start()
    assert first_started.wait(timeout=1)
    second.start()
    time.sleep(0.02)
    release_first.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert maximum_active == 1


def test_aic_identifying_header_is_scoped_to_aic_hosts():
    assert aic_request_headers(AIC_843)["AIC-User-Agent"] == AIC_USER_AGENT
    assert "AIC-User-Agent" not in aic_request_headers(
        "https://museum.example/image.jpg"
    )


def test_analysis_rate_limit_retry_is_bounded_and_opens_health_circuit(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        quality_filter.socket,
        "getaddrinfo",
        lambda hostname, port, type: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ],
    )
    calls = []
    monkeypatch.setattr(
        quality_filter.requests,
        "get",
        lambda url, **kwargs: (calls.append((url, kwargs)) or Response(429)),
    )
    policy = reset_aic_image_request_policy_for_tests(
        AICImageRequestPolicy(interval_seconds=0)
    )

    first = quality_filter.validate_and_download_image_with_metadata(
        AIC_1686,
        str(tmp_path / "first.jpg"),
        purpose=ImageDownloadPurpose.IMAGE_ANALYSIS,
    )
    second = quality_filter.validate_and_download_image_with_metadata(
        AIC_1686,
        str(tmp_path / "second.jpg"),
        purpose=ImageDownloadPurpose.IMAGE_ANALYSIS,
    )

    assert not first.valid and not second.valid
    assert [url for url, _ in calls] == [AIC_843, AIC_843, AIC_843]
    assert all(
        kwargs["headers"]["AIC-User-Agent"] == AIC_USER_AGENT for _, kwargs in calls
    )
    diagnostics = policy.diagnostics()
    assert diagnostics.rate_limited == 3
    assert diagnostics.circuit_open
    assert diagnostics.failed == 2
