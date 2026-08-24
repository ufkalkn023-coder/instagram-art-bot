from pathlib import Path


def test_insights_workflow_is_separate_and_uses_only_required_secrets():
    workflow = Path(".github/workflows/instagram_insights.yml").read_text()
    assert 'cron: "0 3 * * *"' in workflow
    assert 'python-version: "3.10"' in workflow
    assert "group: instagram-insights" in workflow
    assert "python scripts/collect_insights.py" in workflow
    assert "main.py" not in workflow
    for allowed in (
        "INSTAGRAM_ACCESS_TOKEN",
        "CLOUDFLARE_R2_ACCOUNT_ID",
        "CLOUDFLARE_R2_ACCESS_KEY_ID",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
        "CLOUDFLARE_R2_BUCKET_NAME",
    ):
        assert allowed in workflow
    for forbidden in ("GOOGLE_GEMINI_API_KEY", "MUSEUM", "PINTEREST", "INSTAGRAM_ACCOUNT_ID"):
        assert forbidden not in workflow
