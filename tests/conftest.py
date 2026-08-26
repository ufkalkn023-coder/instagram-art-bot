import pytest

from src.aic_image_policy import (
    AICImageRequestPolicy,
    reset_aic_image_request_policy_for_tests,
)


@pytest.fixture(autouse=True)
def _isolated_aic_image_policy():
    """Keep process-local AIC health state isolated and test runs instantaneous."""
    reset_aic_image_request_policy_for_tests(AICImageRequestPolicy(interval_seconds=0))
    yield
    reset_aic_image_request_policy_for_tests(AICImageRequestPolicy(interval_seconds=0))
