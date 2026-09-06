import requests
import pytest

from src.instagram_insights import (
    InstagramInsightsAuthenticationError,
    InstagramInsightsClient,
    InstagramInsightsConfigurationError,
    InstagramInsightsPermissionError,
    InstagramInsightsRequestError,
    OPTIONAL_METRICS,
    TARGET_METRICS,
    parse_rate_limit_headers,
)


class FakeResponse:
    def __init__(self, status_code, payload=None, json_error=False, headers=None):
        self.status_code = status_code
        self.payload = payload
        self.json_error = json_error
        self.headers = headers or {}

    def json(self):
        if self.json_error:
            raise ValueError("invalid json")
        return self.payload


def test_full_response_uses_configured_endpoint_and_requested_metrics():
    calls = []

    class Session:
        def get(self, *args, **kwargs):
            calls.append((args, kwargs))
            return FakeResponse(
                200,
                {"data": [{"name": metric, "values": [{"value": index}]} for index, metric in enumerate(TARGET_METRICS)],},
            )

    response = InstagramInsightsClient("secret-token", Session()).fetch_media_insights("parent-media")

    assert response.metrics["views"] == 0
    assert response.returned_metrics == TARGET_METRICS
    assert response.missing_metrics == ()
    assert calls[0][0][0].endswith("/v22.0/parent-media/insights")
    assert calls[0][1]["params"] == {"metric": ",".join(TARGET_METRICS), "access_token": "secret-token"}


def test_partial_and_malformed_values_remain_missing():
    class Session:
        def get(self, *args, **kwargs):
            return FakeResponse(
                200,
                {
                    "data": [
                        {"name": "views", "values": [{"value": 123}]},
                        {"name": "saved", "values": [{"value": 0}]},
                        {"name": "shares", "values": [{"value": "not-a-number"}]},
                        {"name": "impressions", "values": [{"value": 999}]},
                    ]
                },
            )

    response = InstagramInsightsClient("token", Session()).fetch_media_insights("media")

    assert response.metrics == {"views": 123, "saved": 0}
    assert response.returned_metrics == ("views", "saved")
    assert "shares" in response.missing_metrics
    assert "impressions" not in response.metrics


def test_unsupported_optional_metric_is_isolated_without_losing_core_metrics():
    calls = []

    class Session:
        def get(self, *args, **kwargs):
            requested = tuple(kwargs["params"]["metric"].split(","))
            calls.append(requested)
            if "ig_reels_avg_watch_time" in requested:
                return FakeResponse(400, {"error": {"code": 100, "message": "unsupported"}})
            return FakeResponse(200, {"data": [{"name": name, "values": [{"value": 1}]} for name in requested]})

    response = InstagramInsightsClient("token", Session()).fetch_media_insights("media")
    assert response.metrics["views"] == 1
    assert "ig_reels_avg_watch_time" not in response.metrics
    assert "ig_reels_avg_watch_time" in response.missing_metrics
    assert response.api_calls == len(calls) > 1
    assert set(OPTIONAL_METRICS).intersection(response.requested_metrics)
    assert response.permanently_unavailable is False
    assert response.permanently_failed_metrics == ("ig_reels_avg_watch_time",)
    assert response.diagnostics


def test_multiple_unsupported_metrics_are_retained_while_other_metrics_succeed():
    unsupported = {"saved", "clips_replays_count"}

    class Session:
        def get(self, *args, **kwargs):
            requested = tuple(kwargs["params"]["metric"].split(","))
            if unsupported.intersection(requested):
                return FakeResponse(400, {"error": {"code": 100, "message": "unsupported"}})
            return FakeResponse(200, {
                "data": [{"name": name, "values": [{"value": 1}]} for name in requested]
            })

    response = InstagramInsightsClient("token", Session()).fetch_media_insights("media")

    assert response.metrics["reach"] == 1
    assert set(response.permanently_failed_metrics) == unsupported
    assert response.permanently_unavailable is False
    assert len(response.diagnostics) == 2


def test_all_code_100_metric_failures_are_explicit_permanent_and_sanitized(caplog):
    secret = "very-secret-token"

    class Session:
        def get(self, *args, **kwargs):
            return FakeResponse(400, {"error": {
                "type": "OAuthException",
                "code": 100,
                "message": f"unsupported access_token={secret}",
            }})

    response = InstagramInsightsClient(secret, Session()).fetch_media_insights("media")

    assert response.metrics == {}
    assert response.permanently_unavailable is True
    assert response.permanent_failure_category == "all_metrics_permanently_unavailable"
    assert response.permanently_failed_metrics == TARGET_METRICS
    assert len(response.diagnostics) == len(TARGET_METRICS)
    assert all("code=100" in diagnostic for diagnostic in response.diagnostics)
    assert secret not in str(response.diagnostics)
    assert secret not in caplog.text


def test_server_error_is_not_misreported_as_an_unsupported_metric():
    class Session:
        def get(self, *args, **kwargs):
            return FakeResponse(503, {"error": {"code": 2, "message": "temporary"}})

    with pytest.raises(InstagramInsightsRequestError, match="HTTP 503"):
        InstagramInsightsClient("token", Session()).fetch_media_insights("media")


def test_discovery_filters_reels_and_uses_get_only():
    calls = []

    class Session:
        def get(self, *args, **kwargs):
            calls.append((args, kwargs))
            return FakeResponse(200, {"data": [
                {"id": "reel", "media_type": "VIDEO", "media_product_type": "REELS", "caption": "safe", "timestamp": "2026-08-25T10:00:00+0000"},
                {"id": "photo", "media_type": "IMAGE", "media_product_type": "FEED", "timestamp": "2026-08-25T09:00:00+0000"},
            ]})

    response = InstagramInsightsClient("token", Session()).discover_recent_media("account")
    assert [item.id for item in response.media] == ["reel"]
    assert calls[0][0][0].endswith("/v22.0/account/media")
    assert not hasattr(Session(), "post")


def test_discovery_rejects_account_id_stored_as_access_token_without_an_api_call():
    class Session:
        def get(self, *args, **kwargs):
            raise AssertionError("Meta must not be called with a known-invalid credential")

    with pytest.raises(InstagramInsightsConfigurationError, match="contains INSTAGRAM_ACCOUNT_ID"):
        InstagramInsightsClient("17841470283853922", Session()).discover_recent_media("17841470283853922")


def test_rate_limit_headers_keep_only_anonymous_numeric_usage():
    usage = parse_rate_limit_headers({
        "x-app-usage": '{"call_count":3,"total_cputime":2,"total_time":4}',
        "x-business-use-case-usage": '{"178-secret":[{"type":"instagram","call_count":5,"estimated_time_to_regain_access":0}]}',
        "authorization": "secret-token",
    })
    assert usage == {
        "app.call_count": 3, "app.total_cputime": 2, "app.total_time": 4,
        "business.call_count": 5, "business.estimated_time_to_regain_access": 0,
    }
    assert "178-secret" not in str(usage)
    assert "secret-token" not in str(usage)


@pytest.mark.parametrize("payload", [{"data": []}, {"data": [{}]}])
def test_empty_or_unusable_data_is_normal_availability_pending(payload):
    class Session:
        def get(self, *args, **kwargs):
            return FakeResponse(200, payload)

    response = InstagramInsightsClient("token", Session()).fetch_media_insights("media")
    assert response.metrics == {}
    assert response.missing_metrics == TARGET_METRICS


@pytest.mark.parametrize("response", [FakeResponse(200, json_error=True), FakeResponse(200, {})])
def test_invalid_json_or_shape_is_a_safe_failure(response):
    class Session:
        def get(self, *args, **kwargs):
            return response

    with pytest.raises(InstagramInsightsRequestError):
        InstagramInsightsClient("token", Session()).fetch_media_insights("media")


def test_network_and_permission_errors_do_not_expose_token(caplog):
    secret = "very-secret-token"

    class NetworkSession:
        def get(self, *args, **kwargs):
            raise requests.Timeout(f"request for {secret}")

    with pytest.raises(InstagramInsightsRequestError) as network_error:
        InstagramInsightsClient(secret, NetworkSession()).fetch_media_insights("media-123456")
    assert secret not in str(network_error.value)
    assert secret not in caplog.text


def test_meta_400_diagnostic_is_sanitized_and_identifies_request(caplog):
    secret = "very-secret-token"

    class Session:
        def get(self, *args, **kwargs):
            return FakeResponse(400, {"error": {
                "type": "OAuthException",
                "code": 100,
                "error_subcode": 33,
                "message": f"Unsupported get request. access_token={secret}",
            }})

    with pytest.raises(InstagramInsightsRequestError) as caught:
        InstagramInsightsClient(secret, Session()).discover_recent_media("178900000000001")

    diagnostic = str(caught.value)
    assert "type=OAuthException" in diagnostic
    assert "code=100" in diagnostic
    assert "error_subcode=33" in diagnostic
    assert "message=Unsupported get request. access_token=[REDACTED]" in diagnostic
    assert "endpoint=https://graph.facebook.com/v22.0/178900000000001/media" in diagnostic
    assert "api_version=v22.0" in diagnostic
    assert secret not in diagnostic
    assert secret not in caplog.text

    class AuthenticationSession:
        def get(self, *args, **kwargs):
            return FakeResponse(400, {"error": {"code": 190, "message": secret}})

    with pytest.raises(InstagramInsightsAuthenticationError) as authentication_error:
        InstagramInsightsClient(secret, AuthenticationSession()).fetch_media_insights("media-123456")
    assert "replace or refresh" in str(authentication_error.value)
    assert secret not in str(authentication_error.value)
    assert secret not in caplog.text

    class PermissionSession:
        def get(self, *args, **kwargs):
            return FakeResponse(400, {"error": {"code": 200, "message": secret}})

    with pytest.raises(InstagramInsightsPermissionError) as permission_error:
        InstagramInsightsClient(secret, PermissionSession()).fetch_media_insights("media-123456")
    assert "instagram_manage_insights" in str(permission_error.value)
    assert secret not in str(permission_error.value)
