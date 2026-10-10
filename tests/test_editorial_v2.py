import json
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from src import editorial_v2 as v2
from src.models import NormalizedArtwork


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "work.jpg"
    Image.new("RGB", (800, 1000), "navy").save(path)
    artwork = NormalizedArtwork(
        source="cleveland",
        source_id="1",
        title="Woman Reading",
        artist_name="Artist unknown",
        museum_name="Cleveland Museum of Art",
        description="A woman sits beside a window with an open book.",
        artwork_url="https://www.clevelandart.org/art/1",
        is_public_domain=True,
        rights_status="CONFIRMED_OPEN_ACCESS",
        license="CC0",
    )
    return v2.SourceArtwork(artwork=artwork, image_path=str(path))


def candidate(title="Beyond the Page", **changes):
    data = dict(
        public_title=title,
        editorial_angle="Reading and quiet attention",
        evidence=[
            dict(
                artwork_id="cleveland_1",
                kind="visual",
                statement="A seated figure holds an open book.",
            )
        ],
        visual_supported=True,
        confidence=0.9,
        cover_style="artwork_first",
        focus=None,
    )
    data.update(changes)
    return v2.HeadlineCandidate.model_validate(data)


def test_public_title_is_separate_from_registry_theme(source):
    plan = v2.select_plan([source], "women_reading", "Women Reading", [candidate()])
    assert plan.theme_id == "women_reading"
    assert plan.theme_title == "Women Reading"
    assert plan.public_title == "Beyond the Page"
    assert plan.status == "ai_selected"
    assert plan.manual_review_required is True


@pytest.mark.parametrize(
    "change, reason",
    [
        ({"visual_supported": False}, "visual_review_failed"),
        ({"confidence": 0.3}, "low_confidence"),
        ({"public_title": "Timeless Beauty"}, "generic_title"),
        (
            {
                "evidence": [
                    dict(artwork_id="cleveland_999", kind="visual", statement="A book.")
                ]
            },
            "unknown_artwork",
        ),
        (
            {
                "evidence": [
                    dict(
                        artwork_id="cleveland_1",
                        kind="museum",
                        statement="Royal patronage",
                        source_quote="Commissioned by a king.",
                    )
                ]
            },
            "unverified_source_quote",
        ),
    ],
)
def test_failed_evidence_returns_factual_fallback(source, change, reason):
    plan = v2.select_plan(
        [source], "women_reading", "Women Reading", [candidate(**change)]
    )
    assert plan.public_title == "Woman Reading"
    assert plan.editorial_angle == ""
    assert plan.status == "factual_fallback"
    assert reason in plan.rejections
    assert plan.focus is None


def test_near_duplicate_title_is_rejected_against_last_100(source):
    plan = v2.select_plan(
        [source],
        "reading",
        "Reading",
        [candidate("Beyond, the Page!")],
        history=["Beyond the Page"],
    )
    assert "repeated_title" in plan.rejections
    assert plan.status == "factual_fallback"


def test_museum_quote_must_be_real_source_text(source):
    evidence = [
        dict(
            artwork_id="cleveland_1",
            kind="museum",
            statement="An open book",
            source_quote="an open book",
        )
    ]
    plan = v2.select_plan(
        [source], "reading", "Reading", [candidate(evidence=evidence)]
    )
    assert plan.public_title == "Beyond the Page"


def test_unconfirmed_rights_cannot_enter_editorial_analysis(source):
    with pytest.raises(ValidationError, match="rights"):
        v2.SourceArtwork(
            artwork=source.artwork.model_copy(update={"is_public_domain": False}),
            image_path=source.image_path,
        )


def test_gemini_receives_image_bytes_and_cache_tracks_metadata(source, tmp_path):
    calls = []
    response_data = {
        "candidates": [
            candidate(f"Quiet Reading {word}").model_dump()
            for word in ("Today", "Again", "Together", "Within")
        ]
    }

    def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text=json.dumps(response_data), usage_metadata=None)

    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    provider = v2.GeminiEditorialProvider(
        client=client, cache_directory=tmp_path / "cache", max_calls=2
    )
    provider.analyze([source], "reading", "Reading")
    provider.analyze([source], "reading", "Reading")
    assert len(calls) == 1
    request = calls[0]
    assert request["model"] == "gemini-3.8-flash"
    assert request["config"].thinking_config.thinking_level.value == "MEDIUM"
    assert any(getattr(part, "inline_data", None) for part in request["contents"])
    changed = source.model_copy(
        update={
            "artwork": source.artwork.model_copy(
                update={"description": "New source text"}
            )
        }
    )
    provider.analyze([changed], "reading", "Reading")
    assert len(calls) == 2
    with pytest.raises(v2.EditorialProviderUnavailable, match="call_limit"):
        provider.analyze([source], "another", "Another")


def test_invalid_provider_response_is_not_cached(source, tmp_path):
    client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **_: SimpleNamespace(text='{"candidates": []}')
        )
    )
    provider = v2.GeminiEditorialProvider(
        client=client, cache_directory=tmp_path / "cache", max_calls=1
    )
    with pytest.raises(v2.EditorialProviderUnavailable):
        provider.analyze([source], "reading", "Reading")
    assert not list((tmp_path / "cache").glob("*.json"))


@pytest.mark.parametrize(
    "focus", [(0, 0, 1, 1), (0.9, 0, 0.1, 1), (0, 0, float("nan"), 1)]
)
def test_invalid_focus_is_rejected(focus):
    with pytest.raises(ValidationError):
        candidate(focus=focus)


def test_visual_review_requires_a_real_boolean():
    with pytest.raises(ValidationError):
        candidate(visual_supported="true")


def test_old_title_outside_history_window_is_not_rejected(source):
    plan = v2.select_plan(
        [source],
        "reading",
        "Reading",
        [candidate()],
        history=["Beyond the Page"] + [f"Other title {i}" for i in range(100)],
    )
    assert plan.status == "ai_selected"


@pytest.mark.parametrize("title", ["Timeless Beauty of Art", "A Journey Through Color"])
def test_generic_phrase_variants_are_rejected(source, title):
    plan = v2.select_plan([source], "reading", "Reading", [candidate(title)])
    assert "generic_title" in plan.rejections


def test_blocked_cache_is_bounded_provider_failure_before_call(source, tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("user data")
    provider = v2.GeminiEditorialProvider(
        client=None, cache_directory=blocked, max_calls=1
    )
    with pytest.raises(v2.EditorialProviderUnavailable, match="cache_unavailable"):
        provider.analyze([source], "reading", "Reading")
    assert provider.calls == 0
    assert blocked.read_text() == "user data"


def test_cache_write_failure_becomes_explicit_provider_fallback(
    source, tmp_path, monkeypatch
):
    from pathlib import Path

    data = {
        "candidates": [
            candidate(f"Quiet Reading {word}").model_dump()
            for word in ("Today", "Again", "Together", "Within")
        ]
    }
    client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **_: SimpleNamespace(text=json.dumps(data))
        )
    )
    provider = v2.GeminiEditorialProvider(
        client=client, cache_directory=tmp_path / "cache", max_calls=1
    )

    def fail_replace(*args):
        raise OSError("write failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(
        v2.EditorialProviderUnavailable, match="cache_persistence_failed"
    ):
        provider.analyze([source], "reading", "Reading")
    assert provider.calls == 1


def test_sdk_timeout_becomes_bounded_fallback(source, tmp_path):
    import httpx

    def timeout(**kwargs):
        raise httpx.ReadTimeout("private transport detail")

    client = SimpleNamespace(models=SimpleNamespace(generate_content=timeout))
    provider = v2.GeminiEditorialProvider(
        client=client, cache_directory=tmp_path / "cache", max_calls=1
    )
    with pytest.raises(
        v2.EditorialProviderUnavailable, match="invalid_or_unavailable_response"
    ):
        provider.analyze([source], "reading", "Reading")
    assert provider.calls == 1
    assert not list((tmp_path / "cache").glob("*.json"))
