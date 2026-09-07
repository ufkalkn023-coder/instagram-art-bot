#!/usr/bin/env python3
"""Run a unified, production-safe, read-only Artfolio health check."""

from __future__ import annotations

import argparse
import json
import logging
import os
import plistlib
import random
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.install_insights_launchd import (  # noqa: E402
    LABEL as LAUNCHAGENT_LABEL,
    PLIST_PATH as DEFAULT_PLIST_PATH,
    launchd_payload,
)
from src.art_fetcher import get_museum_adapters  # noqa: E402
from src.engagement_learning import (  # noqa: E402
    SNAPSHOT_CONFIDENCE,
    LearningConfig,
    analyze_engagement_learning,
)
from src.history_tracker import (  # noqa: E402
    PENDING_RESERVATION_TTL,
    PublicationStatus,
    _is_stale_pending,
    _parse_reserved_at,
    _publication_key,
)
from src.insights_health import freshness_state, inspect_collector_log  # noqa: E402
from src.insights_storage import InsightsStorage  # noqa: E402
from src.instagram_insights import (  # noqa: E402
    InstagramInsightsAuthenticationError,
    InstagramInsightsClient,
    InstagramInsightsConfigurationError,
    InstagramInsightsError,
    InstagramInsightsPermissionError,
)
from src.local_credentials import (  # noqa: E402
    COLLECTOR_PROFILE,
    ENGAGEMENT_AUDIT_PROFILE,
    active_collector_r2_credential_matches_audit_profile,
    load_keychain_credentials,
)
from src.production_config import (  # noqa: E402
    ProductionConfigurationError,
    validate_production_configuration,
)
from src.publication_reconciliation import (  # noqa: E402
    PUBLISHING_RECONCILIATION_GRACE,
)
from src.rights_policy import RIGHTS_POLICY_ENV, RightsPolicyMode  # noqa: E402

DEFAULT_COLLECTOR_LOG = (
    Path.home() / "Library" / "Logs" / "Artfolio" / "instagram-insights.log"
)
R2_VARIABLES = (
    "CLOUDFLARE_R2_ACCOUNT_ID",
    "CLOUDFLARE_R2_ACCESS_KEY_ID",
    "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
    "CLOUDFLARE_R2_BUCKET_NAME",
)
INSTAGRAM_VARIABLES = ("INSTAGRAM_ACCOUNT_ID", "INSTAGRAM_ACCESS_TOKEN")
LOW_LEARNING_CONFIDENCE = 0.10
MET_SOURCE_PROBE_LIMIT = 5


class Status(str, Enum):
    HEALTHY = "HEALTHY"
    SKIPPED = "SKIPPED"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"


STATUS_EXIT_CODES = {
    Status.HEALTHY: 0,
    Status.SKIPPED: 0,
    Status.DEGRADED: 1,
    Status.CRITICAL: 2,
}


@dataclass(frozen=True)
class Issue:
    severity: Status
    check: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity.value,
            "check": self.check,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class CheckResult:
    status: Status
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    issues: tuple[Issue, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "summary": self.summary,
            "details": self.details,
        }


@dataclass(frozen=True)
class DoctorReport:
    mode: str
    checks: dict[str, CheckResult]

    @property
    def overall(self) -> Status:
        return aggregate_status(self.checks.values())

    @property
    def issues(self) -> tuple[Issue, ...]:
        return tuple(
            issue for result in self.checks.values() for issue in result.issues
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "overall": self.overall.value,
            "status": self.overall.value,
            "mode": self.mode,
            "checks": {name: result.as_dict() for name, result in self.checks.items()},
            "issues": [issue.as_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class CredentialContext:
    collector_environment: dict[str, str]
    audit_environment: dict[str, str]
    collector_status: dict[str, bool]
    audit_status: dict[str, bool]
    role_collision: bool


@dataclass(frozen=True)
class ProductionData:
    history: dict[str, Any] | None = None
    snapshots: tuple[dict[str, Any], ...] = ()
    invalid_snapshots: int = 0


def aggregate_status(results: Iterable[CheckResult]) -> Status:
    statuses = {result.status for result in results}
    if Status.CRITICAL in statuses:
        return Status.CRITICAL
    if Status.DEGRADED in statuses:
        return Status.DEGRADED
    return Status.HEALTHY


def _issue(check: str, severity: Status, code: str, message: str) -> Issue:
    if severity not in {Status.DEGRADED, Status.CRITICAL}:
        raise ValueError("Issues must be DEGRADED or CRITICAL")
    return Issue(severity, check, code, message)


def _result(
    status: Status,
    summary: str,
    details: dict[str, Any],
    issues: Sequence[Issue] = (),
) -> CheckResult:
    return CheckResult(status, summary, details, tuple(issues))


def _run_command(
    arguments: Sequence[str],
    *,
    cwd: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
    )


def check_repository(
    *,
    root: Path = ROOT,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run_command,
) -> CheckResult:
    name = "repository"
    try:
        branch_result = runner(("git", "branch", "--show-current"), cwd=root)
        head_result = runner(("git", "rev-parse", "HEAD"), cwd=root)
        dirty_result = runner(
            ("git", "status", "--porcelain", "--untracked-files=normal"),
            cwd=root,
        )
    except (OSError, subprocess.SubprocessError):
        issue = _issue(
            name, Status.CRITICAL, "GIT_UNAVAILABLE", "Git state is unreadable"
        )
        return _result(Status.CRITICAL, "unreadable", {}, (issue,))

    if any(item.returncode for item in (branch_result, head_result, dirty_result)):
        issue = _issue(
            name, Status.CRITICAL, "GIT_UNAVAILABLE", "Git state is unreadable"
        )
        return _result(Status.CRITICAL, "unreadable", {}, (issue,))

    branch = branch_result.stdout.strip() or "DETACHED"
    head = head_result.stdout.strip()
    dirty_count = len(dirty_result.stdout.splitlines())
    divergence: dict[str, Any] = {"available": False, "ahead": None, "behind": None}
    issues: list[Issue] = []
    if dirty_count:
        issues.append(
            _issue(
                name,
                Status.DEGRADED,
                "WORKTREE_DIRTY",
                f"Working tree has {dirty_count} changed path(s)",
            )
        )

    try:
        origin_result = runner(
            ("git", "rev-parse", "--verify", "refs/remotes/origin/main"),
            cwd=root,
        )
        if origin_result.returncode == 0:
            count_result = runner(
                ("git", "rev-list", "--left-right", "--count", "HEAD...origin/main"),
                cwd=root,
            )
            values = count_result.stdout.split()
            if count_result.returncode == 0 and len(values) == 2:
                ahead, behind = (int(value) for value in values)
                divergence = {"available": True, "ahead": ahead, "behind": behind}
                if ahead or behind:
                    issues.append(
                        _issue(
                            name,
                            Status.DEGRADED,
                            "ORIGIN_DIVERGENCE",
                            f"HEAD is {ahead} ahead and {behind} behind origin/main",
                        )
                    )
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    status = Status.DEGRADED if issues else Status.HEALTHY
    clean = dirty_count == 0
    summary = f"branch={branch}, {'clean' if clean else 'dirty'}, HEAD={head[:12]}"
    return _result(
        status,
        summary,
        {
            "branch": branch,
            "clean": clean,
            "dirty_path_count": dirty_count,
            "head": head,
            "origin_main_divergence": divergence,
        },
        issues,
    )


def collect_credentials(
    environment: Mapping[str, str] | None = None,
) -> CredentialContext:
    base = dict(os.environ if environment is None else environment)
    collector_environment = dict(base)
    audit_environment = dict(base)
    collector_status = load_keychain_credentials(
        COLLECTOR_PROFILE,
        collector_environment,
    )
    audit_status = load_keychain_credentials(
        ENGAGEMENT_AUDIT_PROFILE,
        audit_environment,
    )
    collector_pair = tuple(
        collector_environment.get(name, "").strip() for name in R2_VARIABLES[1:3]
    )
    audit_pair = tuple(
        audit_environment.get(name, "").strip() for name in R2_VARIABLES[1:3]
    )
    direct_collision = all(collector_pair) and collector_pair == audit_pair
    profile_collision = active_collector_r2_credential_matches_audit_profile(
        collector_environment
    )
    return CredentialContext(
        collector_environment=collector_environment,
        audit_environment=audit_environment,
        collector_status=collector_status,
        audit_status=audit_status,
        role_collision=direct_collision or profile_collision,
    )


def check_production_configuration(environment: Mapping[str, str]) -> CheckResult:
    name = "production_config"
    configured_rights = environment.get(RIGHTS_POLICY_ENV, "").strip()
    details: dict[str, Any] = {
        "valid": False,
        "rights_policy": configured_rights or "MISSING",
        "strict_rights_policy_active": (
            configured_rights == RightsPolicyMode.STRICT_PUBLIC_DOMAIN.value
        ),
        "permissive_rights_policy_active": (
            configured_rights == RightsPolicyMode.PERMISSIVE.value
        ),
    }
    try:
        optional = validate_production_configuration(environment)
    except ProductionConfigurationError as error:
        issue = _issue(
            name,
            Status.CRITICAL,
            "INVALID_PRODUCTION_CONFIG",
            str(error),
        )
        return _result(Status.CRITICAL, "invalid", details, (issue,))
    details.update({"valid": True, "optional_integrations": optional})
    return _result(Status.HEALTHY, "valid; strict_public_domain active", details)


@contextmanager
def _selected_environment(environment: Mapping[str, str]) -> Iterator[None]:
    names = set(R2_VARIABLES) | set(INSTAGRAM_VARIABLES)
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            value = environment.get(name, "").strip()
            if value:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def check_instagram(credentials: CredentialContext) -> CheckResult:
    name = "instagram"
    complete = all(
        credentials.collector_status.get(item) for item in INSTAGRAM_VARIABLES
    )
    details: dict[str, Any] = {
        "credential_profile_complete": complete,
        "operation": "GET account/media discovery",
        "read_access": "NOT_CHECKED",
    }
    if not complete:
        issue = _issue(
            name,
            Status.CRITICAL,
            "INSTAGRAM_CREDENTIALS_INCOMPLETE",
            "Collector Instagram credential profile is incomplete",
        )
        return _result(Status.CRITICAL, "credentials incomplete", details, (issue,))
    try:
        response = InstagramInsightsClient(
            access_token=credentials.collector_environment["INSTAGRAM_ACCESS_TOKEN"]
        ).discover_recent_media(
            credentials.collector_environment["INSTAGRAM_ACCOUNT_ID"]
        )
    except InstagramInsightsAuthenticationError:
        code = "INSTAGRAM_AUTH_INVALID"
        message = "Instagram authentication is invalid"
    except InstagramInsightsPermissionError:
        code = "INSTAGRAM_PERMISSION_INVALID"
        message = "Instagram read permission is unavailable"
    except InstagramInsightsConfigurationError:
        code = "INSTAGRAM_CONFIG_INVALID"
        message = "Instagram read configuration is invalid"
    except InstagramInsightsError:
        code = "INSTAGRAM_READ_INACCESSIBLE"
        message = "Instagram GET-only discovery is inaccessible"
    except Exception:
        code = "INSTAGRAM_READ_FAILED"
        message = "Instagram GET-only discovery failed unexpectedly"
    else:
        details.update(
            {
                "read_access": "OK",
                "media_discovered": len(response.media),
                "api_calls": response.api_calls,
            }
        )
        return _result(Status.HEALTHY, "GET-only discovery OK", details)
    details["read_access"] = "INVALID"
    issue = _issue(name, Status.CRITICAL, code, message)
    return _result(Status.CRITICAL, "GET-only discovery failed", details, (issue,))


def check_r2(credentials: CredentialContext) -> tuple[CheckResult, ProductionData]:
    name = "r2"
    collector_complete = all(
        credentials.collector_status.get(item) for item in R2_VARIABLES
    )
    audit_complete = all(credentials.audit_status.get(item) for item in R2_VARIABLES)
    details: dict[str, Any] = {
        "collector_profile_complete": collector_complete,
        "audit_profile_complete": audit_complete,
        "collector_audit_role_collision": credentials.role_collision,
        "bucket_configured": bool(
            credentials.collector_status.get("CLOUDFLARE_R2_BUCKET_NAME")
            and credentials.audit_status.get("CLOUDFLARE_R2_BUCKET_NAME")
        ),
        "collector_read_access": "NOT_CHECKED",
        "audit_list_get_access": "NOT_CHECKED",
        "write_permission": (
            "INVALID_ROLE_COLLISION"
            if credentials.role_collision
            else "CONFIGURED_PERMISSION_NOT_PROVEN"
            if collector_complete
            else "MISSING"
        ),
        "operations": ["GetObject", "ListObjectsV2"],
    }
    issues: list[Issue] = []
    if not collector_complete:
        issues.append(
            _issue(
                name,
                Status.CRITICAL,
                "COLLECTOR_R2_CREDENTIALS_INCOMPLETE",
                "Collector R2 credential profile is incomplete",
            )
        )
    if not audit_complete:
        issues.append(
            _issue(
                name,
                Status.CRITICAL,
                "AUDIT_R2_CREDENTIALS_INCOMPLETE",
                "Engagement-audit R2 credential profile is incomplete",
            )
        )
    if credentials.role_collision:
        issues.append(
            _issue(
                name,
                Status.CRITICAL,
                "R2_ROLE_COLLISION",
                "Collector and engagement-audit R2 credentials collide",
            )
        )
    if issues:
        return _result(
            Status.CRITICAL, "credential profiles invalid", details, issues
        ), ProductionData()

    try:
        with _selected_environment(credentials.collector_environment):
            InsightsStorage().load_history()
    except Exception:
        details["collector_read_access"] = "INACCESSIBLE"
        issues.append(
            _issue(
                name,
                Status.CRITICAL,
                "COLLECTOR_R2_READ_INACCESSIBLE",
                "Collector profile cannot read production history from R2",
            )
        )
    else:
        details["collector_read_access"] = "OK"

    data = ProductionData()
    try:
        with _selected_environment(credentials.audit_environment):
            storage = InsightsStorage()
            history = storage.load_history()
            snapshots = storage.load_all_snapshots()
            invalid_snapshots = storage.last_snapshot_load_diagnostics.invalid_snapshots
    except Exception:
        details["audit_list_get_access"] = "INACCESSIBLE"
        issues.append(
            _issue(
                name,
                Status.CRITICAL,
                "AUDIT_R2_READ_INACCESSIBLE",
                "Audit profile cannot LIST/GET production analytics from R2",
            )
        )
    else:
        details.update(
            {
                "audit_list_get_access": "OK",
                "snapshot_partitions_loaded": (
                    storage.last_snapshot_load_diagnostics.partitions_loaded
                ),
            }
        )
        data = ProductionData(history, tuple(snapshots), invalid_snapshots)

    status = Status.CRITICAL if issues else Status.HEALTHY
    summary = (
        "LIST/GET read access OK; write permission unproven"
        if not issues
        else "read access failed"
    )
    return _result(status, summary, details, issues), data


def check_publication_lifecycle(
    history: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> CheckResult:
    name = "publication_lifecycle"
    if history is None:
        issue = _issue(
            name,
            Status.CRITICAL,
            "HISTORY_UNAVAILABLE",
            "Production publication history is unavailable",
        )
        return _result(Status.CRITICAL, "unavailable", {}, (issue,))
    records = history.get("posted_artworks")
    if not isinstance(records, list):
        issue = _issue(
            name,
            Status.CRITICAL,
            "HISTORY_MALFORMED",
            "Production posted_artworks history is malformed",
        )
        return _result(Status.CRITICAL, "malformed", {}, (issue,))

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("Doctor time must be timezone-aware")
    reference = reference.astimezone(timezone.utc)
    groups: dict[str, list[dict[str, Any]]] = {}
    malformed_records = 0
    for record in records:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("id"), str)
            or not record["id"].strip()
        ):
            malformed_records += 1
            continue
        groups.setdefault(_publication_key(record), []).append(record)

    counts = Counter({status.value: 0 for status in PublicationStatus})
    stale_pending = 0
    stale_publishing = 0
    invalid_units = 0
    for unit_records in groups.values():
        statuses = {
            str(record.get("status", "")).strip().upper() for record in unit_records
        }
        if statuses == {""}:
            counts[PublicationStatus.PUBLISHED.value] += 1
            continue
        if len(statuses) != 1:
            invalid_units += 1
            continue
        status_text = next(iter(statuses))
        try:
            status = PublicationStatus(status_text)
        except ValueError:
            invalid_units += 1
            continue
        counts[status.value] += 1
        if status is PublicationStatus.PENDING and all(
            _is_stale_pending(record, reference) for record in unit_records
        ):
            stale_pending += 1
        if status is PublicationStatus.PUBLISHING:
            started = _parse_reserved_at(
                unit_records[0].get(
                    "publish_started_at",
                    unit_records[0].get("publishing_at"),
                )
            )
            if (
                started is None
                or reference - started >= PUBLISHING_RECONCILIATION_GRACE
            ):
                stale_publishing += 1

    details = {
        "counts": dict(sorted(counts.items())),
        "publication_units": len(groups),
        "stale_pending": stale_pending,
        "stale_publishing": stale_publishing,
        "unresolved_ambiguous": counts[PublicationStatus.AMBIGUOUS.value],
        "pending_stale_after_seconds": int(PENDING_RESERVATION_TTL.total_seconds()),
        "publishing_stale_after_seconds": int(
            PUBLISHING_RECONCILIATION_GRACE.total_seconds()
        ),
        "invalid_units": invalid_units,
        "malformed_records": malformed_records,
    }
    issues: list[Issue] = []
    if malformed_records or invalid_units:
        issues.append(
            _issue(
                name,
                Status.CRITICAL,
                "LIFECYCLE_MALFORMED",
                "Publication lifecycle contains malformed or inconsistent units",
            )
        )
    if stale_publishing:
        issues.append(
            _issue(
                name,
                Status.CRITICAL,
                "STALE_PUBLISHING",
                f"{stale_publishing} publication unit(s) are stuck in PUBLISHING",
            )
        )
    if stale_pending:
        issues.append(
            _issue(
                name,
                Status.DEGRADED,
                "STALE_PENDING",
                f"{stale_pending} publication unit(s) have stale PENDING reservations",
            )
        )
    ambiguous = counts[PublicationStatus.AMBIGUOUS.value]
    if ambiguous:
        issues.append(
            _issue(
                name,
                Status.DEGRADED,
                "UNRESOLVED_AMBIGUOUS",
                f"{ambiguous} publication unit(s) remain AMBIGUOUS",
            )
        )
    status = aggregate_status([CheckResult(issue.severity, "") for issue in issues])
    summary = ", ".join(
        f"{key}={counts[key]}"
        for key in ("PENDING", "PUBLISHING", "PUBLISHED", "AMBIGUOUS", "EXPIRED")
    )
    return _result(status, summary, details, issues)


def check_launchagent(
    *,
    root: Path = ROOT,
    plist_path: Path = DEFAULT_PLIST_PATH,
    platform: str = sys.platform,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> CheckResult:
    name = "launchagent"
    if platform != "darwin":
        return _result(
            Status.SKIPPED,
            "not applicable on this platform",
            {"platform": platform},
        )
    details: dict[str, Any] = {
        "installed": plist_path.is_file(),
        "loaded": False,
    }
    if not plist_path.is_file():
        issue = _issue(
            name,
            Status.DEGRADED,
            "LAUNCHAGENT_MISSING",
            "Hourly Insights LaunchAgent is not installed",
        )
        return _result(Status.DEGRADED, "not installed", details, (issue,))
    try:
        with plist_path.open("rb") as source:
            payload = plistlib.load(source)
    except (OSError, plistlib.InvalidFileException):
        issue = _issue(
            name,
            Status.DEGRADED,
            "LAUNCHAGENT_UNREADABLE",
            "Insights LaunchAgent plist is unreadable",
        )
        return _result(Status.DEGRADED, "unreadable", details, (issue,))
    if not isinstance(payload, dict):
        issue = _issue(
            name,
            Status.DEGRADED,
            "LAUNCHAGENT_MALFORMED",
            "Insights LaunchAgent plist is malformed",
        )
        return _result(Status.DEGRADED, "malformed", details, (issue,))

    expected = launchd_payload(repo_root=root)
    arguments = payload.get("ProgramArguments")
    python_executable = (
        arguments[0]
        if isinstance(arguments, list) and arguments and isinstance(arguments[0], str)
        else None
    )
    script_path = (
        arguments[1]
        if isinstance(arguments, list)
        and len(arguments) > 1
        and isinstance(arguments[1], str)
        else None
    )
    mismatches = []
    if payload.get("RunAtLoad") is not True:
        mismatches.append("RunAtLoad")
    if payload.get("StartInterval") != 3600:
        mismatches.append("StartInterval")
    if payload.get("WorkingDirectory") != expected["WorkingDirectory"]:
        mismatches.append("WorkingDirectory")
    if script_path != expected["ProgramArguments"][1]:
        mismatches.append("script_path")
    if not python_executable or not Path(python_executable).is_file():
        mismatches.append("python_executable")

    try:
        launchctl = runner(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{LAUNCHAGENT_LABEL}"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        launchctl = None
    loaded = launchctl is not None and launchctl.returncode == 0
    output = launchctl.stdout if loaded else ""
    run_match = re.search(r"^\s*runs = (\d+)\s*$", output, re.MULTILINE)
    exit_match = re.search(r"^\s*last exit code = (-?\d+)\s*$", output, re.MULTILINE)
    state_match = re.search(r"^\s*state = (\S+)\s*$", output, re.MULTILINE)
    last_exit = int(exit_match.group(1)) if exit_match else None
    details.update(
        {
            "loaded": loaded,
            "state": state_match.group(1) if state_match else "UNKNOWN",
            "run_at_load": payload.get("RunAtLoad"),
            "start_interval": payload.get("StartInterval"),
            "python_executable": python_executable,
            "script_path": script_path,
            "working_directory": payload.get("WorkingDirectory"),
            "runs": int(run_match.group(1)) if run_match else None,
            "last_exit_code": last_exit,
            "configuration_mismatches": sorted(mismatches),
        }
    )
    issues: list[Issue] = []
    if not loaded:
        issues.append(
            _issue(
                name,
                Status.DEGRADED,
                "LAUNCHAGENT_NOT_LOADED",
                "Insights LaunchAgent is installed but not loaded",
            )
        )
    if mismatches:
        issues.append(
            _issue(
                name,
                Status.DEGRADED,
                "LAUNCHAGENT_CONFIG_DRIFT",
                "Insights LaunchAgent configuration differs from the repository contract",
            )
        )
    if last_exit not in {None, 0}:
        issues.append(
            _issue(
                name,
                Status.DEGRADED,
                "LAUNCHAGENT_LAST_RUN_FAILED",
                f"Insights LaunchAgent last exit code is {last_exit}",
            )
        )
    status = Status.DEGRADED if issues else Status.HEALTHY
    summary = "installed and loaded" if not issues else "requires attention"
    return _result(status, summary, details, issues)


def check_collector(
    credentials: CredentialContext,
    instagram: CheckResult,
    r2: CheckResult,
    *,
    log_path: Path = DEFAULT_COLLECTOR_LOG,
    now: datetime | None = None,
) -> CheckResult:
    name = "collector"
    profile_complete = all(credentials.collector_status.values())
    audit_complete = all(credentials.audit_status.values())
    log_health = inspect_collector_log(log_path)
    freshness, age_hours = freshness_state(
        log_health.last_success,
        now=now,
        consecutive_failures=log_health.consecutive_failures,
    )
    details = {
        "collector_credential_profile_complete": profile_complete,
        "audit_credential_profile_complete": audit_complete,
        "collector_audit_r2_role_collision": credentials.role_collision,
        "instagram_read_access": instagram.details.get("read_access", "NOT_CHECKED"),
        "r2_get_access": r2.details.get("collector_read_access", "NOT_CHECKED"),
        "bucket_configured": credentials.collector_status.get(
            "CLOUDFLARE_R2_BUCKET_NAME", False
        ),
        "r2_write_permission": r2.details.get("write_permission", "UNKNOWN"),
        "last_success": (
            log_health.last_success.isoformat().replace("+00:00", "Z")
            if log_health.last_success
            else None
        ),
        "success_age_hours": round(age_hours, 4) if age_hours is not None else None,
        "consecutive_failures": log_health.consecutive_failures,
        "freshness": freshness,
    }
    issues: list[Issue] = []
    if not profile_complete:
        issues.append(
            _issue(
                name,
                Status.CRITICAL,
                "COLLECTOR_PROFILE_INCOMPLETE",
                "Collector credential profile is incomplete",
            )
        )
    if not audit_complete:
        issues.append(
            _issue(
                name,
                Status.CRITICAL,
                "AUDIT_PROFILE_INCOMPLETE",
                "Engagement-audit credential profile is incomplete",
            )
        )
    if credentials.role_collision:
        issues.append(
            _issue(
                name,
                Status.CRITICAL,
                "COLLECTOR_AUDIT_ROLE_COLLISION",
                "Collector and audit R2 credentials are not separated",
            )
        )
    if instagram.status is Status.CRITICAL:
        issues.append(
            _issue(
                name,
                Status.CRITICAL,
                "COLLECTOR_INSTAGRAM_UNAVAILABLE",
                "Collector Instagram GET access is unavailable",
            )
        )
    if r2.details.get("collector_read_access") != "OK":
        issues.append(
            _issue(
                name,
                Status.CRITICAL,
                "COLLECTOR_R2_UNAVAILABLE",
                "Collector R2 GET access is unavailable",
            )
        )
    if freshness == "CRITICAL":
        issues.append(
            _issue(
                name,
                Status.CRITICAL,
                "COLLECTOR_STALE_CRITICAL",
                "Collector has no recent successful run",
            )
        )
    elif freshness in {"STALE", "UNKNOWN"}:
        issues.append(
            _issue(
                name,
                Status.DEGRADED,
                "COLLECTOR_STALE",
                "Collector success freshness requires attention",
            )
        )
    elif log_health.consecutive_failures:
        issues.append(
            _issue(
                name,
                Status.DEGRADED,
                "COLLECTOR_RECENT_FAILURES",
                "Collector has failures after its last successful run",
            )
        )
    status = aggregate_status([CheckResult(issue.severity, "") for issue in issues])
    summary = (
        f"freshness={freshness}, failures={log_health.consecutive_failures}, "
        f"Instagram={details['instagram_read_access']}, R2={details['r2_get_access']}"
    )
    return _result(status, summary, details, issues)


def check_engagement_learning(data: ProductionData) -> CheckResult:
    name = "engagement_learning"
    if data.history is None:
        issue = _issue(
            name,
            Status.CRITICAL,
            "ENGAGEMENT_DATA_UNAVAILABLE",
            "Engagement learning inputs are unavailable",
        )
        return _result(Status.CRITICAL, "unavailable", {}, (issue,))
    try:
        audit = analyze_engagement_learning(data.history, data.snapshots)
    except Exception:
        issue = _issue(
            name,
            Status.CRITICAL,
            "ENGAGEMENT_ANALYSIS_FAILED",
            "Engagement learning inputs could not be analyzed",
        )
        return _result(Status.CRITICAL, "analysis failed", {}, (issue,))

    learned_influence = (
        audit.model.config.mature_engagement_weight * audit.global_confidence
    )
    details = {
        "total_publications": audit.total_publication_records,
        "carousel_publications": audit.carousel_publications,
        "publications_with_snapshots": audit.publications_with_snapshots,
        "usable_publications": audit.model.useful_carousel_observations,
        "invalid_snapshots": data.invalid_snapshots,
        "effective_observations": audit.effective_observations,
        "global_confidence": audit.global_confidence,
        "current_learned_influence": learned_influence,
        "selected_snapshot_counts": {
            f"{slot}h": audit.selected_snapshot_slot_counts.get(slot, 0)
            for slot in (24, 72, 168)
        },
    }
    issues: list[Issue] = []
    if data.invalid_snapshots:
        issues.append(
            _issue(
                name,
                Status.DEGRADED,
                "INVALID_ENGAGEMENT_SNAPSHOTS",
                f"{data.invalid_snapshots} invalid Insights snapshot(s) were skipped",
            )
        )
    if (
        audit.carousel_publications
        and audit.publications_with_snapshots
        and not audit.model.useful_carousel_observations
    ):
        issues.append(
            _issue(
                name,
                Status.CRITICAL,
                "ENGAGEMENT_PIPELINE_BROKEN",
                "Snapshots exist but none are usable for engagement learning",
            )
        )
    elif audit.global_confidence < LOW_LEARNING_CONFIDENCE:
        state = (
            "operational but low-confidence"
            if audit.model.useful_carousel_observations
            else "awaiting usable data"
        )
        issues.append(
            _issue(
                name,
                Status.DEGRADED,
                "ENGAGEMENT_LOW_CONFIDENCE",
                f"Engagement learning is {state}",
            )
        )
    status = aggregate_status([CheckResult(issue.severity, "") for issue in issues])
    operational = audit.model.useful_carousel_observations > 0
    summary = (
        f"{'operational' if operational else 'no usable observations'}, "
        f"usable={audit.model.useful_carousel_observations}, "
        f"confidence={audit.global_confidence:.2%}"
    )
    return _result(status, summary, details, issues)


def check_calibration(config: LearningConfig | None = None) -> CheckResult:
    active = config or LearningConfig()
    details = {
        "reach_prior_k": active.reach_confidence_prior,
        "confidence_denominator": active.global_confidence_observations,
        "maturity_weighting": {
            f"{slot}h": SNAPSHOT_CONFIDENCE[slot]
            for slot in sorted(SNAPSHOT_CONFIDENCE)
        },
        "recency_half_life_days": active.recency_half_life_days,
    }
    if active.reach_confidence_prior != 750:
        issue = _issue(
            "calibration",
            Status.DEGRADED,
            "REACH_PRIOR_DRIFT",
            "Engagement reach prior differs from the intended production K=750",
        )
        return _result(Status.DEGRADED, "calibration drift detected", details, (issue,))
    return _result(Status.HEALTHY, "K=750; calibration unchanged", details)


def check_sources(*, quick: bool) -> CheckResult:
    name = "sources"
    if quick:
        return _result(
            Status.SKIPPED,
            "external museum probes skipped in quick mode",
            {"mode": "quick", "probes_run": 0},
        )
    source_results: dict[str, dict[str, Any]] = {}
    healthy = 0
    failed = 0
    for adapter in get_museum_adapters():
        source_id = adapter.source_id
        unavailable = adapter.unavailable_reason()
        if unavailable:
            source_results[source_id] = {"state": "SKIPPED", "reason": unavailable}
            continue
        try:
            probe_limit = MET_SOURCE_PROBE_LIMIT if source_id == "met" else 1
            candidates = adapter.fetch_candidates(
                limit=probe_limit,
                query="painting",
                rng=random.Random(f"artfolio-doctor:{source_id}"),
            )
        except Exception:
            category = getattr(adapter, "source_failure_category", None) or "UNKNOWN"
            source_results[source_id] = {"state": "FAILED", "category": category}
            failed += 1
            continue
        category = getattr(adapter, "source_failure_category", None)
        if category:
            source_results[source_id] = {"state": "FAILED", "category": category}
            failed += 1
        elif not candidates:
            source_results[source_id] = {
                "state": "EMPTY_UNPROVEN",
                "category": "NO_CANDIDATE_RETURNED",
            }
            failed += 1
        else:
            source_results[source_id] = {
                "state": "OK",
                "candidates_returned": len(candidates),
            }
            healthy += 1
    issues: list[Issue] = []
    if failed:
        severity = Status.CRITICAL if not healthy else Status.DEGRADED
        issues.append(
            _issue(
                name,
                severity,
                "MUSEUM_SOURCE_FAILURE",
                f"{failed} museum source probe(s) failed",
            )
        )
    if not healthy and not failed:
        issues.append(
            _issue(
                name,
                Status.DEGRADED,
                "NO_MUSEUM_SOURCE_PROBED",
                "No configured museum source could be probed",
            )
        )
    status = aggregate_status([CheckResult(issue.severity, "") for issue in issues])
    return _result(
        status,
        f"healthy={healthy}, failed={failed}, total={len(source_results)}",
        {"mode": "full", "probes_run": healthy + failed, "sources": source_results},
        issues,
    )


def collect_report(
    *,
    quick: bool,
    environment: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> DoctorReport:
    credentials = collect_credentials(environment)
    production_environment = dict(credentials.collector_environment)
    if environment is not None:
        production_environment.update(environment)

    repository = check_repository()
    production = check_production_configuration(production_environment)
    launchagent = check_launchagent()
    instagram = check_instagram(credentials)
    r2, data = check_r2(credentials)
    lifecycle = check_publication_lifecycle(data.history, now=now)
    collector = check_collector(
        credentials,
        instagram,
        r2,
        now=now,
    )
    engagement = check_engagement_learning(data)
    calibration = check_calibration()
    sources = check_sources(quick=quick)
    checks = {
        "repository": repository,
        "production_config": production,
        "rights_policy": _result(
            production.status,
            (
                "strict_public_domain active"
                if production.details.get("strict_rights_policy_active")
                else "unsafe or unavailable"
            ),
            {
                "configured": production.details.get("rights_policy", "MISSING"),
                "strict": production.details.get("strict_rights_policy_active", False),
                "permissive": production.details.get(
                    "permissive_rights_policy_active", False
                ),
            },
        ),
        "publication_lifecycle": lifecycle,
        "collector": collector,
        "launchagent": launchagent,
        "engagement_learning": engagement,
        "calibration": calibration,
        "r2": r2,
        "instagram": instagram,
        "sources": sources,
    }
    return DoctorReport("quick" if quick else "full", checks)


def render_text(report: DoctorReport) -> str:
    labels = {
        "repository": "Repository",
        "production_config": "Production config",
        "rights_policy": "Rights policy",
        "publication_lifecycle": "Publication lifecycle",
        "collector": "Collector",
        "launchagent": "LaunchAgent",
        "engagement_learning": "Engagement learning",
        "calibration": "Calibration",
        "r2": "R2",
        "instagram": "Instagram",
        "sources": "Sources",
    }
    lines = [
        "Artfolio Production Doctor",
        "==========================",
        f"Overall: {report.overall.value}",
        f"Mode: {report.mode}",
        "",
    ]
    for name, result in report.checks.items():
        lines.append(f"{labels[name]}: {result.status.value} — {result.summary}")
    lines.extend(("", "Issues:"))
    if report.issues:
        lines.extend(
            f"- [{issue.severity.value}] {issue.message} ({issue.code})"
            for issue in report.issues
        )
    else:
        lines.append("- none")
    return "\n".join(lines)


def render_json(report: DoctorReport) -> str:
    return json.dumps(report.as_dict(), indent=2, sort_keys=True)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Artfolio production health doctor"
    )
    parser.add_argument("--json", action="store_true", help="Emit valid JSON only")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Skip external museum source probes",
    )
    args = parser.parse_args(argv)
    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        report = collect_report(quick=args.quick)
    finally:
        logging.disable(previous_logging_disable)
    print(render_json(report) if args.json else render_text(report))
    return STATUS_EXIT_CODES[report.overall]


if __name__ == "__main__":
    raise SystemExit(run())
