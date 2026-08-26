from __future__ import annotations

from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).parents[1]
WORKFLOW_PATH = (
    REPOSITORY_ROOT
    / ".github"
    / "workflows"
    / "r2_integration_verification.yml"
)
JOB_NAME = "verify-r2-conditional-writes"
CONFIRMATION = "RUN_R2_INTEGRATION"
R2_SECRET_NAMES = {
    "CLOUDFLARE_R2_ACCOUNT_ID",
    "CLOUDFLARE_R2_ACCESS_KEY_ID",
    "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
    "CLOUDFLARE_R2_BUCKET_NAME",
}


def _workflow() -> dict:
    with WORKFLOW_PATH.open(encoding="utf-8") as workflow_file:
        return yaml.safe_load(workflow_file)


def _job() -> dict:
    workflow = _workflow()
    assert set(workflow["jobs"]) == {JOB_NAME}
    return workflow["jobs"][JOB_NAME]


def _steps_by_name() -> dict[str, dict]:
    return {step["name"]: step for step in _job()["steps"]}


def test_r2_workflow_has_only_an_explicit_manual_trigger():
    workflow = _workflow()

    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert "concurrency" not in workflow
    dispatch = workflow["on"]["workflow_dispatch"]
    confirmation = dispatch["inputs"]["confirm_r2_integration"]
    assert confirmation["required"] is True
    assert confirmation["type"] == "string"
    assert "default" not in confirmation

    job_condition = _job()["if"]
    assert "github.event_name == 'workflow_dispatch'" in job_condition
    assert (
        f"github.event.inputs.confirm_r2_integration == '{CONFIRMATION}'"
        in job_condition
    )


def test_r2_workflow_uses_python_310_and_the_hashed_development_lock():
    steps = _steps_by_name()
    setup = steps["Set up Python 3.10"]
    install = steps["Install locked development dependencies"]

    assert setup["uses"] == "actions/setup-python@v5"
    assert setup["with"]["python-version"] == "3.10"
    assert setup["with"]["cache-dependency-path"] == "requirements-dev.lock"
    assert install["run"] == (
        "python -m pip install --require-hashes -r requirements-dev.lock"
    )


def test_r2_workflow_gates_live_mutation_on_preflight_and_secret_presence():
    steps = _job()["steps"]
    names = [step["name"] for step in steps]
    preflight_index = names.index("Run offline R2 safety preflight")
    secret_check_index = names.index("Verify required R2 secrets are configured")
    live_index = names.index("Run live R2 conditional-write integration suite")

    assert preflight_index < secret_check_index < live_index
    assert steps[preflight_index]["run"] == (
        "python -m pytest -q tests/test_r2_integration_safety.py"
    )
    assert set(steps[secret_check_index]["env"]) == R2_SECRET_NAMES
    for name in R2_SECRET_NAMES:
        assert steps[secret_check_index]["env"][name] == (
            f"${{{{ secrets.{name} }}}}"
        )
    assert "exit 1" in steps[secret_check_index]["run"]


def test_live_opt_in_and_secrets_are_scoped_to_the_intended_steps():
    steps = _job()["steps"]
    live = _steps_by_name()["Run live R2 conditional-write integration suite"]

    assert live["env"]["ARTFOLIO_RUN_R2_INTEGRATION"] == "1"
    assert set(live["env"]) == R2_SECRET_NAMES | {
        "ARTFOLIO_RUN_R2_INTEGRATION"
    }
    for name in R2_SECRET_NAMES:
        assert live["env"][name] == f"${{{{ secrets.{name} }}}}"
    for step in steps:
        if step["name"] == live["name"]:
            continue
        assert "ARTFOLIO_RUN_R2_INTEGRATION" not in step.get("env", {})


def test_workflow_runs_only_the_explicit_live_suite_and_no_publication_code():
    steps = _job()["steps"]
    live = _steps_by_name()["Run live R2 conditional-write integration suite"]

    assert live["run"] == (
        "python -m pytest -q -s "
        "tests/integration/test_r2_conditional_writes.py"
    )
    run_commands = "\n".join(step.get("run", "") for step in steps)
    assert "main.py" not in run_commands
    assert "delete" not in run_commands.casefold()


def test_r2_secrets_are_not_workflow_or_job_global():
    workflow = _workflow()
    job = _job()

    assert "env" not in workflow
    assert "env" not in job
    for step in job["steps"]:
        environment = step.get("env", {})
        secret_names = set(environment) & R2_SECRET_NAMES
        if secret_names:
            assert step["name"] in {
                "Verify required R2 secrets are configured",
                "Run live R2 conditional-write integration suite",
            }
            assert secret_names == R2_SECRET_NAMES
