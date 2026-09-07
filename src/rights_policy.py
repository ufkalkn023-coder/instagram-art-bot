"""Central, reversible feed eligibility policy for artwork rights metadata."""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import Enum
from typing import Protocol

from src.models import CONFIRMED_RIGHTS_STATUSES


RIGHTS_POLICY_ENV = "ARTFOLIO_RIGHTS_POLICY"


class RightsPolicyMode(str, Enum):
    PERMISSIVE = "permissive"
    STRICT_PUBLIC_DOMAIN = "strict_public_domain"


class RightsMetadata(Protocol):
    is_public_domain: bool
    rights_status: str | None


def resolve_rights_policy(
    environment: Mapping[str, str] | None = None,
) -> RightsPolicyMode:
    """Return the configured policy; non-production callers default to permissive."""
    values = os.environ if environment is None else environment
    configured = values.get(RIGHTS_POLICY_ENV, RightsPolicyMode.PERMISSIVE.value)
    try:
        return RightsPolicyMode(configured.strip().casefold())
    except (AttributeError, ValueError) as error:
        supported = ", ".join(mode.value for mode in RightsPolicyMode)
        raise ValueError(
            f"{RIGHTS_POLICY_ENV} must be one of: {supported}"
        ) from error


def is_rights_eligible(
    artwork: RightsMetadata,
    mode: RightsPolicyMode | None = None,
) -> bool:
    """Apply only the configured policy; metadata is never mutated or discarded."""
    active_mode = mode or resolve_rights_policy()
    if active_mode is RightsPolicyMode.PERMISSIVE:
        return True
    return bool(
        artwork.is_public_domain
        and artwork.rights_status in CONFIRMED_RIGHTS_STATUSES
    )
