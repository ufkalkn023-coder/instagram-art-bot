import pytest
import requests

from src import instagram_poster


class FakeResponse:
    def __init__(self, status_code, payload=None, json_error=False):
        self.status_code = status_code
        self.payload = payload
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise ValueError("invalid json")
        return self.payload


def response_sequence(*responses):
    iterator = iter(responses)

    def request(*args, **kwargs):
        value = next(iterator)
        if isinstance(value, Exception):
            raise value
        return value

    return request


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(instagram_poster.time, "sleep", lambda _seconds: None)


def test_single_publish_requires_finished_and_uses_bearer_auth(monkeypatch):
    post = response_sequence(FakeResponse(200, {"id": "container-1"}), FakeResponse(200, {"id": "media-1"}))
    get = response_sequence(FakeResponse(200, {"status_code": "FINISHED"}))
    monkeypatch.setattr(instagram_poster.requests, "post", post)
    monkeypatch.setattr(instagram_poster.requests, "get", get)

    media_id = instagram_poster.post_to_instagram_graph_api(
        "https://images.example/art.jpg", "caption", "account", "secret-token", alt_text="alt"
    )

    assert media_id == "media-1"


@pytest.mark.parametrize("status", ["ERROR", "EXPIRED", "UNEXPECTED"])
def test_non_finished_status_stops_before_publish(monkeypatch, status):
    post_calls = []
    monkeypatch.setattr(
        instagram_poster.requests,
        "post",
        lambda *args, **kwargs: post_calls.append((args, kwargs)) or FakeResponse(200, {"id": "container-1"}),
    )
    monkeypatch.setattr(instagram_poster.requests, "get", response_sequence(FakeResponse(200, {"status_code": status})))

    with pytest.raises(instagram_poster.InstagramMediaProcessingError):
        instagram_poster.post_to_instagram_graph_api("https://images.example/art.jpg", "caption", "account", "token")

    assert len(post_calls) == 1


def test_in_progress_then_finished_succeeds(monkeypatch):
    monkeypatch.setattr(
        instagram_poster.requests,
        "post",
        response_sequence(FakeResponse(200, {"id": "container-1"}), FakeResponse(200, {"id": "media-1"})),
    )
    monkeypatch.setattr(
        instagram_poster.requests,
        "get",
        response_sequence(FakeResponse(200, {"status_code": "IN_PROGRESS"}), FakeResponse(200, {"status_code": "FINISHED"})),
    )

    assert instagram_poster.post_to_instagram_graph_api("https://images.example/art.jpg", "caption", "account", "token") == "media-1"


def test_in_progress_timeout_does_not_publish(monkeypatch):
    post_calls = []
    monkeypatch.setattr(
        instagram_poster.requests,
        "post",
        lambda *args, **kwargs: post_calls.append((args, kwargs)) or FakeResponse(200, {"id": "container-1"}),
    )
    monkeypatch.setattr(instagram_poster.requests, "get", response_sequence(FakeResponse(200, {"status_code": "IN_PROGRESS"})))
    monkeypatch.setattr(instagram_poster.time, "monotonic", response_sequence(0, instagram_poster.MEDIA_STATUS_TIMEOUT_SECONDS))

    with pytest.raises(instagram_poster.InstagramMediaProcessingError, match="timed out"):
        instagram_poster.post_to_instagram_graph_api("https://images.example/art.jpg", "caption", "account", "token")

    assert len(post_calls) == 1


@pytest.mark.parametrize("payload", [{}, {"status_code": None}])
def test_missing_or_malformed_status_is_a_safe_failure(monkeypatch, payload):
    monkeypatch.setattr(instagram_poster.requests, "get", response_sequence(FakeResponse(200, payload)))

    with pytest.raises(instagram_poster.InstagramMediaProcessingError):
        instagram_poster._wait_until_finished("container-1", "token")


def test_malformed_json_is_rejected(monkeypatch):
    monkeypatch.setattr(instagram_poster.requests, "get", response_sequence(FakeResponse(200, json_error=True)))

    with pytest.raises(instagram_poster.InstagramAPIError, match="malformed JSON"):
        instagram_poster._wait_until_finished("container-1", "token")


@pytest.mark.parametrize("status_code", [429, 500])
def test_container_creation_retries_transient_http_errors(monkeypatch, status_code):
    calls = []
    monkeypatch.setattr(
        instagram_poster.requests,
        "post",
        lambda *args, **kwargs: calls.append((args, kwargs)) or (
            FakeResponse(status_code, {"error": {"message": "temporary"}})
            if len(calls) == 1
            else FakeResponse(200, {"id": "container-1"})
        ),
    )

    assert instagram_poster._create_container("account", "token", {"image_url": "https://images.example/art.jpg"}, "container creation") == "container-1"
    assert len(calls) == 2
    assert calls[0][1]["headers"] == {"Authorization": "Bearer token"}
    assert "access_token" not in calls[0][1]["data"]


def test_container_creation_retries_timeout_but_not_permanent_or_auth_errors(monkeypatch):
    monkeypatch.setattr(
        instagram_poster.requests,
        "post",
        response_sequence(requests.Timeout(), FakeResponse(200, {"id": "container-1"})),
    )
    assert instagram_poster._create_container("account", "token", {}, "container creation") == "container-1"

    permanent_calls = []
    monkeypatch.setattr(
        instagram_poster.requests,
        "post",
        lambda *args, **kwargs: permanent_calls.append(1) or FakeResponse(400, {"error": {"message": "bad request"}}),
    )
    with pytest.raises(instagram_poster.InstagramAPIError):
        instagram_poster._create_container("account", "token", {}, "container creation")
    assert len(permanent_calls) == 1

    monkeypatch.setattr(
        instagram_poster.requests,
        "post",
        response_sequence(FakeResponse(400, {"error": {"code": 190, "message": "token failure"}})),
    )
    with pytest.raises(instagram_poster.InstagramAuthError):
        instagram_poster._create_container("account", "token", {}, "container creation")


def test_api_error_metadata_is_structured_without_exposing_the_token(monkeypatch):
    monkeypatch.setattr(
        instagram_poster.requests,
        "post",
        response_sequence(
            FakeResponse(
                400,
                {
                    "error": {
                        "message": "permission denied",
                        "type": "OAuthException",
                        "code": 10,
                        "error_subcode": 200,
                        "fbtrace_id": "trace-1",
                    }
                },
            )
        ),
    )

    with pytest.raises(instagram_poster.InstagramAPIError) as raised:
        instagram_poster._create_container("account", "secret-token", {}, "container creation")

    assert raised.value.error_type == "OAuthException"
    assert raised.value.error_subcode == 200
    assert raised.value.fbtrace_id == "trace-1"
    assert "secret-token" not in str(raised.value)


def test_retry_exhaustion_and_publish_ambiguity_are_distinct(monkeypatch):
    monkeypatch.setattr(
        instagram_poster.requests,
        "post",
        response_sequence(*[FakeResponse(503, {"error": {"message": "unavailable"}}) for _ in range(3)]),
    )
    with pytest.raises(instagram_poster.InstagramTransientError):
        instagram_poster._create_container("account", "token", {}, "container creation")

    monkeypatch.setattr(instagram_poster.requests, "post", response_sequence(requests.Timeout()))
    with pytest.raises(instagram_poster.InstagramPublishAmbiguousError):
        instagram_poster._publish_container("account", "token", "container-1")


@pytest.mark.parametrize(
    "response",
    [FakeResponse(200, {}), FakeResponse(200, json_error=True)],
)
def test_publish_requires_valid_media_id(monkeypatch, response):
    monkeypatch.setattr(instagram_poster.requests, "post", response_sequence(response))

    with pytest.raises(instagram_poster.InstagramPublishAmbiguousError):
        instagram_poster._publish_container("account", "token", "container-1")


def test_carousel_stops_when_child_or_parent_is_not_finished(monkeypatch):
    child_posts = []
    monkeypatch.setattr(
        instagram_poster.requests,
        "post",
        lambda *args, **kwargs: child_posts.append((args, kwargs)) or FakeResponse(200, {"id": "child-1"}),
    )
    monkeypatch.setattr(instagram_poster.requests, "get", response_sequence(FakeResponse(200, {"status_code": "ERROR"})))
    with pytest.raises(instagram_poster.InstagramMediaProcessingError):
        instagram_poster.post_carousel_to_instagram_graph_api(["a", "b"], "caption", "account", "token")
    assert len(child_posts) == 1

    parent_posts = response_sequence(
        FakeResponse(200, {"id": "child-1"}),
        FakeResponse(200, {"id": "child-2"}),
        FakeResponse(200, {"id": "parent"}),
    )
    monkeypatch.setattr(instagram_poster.requests, "post", parent_posts)
    monkeypatch.setattr(
        instagram_poster.requests,
        "get",
        response_sequence(
            FakeResponse(200, {"status_code": "FINISHED"}),
            FakeResponse(200, {"status_code": "FINISHED"}),
            FakeResponse(200, {"status_code": "ERROR"}),
        ),
    )
    with pytest.raises(instagram_poster.InstagramMediaProcessingError):
        instagram_poster.post_carousel_to_instagram_graph_api(["a", "b"], "caption", "account", "token")


def test_carousel_publishes_only_after_all_children_and_parent_finish(monkeypatch):
    monkeypatch.setattr(
        instagram_poster.requests,
        "post",
        response_sequence(
            FakeResponse(200, {"id": "child-1"}),
            FakeResponse(200, {"id": "child-2"}),
            FakeResponse(200, {"id": "parent"}),
            FakeResponse(200, {"id": "media-1"}),
        ),
    )
    monkeypatch.setattr(
        instagram_poster.requests,
        "get",
        response_sequence(*[FakeResponse(200, {"status_code": "FINISHED"}) for _ in range(3)]),
    )

    assert instagram_poster.post_carousel_to_instagram_graph_api(["a", "b"], "caption", "account", "token") == "media-1"


@pytest.mark.parametrize("media_urls", [[], ["one"], [str(index) for index in range(11)]])
def test_carousel_item_limits_are_validated_before_requests(monkeypatch, media_urls):
    monkeypatch.setattr(instagram_poster.requests, "post", lambda *args, **kwargs: pytest.fail("request should not be made"))

    with pytest.raises(ValueError):
        instagram_poster.post_carousel_to_instagram_graph_api(media_urls, "caption", "account", "token")
