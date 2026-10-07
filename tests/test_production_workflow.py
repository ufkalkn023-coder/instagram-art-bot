from __future__ import annotations

from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "instagram_bot.yml"
JOB_NAME = "post-to-instagram"
SCHEDULE_FLAG = "ARTFOLIO_PRODUCTION_SCHEDULE_ENABLED"
CONFIRMATION = "PUBLISH_TO_INSTAGRAM"
CAROUSEL_CRON = "17 17 * * *"
EXPECTED_PRODUCTION_SECRET_NAMES = {
    "INSTAGRAM_ACCOUNT_ID",
    "INSTAGRAM_ACCESS_TOKEN",
    "CLOUDFLARE_R2_ACCOUNT_ID",
    "CLOUDFLARE_R2_ACCESS_KEY_ID",
    "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
    "CLOUDFLARE_R2_BUCKET_NAME",
    "CLOUDFLARE_R2_PUBLIC_URL",
    "CLOUDFLARE_STATE_R2_BUCKET_NAME",
    "CLOUDFLARE_STATE_R2_ACCESS_KEY_ID",
    "CLOUDFLARE_STATE_R2_SECRET_ACCESS_KEY",
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


def test_manual_dispatch_requires_exact_feed_confirmation():
    dispatch = _workflow()["on"]["workflow_dispatch"]
    assert set(dispatch["inputs"]) == {"confirm_publish", "authorization_id"}

    confirmation = dispatch["inputs"]["confirm_publish"]
    assert confirmation["required"] is True
    assert confirmation["type"] == "string"
    assert CONFIRMATION in confirmation["description"]
    assert "REAL Instagram Feed post" in confirmation["description"]
    assert "default" not in confirmation
    assert dispatch["inputs"]["authorization_id"]["required"] is True
    assert "default" not in dispatch["inputs"]["authorization_id"]


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


def test_schedule_has_one_daily_utc_eligibility_event():
    schedules = _workflow()["on"]["schedule"]
    assert schedules == [{"cron": CAROUSEL_CRON}]
    assert CAROUSEL_CRON.split() == ["17", "17", "*", "*", "*"]

    publish = _steps_by_name()["Fetch artwork, process image, and post to Instagram"]
    assert publish["run"] == "python main.py --mode auto"


def test_production_invocations_use_success_based_format_rotation():
    publish = _steps_by_name()["Fetch artwork, process image, and post to Instagram"]
    script = publish["run"]
    assert "--force-carousel" not in script
    assert script == "python main.py --mode auto"


def test_production_workflow_retains_operational_safety_gates():
    workflow = _workflow()
    job = _job()
    steps = _steps_by_name()

    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert workflow["concurrency"] == {
        "group": "instagram-bot",
        "cancel-in-progress": False,
    }
    assert job["timeout-minutes"] == 45
    assert "PRODUCTION_RERUN_PUBLICATION_BLOCKED" in steps[
        "Block publication on GitHub reruns"
    ]["run"]
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
    assert steps["Preflight normal Feed production"]["run"] == (
        "python main.py --preflight-feed"
    )


def test_production_workflow_sets_strict_rights_policy_and_fences_legacy_token():
    steps = _steps_by_name()
    validation_environment = steps["Preflight normal Feed production"]["env"]
    publish_environment = steps[
        "Fetch artwork, process image, and post to Instagram"
    ]["env"]

    assert set(validation_environment) == EXPECTED_PRODUCTION_SECRET_NAMES | {
        RIGHTS_POLICY_VARIABLE
    }
    assert EXPECTED_PRODUCTION_SECRET_NAMES.issubset(publish_environment)
    assert validation_environment[RIGHTS_POLICY_VARIABLE] == PRODUCTION_RIGHTS_POLICY
    assert publish_environment[RIGHTS_POLICY_VARIABLE] == PRODUCTION_RIGHTS_POLICY
    assert publish_environment["GITHUB_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    assert publish_environment["ARTFOLIO_MANUAL_AUTHORIZATION_ID"] == (
        "${{ vars.ARTFOLIO_MANUAL_AUTHORIZATION_ID }}"
    )
    assert publish_environment["ARTFOLIO_MANUAL_AUTHORIZATION_ISSUED_AT"] == (
        "${{ vars.ARTFOLIO_MANUAL_AUTHORIZATION_ISSUED_AT }}"
    )
    for variable in EXPECTED_PRODUCTION_SECRET_NAMES:
        secret = "INSTAGRAM_PUBLICATION_ACCESS_TOKEN" if variable == "INSTAGRAM_ACCESS_TOKEN" else variable
        expected = "${{ secrets." + secret + " }}"
        assert validation_environment[variable] == expected
        assert publish_environment[variable] == expected
    assert "${{ secrets.INSTAGRAM_ACCESS_TOKEN }}" not in WORKFLOW_PATH.read_text()
    assert all("keychain" not in step.get("run", "").lower() for step in steps.values())
