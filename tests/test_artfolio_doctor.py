import json
import plistlib
import subprocess
from datetime import datetime, timedelta, timezone

import scripts.artfolio_doctor as doctor
from scripts.artfolio_doctor import (
    CheckResult,
    CredentialContext,
    DoctorReport,
    Status,
)
from src.local_credentials import (
    COLLECTOR_PROFILE,
    ENGAGEMENT_AUDIT_PROFILE,
    credential_variables,
)
from src.production_config import REQUIRED_PRODUCTION_VARIABLES
from src.rights_policy import RIGHTS_POLICY_ENV


def _check(status: Status) -> CheckResult:
    return CheckResult(status, status.value.lower())


def _credential_context(*, role_collision: bool = False) -> CredentialContext:
    collector_status = {
        variable: True for variable in credential_variables(COLLECTOR_PROFILE)
    }
    audit_status = {
        variable: True for variable in credential_variables(ENGAGEMENT_AUDIT_PROFILE)
    }
    return CredentialContext(
        collector_environment={},
        audit_environment={},
        collector_status=collector_status,
        audit_status=audit_status,
        role_collision=role_collision,
    )


def test_overall_aggregation_healthy():
    assert (
        doctor.aggregate_status([_check(Status.HEALTHY), _check(Status.SKIPPED)])
        is Status.HEALTHY
    )


def test_overall_aggregation_degraded():
    assert (
        doctor.aggregate_status([_check(Status.HEALTHY), _check(Status.DEGRADED)])
        is Status.DEGRADED
    )


def test_overall_aggregation_critical():
    assert (
        doctor.aggregate_status([_check(Status.DEGRADED), _check(Status.CRITICAL)])
        is Status.CRITICAL
    )


def test_json_output_is_valid_and_has_stable_top_level_shape():
    report = DoctorReport(
        mode="quick",
        checks={
            "repository": CheckResult(
                Status.DEGRADED,
                "dirty",
                {"clean": False},
                (
                    doctor.Issue(
                        Status.DEGRADED,
                        "repository",
                        "WORKTREE_DIRTY",
                        "Working tree has one changed path",
                    ),
                ),
            )
        },
    )

    payload = json.loads(doctor.render_json(report))

    assert payload["overall"] == payload["status"] == "DEGRADED"
    assert payload["mode"] == "quick"
    assert payload["checks"]["repository"]["status"] == "DEGRADED"
    assert payload["issues"][0]["code"] == "WORKTREE_DIRTY"


def test_output_never_contains_production_secret_values():
    secrets = {
        name: f"unique-secret-value-{index}"
        for index, name in enumerate(REQUIRED_PRODUCTION_VARIABLES)
    }
    environment = {
        **secrets,
        RIGHTS_POLICY_ENV: "strict_public_domain",
    }
    production = doctor.check_production_configuration(environment)
    report = DoctorReport(mode="quick", checks={"production_config": production})

    output = doctor.render_json(report) + doctor.render_text(report)

    assert production.status is Status.HEALTHY
    assert all(secret not in output for secret in secrets.values())


def test_quick_mode_skips_external_source_probes(monkeypatch):
    monkeypatch.setattr(
        doctor,
        "get_museum_adapters",
        lambda: (_ for _ in ()).throw(AssertionError("source probe ran")),
    )

    result = doctor.check_sources(quick=True)

    assert result.status is Status.SKIPPED
    assert result.details["probes_run"] == 0


def test_permissive_rights_policy_is_critical():
    environment = {
        **{name: "configured" for name in REQUIRED_PRODUCTION_VARIABLES},
        RIGHTS_POLICY_ENV: "permissive",
    }

    result = doctor.check_production_configuration(environment)

    assert result.status is Status.CRITICAL
    assert result.details["permissive_rights_policy_active"] is True
    assert result.issues[0].code == "INVALID_PRODUCTION_CONFIG"


def test_credential_role_collision_fails_before_r2_access(monkeypatch):
    def fake_load(profile, environment):
        for variable in credential_variables(profile):
            environment[variable] = (
                "same-access-key"
                if variable == "CLOUDFLARE_R2_ACCESS_KEY_ID"
                else "same-secret-key"
                if variable == "CLOUDFLARE_R2_SECRET_ACCESS_KEY"
                else "configured"
            )
        return {variable: True for variable in credential_variables(profile)}

    monkeypatch.setattr(doctor, "load_keychain_credentials", fake_load)
    monkeypatch.setattr(
        doctor,
        "active_collector_r2_credential_matches_audit_profile",
        lambda environment: False,
    )
    monkeypatch.setattr(
        doctor,
        "InsightsStorage",
        lambda: (_ for _ in ()).throw(AssertionError("R2 access attempted")),
    )

    credentials = doctor.collect_credentials({})
    result, data = doctor.check_r2(credentials)

    assert credentials.role_collision is True
    assert result.status is Status.CRITICAL
    assert {issue.code for issue in result.issues} == {"R2_ROLE_COLLISION"}
    assert data.history is None


def test_collector_stale_beyond_critical_threshold(tmp_path):
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    log_path = tmp_path / "collector.log"
    success = now - timedelta(hours=7)
    logged_success = success.astimezone()
    log_path.write_text(
        f"{logged_success:%Y-%m-%d %H:%M:%S},000 INFO collector: "
        "[insights] media_discovered=1\n",
        encoding="utf-8",
    )
    instagram = CheckResult(Status.HEALTHY, "ok", {"read_access": "OK"})
    r2 = CheckResult(
        Status.HEALTHY,
        "ok",
        {
            "collector_read_access": "OK",
            "write_permission": "CONFIGURED_PERMISSION_NOT_PROVEN",
        },
    )

    result = doctor.check_collector(
        _credential_context(),
        instagram,
        r2,
        log_path=log_path,
        now=now,
    )

    assert result.status is Status.CRITICAL
    assert result.details["freshness"] == "CRITICAL"
    assert "COLLECTOR_STALE_CRITICAL" in {issue.code for issue in result.issues}


def test_unproven_r2_write_permission_does_not_degrade_healthy_collector(tmp_path):
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    log_path = tmp_path / "collector.log"
    success = now - timedelta(hours=1)
    logged_success = success.astimezone()
    log_path.write_text(
        f"{logged_success:%Y-%m-%d %H:%M:%S},000 INFO collector: "
        "[insights] media_discovered=1\n",
        encoding="utf-8",
    )
    instagram = CheckResult(Status.HEALTHY, "ok", {"read_access": "OK"})
    r2 = CheckResult(
        Status.HEALTHY,
        "ok",
        {
            "collector_read_access": "OK",
            "write_permission": "CONFIGURED_PERMISSION_NOT_PROVEN",
        },
    )

    result = doctor.check_collector(
        _credential_context(),
        instagram,
        r2,
        log_path=log_path,
        now=now,
    )

    assert result.status is Status.HEALTHY
    assert result.details["r2_write_permission"] == "CONFIGURED_PERMISSION_NOT_PROVEN"


def test_launchagent_configuration_drift_is_degraded(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    plist_path = tmp_path / "collector.plist"
    payload = doctor.launchd_payload(repo_root=root)
    payload["StartInterval"] = 60
    with plist_path.open("wb") as destination:
        plistlib.dump(payload, destination)

    def loaded_runner(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout="state = not running\nruns = 4\nlast exit code = 0\n",
        )

    result = doctor.check_launchagent(
        root=root,
        plist_path=plist_path,
        platform="darwin",
        runner=loaded_runner,
    )

    assert result.status is Status.DEGRADED
    assert result.details["loaded"] is True
    assert result.details["start_interval"] == 60
    assert result.details["configuration_mismatches"] == ["StartInterval"]


def test_stale_publishing_is_critical_without_reconciliation():
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    started = now - doctor.PUBLISHING_RECONCILIATION_GRACE - timedelta(minutes=1)
    history = {
        "posted_artworks": [
            {
                "id": "aic_1",
                "publication_id": "publication-1",
                "status": "PUBLISHING",
                "publish_started_at": started.isoformat(),
            }
        ]
    }

    result = doctor.check_publication_lifecycle(history, now=now)

    assert result.status is Status.CRITICAL
    assert result.details["stale_publishing"] == 1
    assert "STALE_PUBLISHING" in {issue.code for issue in result.issues}


def test_doctor_source_has_no_production_mutation_calls():
    source = (doctor.ROOT / "scripts" / "artfolio_doctor.py").read_text(
        encoding="utf-8"
    )

    for mutation in (
        ".put_object(",
        ".delete_object(",
        "media_publish(",
        "reconcile_publications(",
        "configure_keychain(",
        "launchctl bootstrap",
        "launchctl bootout",
    ):
        assert mutation not in source
