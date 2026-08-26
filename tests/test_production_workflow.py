from __future__ import annotations

from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "instagram_bot.yml"
JOB_NAME = "post-to-instagram"
SCHEDULE_FLAG = "ARTFOLIO_PRODUCTION_SCHEDULE_ENABLED"
CONFIRMATION = "PUBLISH_TO_INSTAGRAM"
SINGLE_CRON = "0 0,3,6,9,15,18 * * *"
CAROUSEL_CRON = "0 12,21 * * *"


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


def test_manual_dispatch_requires_explicit_mode_and_exact_confirmation():
    dispatch = _workflow()["on"]["workflow_dispatch"]
    assert set(dispatch["inputs"]) == {"publish_mode", "confirm_publish"}

    publish_mode = dispatch["inputs"]["publish_mode"]
    assert publish_mode["required"] is True
    assert publish_mode["type"] == "choice"
    assert publish_mode["options"] == ["single", "carousel"]
    assert "auto" not in publish_mode["options"]

    confirmation = dispatch["inputs"]["confirm_publish"]
    assert confirmation["required"] is True
    assert confirmation["type"] == "string"
    assert CONFIRMATION in confirmation["description"]
    assert "REAL Instagram post" in confirmation["description"]
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


def test_schedule_crons_and_commands_remain_deterministic():
    schedules = _workflow()["on"]["schedule"]
    assert schedules == [{"cron": SINGLE_CRON}, {"cron": CAROUSEL_CRON}]

    publish = _steps_by_name()["Fetch artwork, process image, and post to Instagram"]
    script = publish["run"]
    assert f'"{SINGLE_CRON}") python main.py --mode single ;;' in script
    assert f'"{CAROUSEL_CRON}") python main.py --mode carousel ;;' in script
    assert '*) echo "Unsupported production schedule: $SCHEDULE_EXPRESSION" >&2; exit 2 ;;' in script


def test_manual_modes_map_exactly_and_never_invoke_auto_mode():
    publish = _steps_by_name()["Fetch artwork, process image, and post to Instagram"]
    assert publish["env"]["PUBLISH_MODE"] == "${{ github.event.inputs.publish_mode }}"

    script = publish["run"]
    assert '"single") python main.py --mode single ;;' in script
    assert '"carousel") python main.py --mode carousel ;;' in script
    assert '*) echo "Unsupported manual publish mode: $PUBLISH_MODE" >&2; exit 2 ;;' in script
    assert "--force-carousel" not in script
    assert "python main.py\n" not in script
    assert "python main.py\r\n" not in script


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
