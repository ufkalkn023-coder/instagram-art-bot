"""Fail-fast validation for configuration required by production publishing."""

from collections.abc import Mapping
import os

from src.rights_policy import RIGHTS_POLICY_ENV, RightsPolicyMode, resolve_rights_policy


REQUIRED_PRODUCTION_VARIABLES = (
    "INSTAGRAM_ACCOUNT_ID",
    "INSTAGRAM_ACCESS_TOKEN",
    "CLOUDFLARE_R2_ACCOUNT_ID",
    "CLOUDFLARE_R2_ACCESS_KEY_ID",
    "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
    "CLOUDFLARE_R2_BUCKET_NAME",
    "CLOUDFLARE_R2_PUBLIC_URL",
)

REQUIRED_RECONCILIATION_VARIABLES = (
    "INSTAGRAM_ACCESS_TOKEN",
    "CLOUDFLARE_R2_ACCOUNT_ID",
    "CLOUDFLARE_R2_ACCESS_KEY_ID",
    "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
    "CLOUDFLARE_R2_BUCKET_NAME",
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
