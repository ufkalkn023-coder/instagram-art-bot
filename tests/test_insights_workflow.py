from pathlib import Path

import yaml


EXPECTED_INSIGHTS_SECRETS = {
    "INSTAGRAM_ACCESS_TOKEN",
    "INSTAGRAM_ACCOUNT_ID",
    "CLOUDFLARE_R2_ACCOUNT_ID",
    "CLOUDFLARE_R2_ACCESS_KEY_ID",
    "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
    "CLOUDFLARE_R2_BUCKET_NAME",
}


def test_insights_workflow_is_separate_and_uses_only_required_secrets():
    workflow = Path(".github/workflows/instagram_insights.yml").read_text()
    assert 'cron: "0 3 * * *"' in workflow
    assert 'python-version: "3.10"' in workflow
    assert "group: instagram-insights" in workflow
    assert "python scripts/collect_insights.py" in workflow
    assert "--legacy-publications" not in workflow
    assert "INSTAGRAM_ACCOUNT_ID: ${{ secrets.INSTAGRAM_ACCOUNT_ID }}" in workflow
    assert "main.py" not in workflow
    for allowed in EXPECTED_INSIGHTS_SECRETS:
        assert allowed in workflow
    for forbidden in ("GOOGLE_GEMINI_API_KEY", "MUSEUM", "PINTEREST"):
        assert forbidden not in workflow


def test_insights_workflow_secret_names_are_unchanged_and_keychain_independent():
    workflow = yaml.safe_load(Path(".github/workflows/instagram_insights.yml").read_text())
    steps = workflow["jobs"]["collect-insights"]["steps"]
    collector = next(step for step in steps if step["name"] == "Collect Instagram Insights")

    assert set(collector["env"]) == EXPECTED_INSIGHTS_SECRETS
    assert all(
        value == "${{ secrets." + variable + " }}"
        for variable, value in collector["env"].items()
    )
    assert "keychain" not in collector["run"].lower()
