"""Single-use GitHub Actions authorization for a production carousel run."""

from __future__ import annotations

import hmac
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping
from uuid import UUID

import requests


MAX_AUTHORIZATION_AGE = timedelta(minutes=60)
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class ProductionAuthorizationError(RuntimeError):
    """The workflow is not authorized to make a production mutation."""


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ProductionAuthorization:
    key: str
    run_id: int
    created_at: datetime
    issued_at: datetime | None = None
    head_sha: str | None = None
    repository: str | None = None
    workflow_path: str | None = None
    run_attempt: int = 1
    publication_kind: str = "carousel"

    @property
    def is_scheduled_feed(self) -> bool:
        return self.key.startswith("schedule:") and self.publication_kind == "carousel"

    def require_fresh(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID")
        age = now.astimezone(timezone.utc) - self.created_at
        if age < timedelta(0) or age > MAX_AUTHORIZATION_AGE:
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_EXPIRED")
        if self.issued_at is not None:
            issued_age = now.astimezone(timezone.utc) - self.issued_at
            if (issued_age < timedelta(0) or issued_age > MAX_AUTHORIZATION_AGE
                    or self.created_at < self.issued_at):
                raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_EXPIRED")


def validate_run(
    environment: Mapping[str, str], run: Mapping[str, object], *,
    publication_kind: str = "carousel",
    now: datetime | None = None,
) -> ProductionAuthorization:
    """Bind the authorization to GitHub's immutable run identity and creation time."""
    event = environment.get("GITHUB_EVENT_NAME")
    if event not in {"workflow_dispatch", "schedule"}:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID")
    try:
        attempt = int(environment.get("GITHUB_RUN_ATTEMPT", ""))
        run_id = int(environment.get("GITHUB_RUN_ID", ""))
    except ValueError as error:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID") from error
    if attempt != 1 or type(run.get("run_attempt")) is not int or run.get("run_attempt") != 1:
        raise ProductionAuthorizationError("PRODUCTION_RERUN_PUBLICATION_BLOCKED")
    if run_id < 1 or type(run.get("id")) is not int or run.get("id") != run_id or run.get("event") != event:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID")
    if run.get("head_sha") != environment.get("GITHUB_SHA"):
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID")
    if environment.get("GITHUB_REF") != "refs/heads/main" or run.get("head_branch") != "main":
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID")
    repository = environment.get("GITHUB_REPOSITORY", "")
    run_repository = run.get("repository")
    if (not _REPOSITORY.fullmatch(repository)
            or not isinstance(run_repository, dict)
            or run_repository.get("full_name") != repository):
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID")
    created_at = _timestamp(run.get("created_at"))
    if publication_kind not in {"carousel", "reel"}:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID")

    if event == "schedule":
        schedule_flag = (
            "ARTFOLIO_PRODUCTION_SCHEDULE_ENABLED" if publication_kind == "carousel"
            else "ARTFOLIO_REEL_SCHEDULE_ENABLED"
        )
        if environment.get(schedule_flag) != "true":
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID")
        if publication_kind == "carousel":
            from src.feed_schedule import FEED_REPOSITORY, FEED_WORKFLOW
            if (repository != FEED_REPOSITORY or run.get("path") != FEED_WORKFLOW
                    or environment.get("GITHUB_WORKFLOW_REF") != (
                        f"{repository}/{FEED_WORKFLOW}@refs/heads/main"
                    ) or not re.fullmatch(r"[0-9a-f]{40}", environment.get("GITHUB_SHA", ""))):
                raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID")
        workflow_path = run.get("path")
        authorization = ProductionAuthorization(
            f"schedule:{run_id}", run_id, created_at, head_sha=environment.get("GITHUB_SHA"),
            repository=repository,
            workflow_path=workflow_path if isinstance(workflow_path, str) else None,
            run_attempt=attempt, publication_kind=publication_kind,
        )
    else:
        confirmation = (
            "PUBLISH_TO_INSTAGRAM" if publication_kind == "carousel"
            else "PUBLISH_REEL_TO_INSTAGRAM"
        )
        if environment.get("ARTFOLIO_CONFIRM_PUBLISH") != confirmation:
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID")
        supplied = environment.get("ARTFOLIO_AUTHORIZATION_ID", "")
        expected = environment.get("ARTFOLIO_MANUAL_AUTHORIZATION_ID", "")
        try:
            valid_id = str(UUID(supplied, version=4)) == supplied
        except (ValueError, AttributeError):
            valid_id = False
        if not valid_id or not expected or not hmac.compare_digest(supplied, expected):
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID")
        issued_at = _timestamp(environment.get("ARTFOLIO_MANUAL_AUTHORIZATION_ISSUED_AT"))
        authorization = ProductionAuthorization(
            f"manual:{supplied}", run_id, created_at, issued_at
        )
    authorization.require_fresh(now)
    return authorization


def load_workflow_authorization(
    environment: Mapping[str, str] | None = None,
    *, publication_kind: str = "carousel",
) -> ProductionAuthorization:
    """Read only this Actions run; a failed or malformed lookup closes the gate."""
    environment = os.environ if environment is None else environment
    repository = environment.get("GITHUB_REPOSITORY", "")
    run_id = environment.get("GITHUB_RUN_ID", "")
    token = environment.get("GITHUB_TOKEN", "")
    if not _REPOSITORY.fullmatch(repository) or not run_id.isdecimal() or not token:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID")
    if environment.get("GITHUB_RUN_ATTEMPT") != "1":
        raise ProductionAuthorizationError("PRODUCTION_RERUN_PUBLICATION_BLOCKED")
    try:
        response = requests.get(
            f"https://api.github.com/repos/{repository}/actions/runs/{run_id}",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"},
            timeout=10,
            allow_redirects=False,
        )
        response.raise_for_status()
        if response.status_code != 200:
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_LOOKUP_FAILED")
        run = response.json()
    except (requests.RequestException, ValueError) as error:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_LOOKUP_FAILED") from error
    if not isinstance(run, dict):
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_LOOKUP_FAILED")
    return validate_run(environment, run, publication_kind=publication_kind)
