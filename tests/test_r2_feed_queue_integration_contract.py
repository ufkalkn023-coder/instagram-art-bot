"""Exercise live queue scenarios offline; this does not prove R2 interoperability."""

import pytest

from tests.integration import test_r2_conditional_writes as live
from tests.integration.r2_test_support import R2IntegrationContext, make_run_prefix
from tests.test_r2_feed_queue import MemoryS3


@pytest.mark.parametrize("scenario", [
    live.test_prepared_feed_queue_survives_runners_and_archives_terminal_batch,
    live.test_prepared_feed_queue_stale_owner_cannot_replace_winner,
    live.test_prepared_feed_queue_lost_claim_response_never_rearms,
], ids=["lifecycle", "stale-cas", "lost-response"])
def test_live_queue_scenario_contract_offline(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(R2IntegrationContext, "wait_for_write_slot", lambda *_: None)
    context = R2IntegrationContext(MemoryS3(), "test-bucket", make_run_prefix())
    scenario(context, tmp_path)
    assert context.created_keys
    assert all(key.startswith(context.prefix) for key in context.client.objects)
