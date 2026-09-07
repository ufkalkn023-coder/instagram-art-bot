from __future__ import annotations

from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "instagram_bot.yml"
JOB_NAME = "post-to-instagram"
SCHEDULE_FLAG = "ARTFOLIO_PRODUCTION_SCHEDULE_ENABLED"
CONFIRMATION = "PUBLISH_TO_INSTAGRAM"
CAROUSEL_CRON = "0 5,10,15,20 * * *"
EXPECTED_PRODUCTION_SECRET_NAMES = {
    "INSTAGRAM_ACCOUNT_ID",
    "INSTAGRAM_ACCESS_TOKEN",
    "CLOUDFLARE_R2_ACCOUNT_ID",
    "CLOUDFLARE_R2_ACCESS_KEY_ID",
    "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
    "CLOUDFLARE_R2_BUCKET_NAME",
    "CLOUDFLARE_R2_PUBLIC_URL",
}
RIGHTS_POLICY_VARIABLE = "ARTFOLIO_RIGHTS_POLICY"
PRODUCTION_RIGHTS_POLICY = "strict_public_domain"


def _workflow() -> dict:
    with WORKFLOW_PATH.open(encoding="utf-8") as workflow_file:
        return yaml.safe_load(workflow_file)


def _job() -> dict:
    workflow = _workflow()
    assert set(workflow["jobs"]) == {JOB_NAME}
    return workflow["jobs"][JOB_NAME]


def _steps_by_name() -> dict[str, dict]:
    return {step["name"]: step for step in _job()["steps"]}


def _normalized(expression: str) -> str:
    return " ".join(expression.split())


def test_manual_dispatch_is_carousel_only_and_requires_exact_confirmation():
    dispatch = _workflow()["on"]["workflow_dispatch"]
    assert set(dispatch["inputs"]) == {"confirm_publish"}

    confirmation = dispatch["inputs"]["confirm_publish"]
    assert confirmation["required"] is True
    assert confirmation["type"] == "string"
    assert CONFIRMATION in confirmation["description"]
    assert "REAL Instagram carousel" in confirmation["description"]
    assert "default" not in confirmation


def test_job_condition_independently_gates_schedule_and_manual_publishing():
    condition = _normalized(_job()["if"])

    assert condition == _normalized(
        """
        (github.event_name == 'schedule' &&
         vars.ARTFOLIO_PRODUCTION_SCHEDULE_ENABLED == 'true') ||
        (github.event_name == 'workflow_dispatch' &&
         github.event.inputs.confirm_publish == 'PUBLISH_TO_INSTAGRAM')
        """
    )
    assert f"vars.{SCHEDULE_FLAG} == 'true'" in condition
    assert f"github.event.inputs.confirm_publish == '{CONFIRMATION}'" in condition

    schedule_branch, manual_branch = condition.split(" || ", maxsplit=1)
    assert "workflow_dispatch" not in schedule_branch
    assert "confirm_publish" not in schedule_branch
    assert SCHEDULE_FLAG not in manual_branch
    assert "schedule" not in manual_branch


def test_schedule_has_exactly_four_daily_utc_carousel_runs():
    schedules = _workflow()["on"]["schedule"]
    assert schedules == [{"cron": CAROUSEL_CRON}]
    assert CAROUSEL_CRON.split()[1].split(",") == ["5", "10", "15", "20"]

    publish = _steps_by_name()["Fetch artwork, process image, and post to Instagram"]
    assert publish["run"] == "python main.py --mode carousel"


def test_all_production_invocations_are_carousel_only():
    publish = _steps_by_name()["Fetch artwork, process image, and post to Instagram"]
    script = publish["run"]
    assert "single" not in script
    assert "--force-carousel" not in script
    assert script == "python main.py --mode carousel"


def test_production_workflow_retains_operational_safety_gates():
    workflow = _workflow()
    job = _job()
    steps = _steps_by_name()

    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "instagram-bot",
        "cancel-in-progress": False,
    }
    assert job["timeout-minutes"] == 45
    assert steps["Install dependencies"]["run"] == (
        "python -m pip install --require-hashes -r requirements-dev.lock"
    )
    assert "git diff --exit-code -- requirements.lock requirements-dev.lock" in (
        steps["Check dependency lock drift"]["run"]
    )
    assert steps["Compile validation"]["run"] == (
        "python -m compileall -q main.py src scripts tests"
    )
    assert steps["Run test suite"]["run"] == "pytest -q"
    assert steps["Validate production configuration"]["run"] == (
        "python main.py --validate-production-config"
    )


def test_production_workflow_sets_strict_rights_policy_and_keeps_secrets_unchanged():
    steps = _steps_by_name()
    validation_environment = steps["Validate production configuration"]["env"]
    publish_environment = steps[
        "Fetch artwork, process image, and post to Instagram"
    ]["env"]

    assert set(validation_environment) == EXPECTED_PRODUCTION_SECRET_NAMES | {
        RIGHTS_POLICY_VARIABLE
    }
    assert EXPECTED_PRODUCTION_SECRET_NAMES.issubset(publish_environment)
    assert validation_environment[RIGHTS_POLICY_VARIABLE] == PRODUCTION_RIGHTS_POLICY
    assert publish_environment[RIGHTS_POLICY_VARIABLE] == PRODUCTION_RIGHTS_POLICY
    for variable in EXPECTED_PRODUCTION_SECRET_NAMES:
        expected = "${{ secrets." + variable + " }}"
        assert validation_environment[variable] == expected
        assert publish_environment[variable] == expected
    assert all("keychain" not in step.get("run", "").lower() for step in steps.values())
