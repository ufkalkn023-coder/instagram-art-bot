#!/usr/bin/env python3
"""Run the isolated, read-only Instagram Insights collector once."""

import argparse
import logging
import os
import re
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.insights_collector import InsightsCollector, format_summary, manual_association  # noqa: E402
from src.insights_health import freshness_state, inspect_collector_log  # noqa: E402
from src.insights_storage import InsightsStorage, InsightsStorageError  # noqa: E402
from src.instagram_insights import (  # noqa: E402
    InstagramInsightsClient,
    InstagramInsightsAuthenticationError,
    InstagramInsightsConfigurationError,
    InstagramInsightsError,
    InstagramInsightsPermissionError,
)
from src.local_credentials import (  # noqa: E402
    COLLECTOR_PROFILE,
    active_collector_r2_credential_matches_audit_profile,
    format_credential_status,
    load_keychain_credentials,
)
from src.reel_analytics import load_local_reels  # noqa: E402

DEFAULT_LOG_PATH = Path.home() / "Library" / "Logs" / "Artfolio" / "instagram-insights.log"


def _default_reels_root() -> Path:
    configured = os.environ.get("ARTFOLIO_REELS_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    return ROOT.parent / "Remotion İnstagram Reels" / "artfolio-reels"


def _configure_logging(log_file: Path | None) -> None:
    if log_file is None:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        return
    destination = log_file.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(destination, maxBytes=1_048_576, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def _launchagent_failure_count() -> int | None:
    """Return the loaded LaunchAgent run count only when its last run failed."""
    try:
        result = subprocess.run(
            [
                "/bin/launchctl",
                "print",
                f"gui/{os.getuid()}/com.artfolio.instagram-insights",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    if result.returncode:
        return None
    runs = re.search(r"^\s*runs = (\d+)\s*$", result.stdout, re.MULTILINE)
    last_exit = re.search(r"^\s*last exit code = (-?\d+)\s*$", result.stdout, re.MULTILINE)
    if runs is None or last_exit is None or int(last_exit.group(1)) == 0:
        return None
    return int(runs.group(1))


def _run_health_check(
    credential_status: dict[str, bool],
    *,
    log_path: Path = DEFAULT_LOG_PATH,
) -> int:
    profile_complete = all(credential_status.values())
    role_collision = (
        profile_complete and active_collector_r2_credential_matches_audit_profile()
    )

    instagram_state = "NOT_CHECKED"
    r2_state = "NOT_CHECKED"
    bucket_state = "MISSING" if not credential_status.get("CLOUDFLARE_R2_BUCKET_NAME") else "PRESENT"
    if profile_complete:
        try:
            InsightsStorage().load_history()
        except InsightsStorageError:
            r2_state = "INVALID"
        else:
            r2_state = "OK"
        try:
            InstagramInsightsClient().discover_recent_media()
        except InstagramInsightsAuthenticationError:
            instagram_state = "INVALID"
        except InstagramInsightsPermissionError:
            instagram_state = "INVALID"
        except InstagramInsightsError:
            instagram_state = "INACCESSIBLE"
        else:
            instagram_state = "OK"

    log_health = inspect_collector_log(log_path)
    consecutive_failures = log_health.consecutive_failures
    scheduled_failures = _launchagent_failure_count()
    if scheduled_failures is not None and consecutive_failures >= scheduled_failures:
        consecutive_failures = scheduled_failures
    freshness, age_hours = freshness_state(
        log_health.last_success,
        consecutive_failures=consecutive_failures,
    )
    last_success = (
        log_health.last_success.isoformat().replace("+00:00", "Z")
        if log_health.last_success is not None
        else "UNKNOWN"
    )
    last_success_age = f"{age_hours:.2f}h" if age_hours is not None else "UNKNOWN"
    profile_state = "OK" if profile_complete and not role_collision else (
        "INVALID" if role_collision else "INCOMPLETE"
    )
    write_configuration = (
        "INVALID_ROLE_COLLISION"
        if role_collision
        else ("CONFIGURED_PERMISSION_NOT_PROVEN" if profile_complete else "MISSING")
    )
    overall = (
        "HEALTHY"
        if profile_state == "OK"
        and instagram_state == "OK"
        and r2_state == "OK"
        and freshness == "HEALTHY"
        else "CRITICAL"
    )
    print(f"Collector profile: {profile_state}")
    print(f"Instagram credentials: {'PRESENT' if all(credential_status.get(name) for name in ('INSTAGRAM_ACCOUNT_ID', 'INSTAGRAM_ACCESS_TOKEN')) else 'MISSING'}")
    print(f"R2 credentials: {'PRESENT' if all(credential_status.get(name) for name in ('CLOUDFLARE_R2_ACCOUNT_ID', 'CLOUDFLARE_R2_ACCESS_KEY_ID', 'CLOUDFLARE_R2_SECRET_ACCESS_KEY', 'CLOUDFLARE_R2_BUCKET_NAME')) else 'MISSING'}")
    print(f"Instagram read access: {instagram_state}")
    print(f"R2 read access: {r2_state}")
    print(f"R2 bucket configuration: {bucket_state}")
    print(f"R2 required write configuration: {write_configuration}")
    print(f"Last success: {last_success}")
    print(f"Last success age: {last_success_age}")
    print(f"Consecutive failures: {consecutive_failures}")
    print(f"Freshness: {freshness}")
    print(f"Overall: {overall}")
    return 0 if overall == "HEALTHY" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect due Instagram media Insights snapshots")
    parser.add_argument("--reels-root", type=Path, default=_default_reels_root(), help="Path to the Artfolio Reels repository")
    parser.add_argument("--dry-run", action="store_true", help="Inspect mapped due slots without Meta calls or R2 writes")
    parser.add_argument("--check-secrets", action="store_true", help="Report required credentials as SET/MISSING without revealing values")
    parser.add_argument("--health-check", action="store_true", help="Run read-only credential, access, and freshness checks")
    parser.add_argument("--log-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--legacy-publications", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--link", nargs=2, metavar=("LOCAL_REEL_ID", "INSTAGRAM_MEDIA_ID"), help="Persist a manual media association")
    args = parser.parse_args()
    _configure_logging(args.log_file)
    credential_status = load_keychain_credentials(COLLECTOR_PROFILE)
    if args.check_secrets:
        print(format_credential_status(COLLECTOR_PROFILE, credential_status))
        return 0
    if args.health_check:
        return _run_health_check(
            credential_status,
            log_path=args.log_file or DEFAULT_LOG_PATH,
        )
    if active_collector_r2_credential_matches_audit_profile():
        logging.getLogger(__name__).error(
            "Insights collector could not start: collector R2 credential duplicates "
            "the engagement-audit profile; dedicated Object Read & Write credential required"
        )
        return 1

    try:
        storage = InsightsStorage()
        local_reels = load_local_reels(args.reels_root) if args.reels_root.exists() else None
        client = None if args.dry_run and not args.link else InstagramInsightsClient()
        if args.link:
            if local_reels is None:
                raise ValueError("A valid Artfolio Reels repository is required for manual linking")
            association, changed = manual_association(storage, client, local_reels, args.link[0], args.link[1])
            logging.getLogger(__name__).info(
                "[insights] manual_link=%s reel_id=%s media_id=%s",
                "written" if changed else "unchanged",
                association["reel_id"],
                association["instagram_media_id"],
            )
            return 0
        summary = InsightsCollector(storage, client).run(
            dry_run=args.dry_run,
            local_reels=local_reels,
            association_mode=not args.legacy_publications,
        )
    except (InsightsStorageError, InstagramInsightsError, InstagramInsightsConfigurationError, ValueError) as exc:
        logging.getLogger(__name__).error("Insights collector could not start: %s", exc)
        return 1

    rendered_summary = format_summary(summary)
    print(rendered_summary)
    if args.log_file is not None:
        logging.getLogger(__name__).info("\n%s", rendered_summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
