"""Fail-fast validation for configuration required by production publishing."""

from collections.abc import Mapping
from datetime import datetime, timezone
import ipaddress
import os
from urllib.parse import urlsplit

from src import history_tracker, instagram_poster, publication_state
from src.rights_policy import RIGHTS_POLICY_ENV, RightsPolicyMode, resolve_rights_policy


REQUIRED_PRODUCTION_VARIABLES = (
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
)

REQUIRED_RECONCILIATION_VARIABLES = (
    "INSTAGRAM_ACCESS_TOKEN",
    "CLOUDFLARE_R2_ACCOUNT_ID",
    "CLOUDFLARE_R2_ACCESS_KEY_ID",
    "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
    "CLOUDFLARE_R2_BUCKET_NAME",
    "CLOUDFLARE_STATE_R2_BUCKET_NAME",
    "CLOUDFLARE_STATE_R2_ACCESS_KEY_ID",
    "CLOUDFLARE_STATE_R2_SECRET_ACCESS_KEY",
)

OPTIONAL_INTEGRATION_VARIABLES = {
    "gemini": ("GOOGLE_GEMINI_API_KEY",),
}


class ProductionConfigurationError(RuntimeError):
    """Required production configuration is absent."""


def validate_production_configuration(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Validate required variables and report optional adapter availability.

    Values are never included in errors or diagnostics.
    """
    environment = os.environ if environment is None else environment
    missing = [
        name for name in REQUIRED_PRODUCTION_VARIABLES if not environment.get(name, "").strip()
    ]
    if not environment.get(RIGHTS_POLICY_ENV, "").strip():
        missing.append(RIGHTS_POLICY_ENV)
    if missing:
        raise ProductionConfigurationError(
            "Missing required production configuration: " + ", ".join(missing)
        )
    try:
        publication_state.StateConfiguration.from_environment(environment)
    except publication_state.StateValidationError as error:
        raise ProductionConfigurationError(str(error)) from error

    try:
        rights_policy = resolve_rights_policy(environment)
    except ValueError as error:
        raise ProductionConfigurationError(str(error)) from error
    if rights_policy is not RightsPolicyMode.STRICT_PUBLIC_DOMAIN:
        raise ProductionConfigurationError(
            f"{RIGHTS_POLICY_ENV} must be "
            f"{RightsPolicyMode.STRICT_PUBLIC_DOMAIN.value} for production publishing"
        )

    optional_status: dict[str, str] = {}
    for integration, variable_names in OPTIONAL_INTEGRATION_VARIABLES.items():
        configured = sum(
            bool(environment.get(name, "").strip()) for name in variable_names
        )
        if configured == len(variable_names):
            optional_status[integration] = "enabled"
        elif configured:
            optional_status[integration] = "incomplete_disabled"
        else:
            optional_status[integration] = "disabled"
    return optional_status


def validate_reconciliation_configuration(
    environment: Mapping[str, str] | None = None,
) -> None:
    """Validate only credentials used by lifecycle reconciliation."""
    environment = os.environ if environment is None else environment
    missing = [
        name
        for name in REQUIRED_RECONCILIATION_VARIABLES
        if not environment.get(name, "").strip()
    ]
    if missing:
        raise ProductionConfigurationError(
            "Missing required reconciliation configuration: " + ", ".join(missing)
        )
    try:
        publication_state.StateConfiguration.from_environment(environment)
    except publication_state.StateValidationError as error:
        raise ProductionConfigurationError(str(error)) from error


def _validate_public_media_base_url(value: str) -> None:
    """Ensure staged image URLs can be anonymous public HTTPS URLs."""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        hostname = None
        port = None
        parsed = None
    if (
        parsed is None
        or parsed.scheme != "https"
        or not hostname
        or "." not in hostname
        or hostname == "localhost"
        or hostname.endswith(".local")
        or parsed.username is not None
        or parsed.password is not None
        or "?" in value
        or "#" in value
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ProductionConfigurationError(
            "CLOUDFLARE_R2_PUBLIC_URL must be an anonymous public HTTPS base URL"
        )
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not address.is_global:
        raise ProductionConfigurationError(
            "CLOUDFLARE_R2_PUBLIC_URL must be an anonymous public HTTPS base URL"
        )


def validate_carousel_production_preflight() -> dict[str, str]:
    """Read-only carousel readiness check before reconciliation or acquisition."""
    optional_status = validate_production_configuration()
    account_id, access_token = instagram_poster.validate_instagram_credentials(
        os.environ.get("INSTAGRAM_ACCOUNT_ID"),
        os.environ.get("INSTAGRAM_ACCESS_TOKEN"),
    )
    _validate_public_media_base_url(os.environ["CLOUDFLARE_R2_PUBLIC_URL"].strip())
    store = publication_state.PublicationStateStore()
    safety, _ = store.load_safety()
    receipts, _ = store.load_receipts()
    require_clear_publication_state(safety, receipts)
    instagram_poster.validate_instagram_account_access(
        account_id, access_token,
    )
    history_tracker.validate_carousel_history_for_production(
        publication_state.history_view(safety)
    )
    return optional_status


def require_clear_publication_state(
    safety: publication_state.PublicationSafetyState,
    receipts: publication_state.PublicationReceipts,
    *,
    owned_feed_ids: set[str] | None = None,
    owned_reel_ids: set[str] | None = None,
    now: datetime | None = None,
) -> None:
    """Fail closed on unresolved lifecycle and owned-media cleanup evidence."""
    publication_state.validate_live_receipt_coverage(safety, receipts)
    now = now or datetime.now(timezone.utc)
    active = safety.active_publication_state
    for row in (*active.posted_artworks, *active.reel_reservations):
        status = row.get("status")
        if status == "PENDING":
            reserved_at = history_tracker._parse_reserved_at(row.get("reserved_at"))
            detail = "stale_pending_requires_reconciliation" if (
                reserved_at is None or now - reserved_at >= history_tracker.PENDING_RESERVATION_TTL
            ) else "live_pending"
            raise ProductionConfigurationError(f"STOP_AUTOMATED_PRODUCTION: {detail}")
        if status in {"PUBLISHING", "AMBIGUOUS"}:
            raise ProductionConfigurationError(
                f"STOP_AUTOMATED_PRODUCTION: unresolved_{status.lower()}"
            )
    if active.staging_media_cleanup_queue or active.reel_staging_cleanup_queue:
        raise ProductionConfigurationError("STOP_AUTOMATED_PRODUCTION: cleanup_queue_unresolved")

    for owned_ids, rows, publication_field in (
        (owned_feed_ids, active.posted_artworks, "publication_id"),
        (owned_reel_ids, active.reel_reservations, "publication_id"),
    ):
        if owned_ids is None:
            continue
        status_by_id: dict[str, set[str]] = {}
        for row in rows:
            status_by_id.setdefault(row[publication_field], set()).add(row["status"])
        for publication_id in owned_ids:
            # PUBLISHED media may still be within its one-day lifecycle, or may
            # have expired naturally. Neither state needs cleanup investigation.
            if status_by_id.get(publication_id) != {"PUBLISHED"}:
                raise ProductionConfigurationError(
                    "STOP_AUTOMATED_PRODUCTION: owned_media_lifecycle_anomaly"
                )
