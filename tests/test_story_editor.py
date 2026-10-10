import pytest
from src.story_editor import validate_editor_request


def request(**changes):
    return validate_editor_request(
        **dict(
            host="127.0.0.1:38129",
            origin="http://127.0.0.1:38129",
            token="session-token",
            expected_token="session-token",
            port=38129,
            content_type="application/json",
            length="100",
            **changes,
        )
    )


def test_editor_accepts_same_origin_authenticated_json():
    assert request() == 100


@pytest.mark.parametrize(
    "changes",
    [
        dict(origin=None),
        dict(origin="https://evil.example"),
        dict(host="evil.example:38129"),
        dict(token=None),
        dict(token="wrong"),
    ],
)
def test_editor_rejects_foreign_or_unauthenticated_writes(changes):
    values = dict(
        host="127.0.0.1:38129",
        origin="http://127.0.0.1:38129",
        token="session-token",
        expected_token="session-token",
        port=38129,
        content_type="application/json",
        length="100",
    )
    values.update(changes)
    with pytest.raises(PermissionError):
        validate_editor_request(**values)


@pytest.mark.parametrize("length", [None, "", "-1", "0", "128001", "abc"])
def test_editor_rejects_unbounded_or_invalid_body(length):
    with pytest.raises(ValueError):
        validate_editor_request(
            host="localhost:38129",
            origin="http://localhost:38129",
            token="t",
            expected_token="t",
            port=38129,
            content_type="application/json",
            length=length,
        )
