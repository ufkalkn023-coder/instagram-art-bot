import requests
import pytest

from src.instagram_insights import (
    InstagramInsightsClient,
    InstagramInsightsPermissionError,
    InstagramInsightsRequestError,
    TARGET_METRICS,
)


class FakeResponse:
    def __init__(self, status_code, payload=None, json_error=False):
        self.status_code = status_code
        self.payload = payload
        self.json_error = json_error

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

    class PermissionSession:
        def get(self, *args, **kwargs):
            return FakeResponse(400, {"error": {"code": 190, "message": secret}})

    with pytest.raises(InstagramInsightsPermissionError) as permission_error:
        InstagramInsightsClient(secret, PermissionSession()).fetch_media_insights("media-123456")
    assert "instagram_manage_insights" in str(permission_error.value)
    assert secret not in str(permission_error.value)
    assert secret not in caplog.text
