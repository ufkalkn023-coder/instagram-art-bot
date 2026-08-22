import config
from src import gemini_ai


class _FakeResponse:
    text = '{"caption":"Visual body.","alt_text":"Alt text.","hashtags":"#SpecificTag","art_movement":"Baroque","recommended_font_size":46}'


class _FakeCarouselResponse:
    text = '{"caption":"Carousel body.","hashtags":"#SpecificTag","recommended_font_size":46,"theme_title":"Theme"}'


def test_config_uses_the_stable_gemini_37_flash_model():
    assert config.GEMINI_MODEL == "gemini-3.7-flash"


def test_carousel_analysis_returns_fallback_signal_when_gemini_client_fails(monkeypatch):
    monkeypatch.setenv("GOOGLE_GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(gemini_ai.genai, "Client", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("unavailable")))

    assert gemini_ai.analyze_carousel("portrait", []) is None


def test_single_prompt_enforces_artfolio_visual_editorial_contract(monkeypatch, tmp_path):
    captured = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            captured.update(kwargs)
            return _FakeResponse()

    class FakeClient:
        models = FakeModels()

        def __init__(self, **kwargs):
            pass

    image_path = tmp_path / "art.jpg"
    image_path.write_bytes(b"image")
    monkeypatch.setenv("GOOGLE_GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(gemini_ai.genai, "Client", FakeClient)
    monkeypatch.setattr(gemini_ai.types.Part, "from_bytes", lambda **kwargs: "image-part")

    assert gemini_ai.analyze_artwork(
        str(image_path), "Artwork", "Artist", "1900", "Museum", content_type="DETAIL_FOCUS"
    )["caption"] == "Visual body."

    prompt = captured["contents"][1]
    for fragment in (
        "visual-first hook",
        "80–140 words",
        "mobile-readable",
        "short paragraphs",
        "SUPPLIED METADATA",
        "UNSUPPORTED CONTEXT",
        "DETAIL_FOCUS",
        "4–7 specific, relevant hashtags",
        "generic AI/art clichés",
        "No emojis",
    ):
        assert fragment.casefold() in prompt.casefold()


def test_carousel_prompt_uses_the_same_concise_grounded_editorial_voice(monkeypatch):
    captured = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            captured.update(kwargs)
            return _FakeCarouselResponse()

    class FakeClient:
        models = FakeModels()

        def __init__(self, **kwargs):
            pass

    monkeypatch.setenv("GOOGLE_GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(gemini_ai.genai, "Client", FakeClient)

    assert gemini_ai.analyze_carousel("portrait", [{"title": "Artwork", "artist": "Artist", "museum": "Museum"}])["caption"] == "Carousel body."

    prompt = captured["contents"][0]
    for fragment in (
        "visual-first hook",
        "80–140 words",
        "mobile-readable",
        "short paragraphs",
        "only factual grounding",
        "Do not invent artist intentions",
        "4–7 specific, relevant hashtags",
        "generic AI/art clichés",
        "NO EMOJIS",
    ):
        assert fragment.casefold() in prompt.casefold()
