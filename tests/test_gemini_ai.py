import config
from src import gemini_ai


def test_config_uses_the_stable_gemini_37_flash_model():
    assert config.GEMINI_MODEL == "gemini-3.7-flash"


def test_carousel_analysis_returns_fallback_signal_when_gemini_client_fails(monkeypatch):
    monkeypatch.setenv("GOOGLE_GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(gemini_ai.genai, "Client", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("unavailable")))

    assert gemini_ai.analyze_carousel("portrait", []) is None
