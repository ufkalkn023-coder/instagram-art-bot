"""Repository secret boundaries for the production and Insights workflows."""

from pathlib import Path

import pytest
import yaml

from src.instagram_insights import InstagramInsightsClient, InstagramInsightsConfigurationError
from src.production_config import (
    ProductionConfigurationError,
    validate_production_configuration,
    validate_reconciliation_configuration,
)


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
PUBLICATION_WORKFLOWS = ("instagram_bot.yml", "instagram_reels.yml")
LEGACY_SECRET = "${{ secrets.INSTAGRAM_ACCESS_TOKEN }}"
PUBLICATION_SECRET = "${{ secrets.INSTAGRAM_ACCESS_TOKEN_V2 }}"
INSIGHTS_SECRET = "${{ secrets.INSTAGRAM_INSIGHTS_ACCESS_TOKEN }}"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_no_workflow_consumes_the_legacy_repository_secret():
    for path in WORKFLOWS.glob("*.yml"):
        assert LEGACY_SECRET not in path.read_text(encoding="utf-8"), path.name


@pytest.mark.parametrize("name", PUBLICATION_WORKFLOWS)
def test_every_publication_token_mapping_uses_v2_without_legacy_fallback(name: str):
    workflow = _workflow(name)
    mappings = [
        step["env"]["INSTAGRAM_ACCESS_TOKEN"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "INSTAGRAM_ACCESS_TOKEN" in step.get("env", {})
    ]

    assert mappings
    assert set(mappings) == {PUBLICATION_SECRET}
    assert LEGACY_SECRET not in (WORKFLOWS / name).read_text(encoding="utf-8")
    assert "INSTAGRAM_ACCESS_TOKEN" not in workflow.get("env", {})


def test_insights_token_mapping_is_separate_from_publication_and_legacy():
    workflow = _workflow("instagram_insights.yml")
    collector = workflow["jobs"]["collect-insights"]["steps"][-1]

    assert collector["run"] == "python scripts/collect_insights.py"
    assert collector["env"]["INSTAGRAM_ACCESS_TOKEN"] == INSIGHTS_SECRET
    assert "INSTAGRAM_ACCESS_TOKEN" not in workflow.get("env", {})
    text = (WORKFLOWS / "instagram_insights.yml").read_text(encoding="utf-8")
    assert LEGACY_SECRET not in text
    assert PUBLICATION_SECRET not in text


def test_missing_v2_token_fails_publication_configuration_even_with_legacy_name():
    # A missing GitHub secret becomes an empty step environment value.
    environment = {"INSTAGRAM_ACCESS_TOKEN": ""}
    with pytest.raises(ProductionConfigurationError, match="INSTAGRAM_ACCESS_TOKEN"):
        validate_production_configuration(environment)
    with pytest.raises(ProductionConfigurationError, match="INSTAGRAM_ACCESS_TOKEN"):
        validate_reconciliation_configuration(environment)


def test_missing_insights_token_fails_before_an_api_request(monkeypatch):
    monkeypatch.delenv("INSTAGRAM_ACCESS_TOKEN", raising=False)
    with pytest.raises(InstagramInsightsConfigurationError, match="INSTAGRAM_ACCESS_TOKEN"):
        InstagramInsightsClient()
