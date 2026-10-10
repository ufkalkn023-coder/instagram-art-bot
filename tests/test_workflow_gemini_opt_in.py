"""A configured paid-provider secret must not silently enable Feed AI calls."""

from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("filename", ["instagram_bot.yml", "prepare_feed_queue.yml"])
def test_feed_workflows_withhold_gemini_key_without_explicit_opt_in(filename):
    root = Path(__file__).parents[1]
    workflow = yaml.safe_load((root / ".github/workflows" / filename).read_text())
    bindings = [step["env"]["GOOGLE_GEMINI_API_KEY"]
                for job in workflow["jobs"].values() for step in job["steps"]
                if "GOOGLE_GEMINI_API_KEY" in step.get("env", {})]
    assert bindings
    for binding in bindings:
        assert binding == "${{ vars.ARTFOLIO_GEMINI_ENABLED == 'true' && secrets.GOOGLE_GEMINI_API_KEY || '' }}"
