# Feed stages 3–6 rollout readiness

Continuation of `2026-10-08-feed-remaining.md`. Preparation is local on
`codex/feed-remaining-improvements`. Direct user approval in this chat on
2026-10-08 authorizes the concrete commit/push/PR/CI, isolated R2 verification,
protected-main merge, production queue installation and activation sequence below.

## Verified 2026-10-08

- Local and live main base: `240f4a7ccab45da20fa838160ea992ae064fce6c`.
  Stage 3–6 changes remain uncommitted; `.mimosa/` is preserved.
- Fresh full suite with Python **3.10.21** and hash-locked dependencies:
  **1525 passed, 13 skipped in 75.65s**. The 13 skips are explicitly gated live
  R2 tests. Focused queue/integration/workflow tests: **45 passed**.
  Compileall, affected-test Ruff and `git diff --check` passed.
- Three live queue scenarios now join the existing integration module:
  immutable JPEG round trip across fresh runners, terminal archive/refill;
  stale-ETag ownership rejection; successful remote claim with a simulated
  lost response remaining CLAIMED even after expiry and blocking refill.
  Offline scenario checks use an in-memory client and do not prove R2 behavior.
- Independent read-only review of the continuation test changes: no actionable
  findings. Namespaced test clients now reject a mismatched bucket before I/O.
- GitHub production schedule variable is true. Queue/experiment/notification
  feature variables are absent. There are no configured repository environments.
- Guarded live R2 GET/LIST at **2026-10-08T20:42:06Z**: safety generation 32,
  receipt generation 6, 73 receipts, queue has no packages, analytics fresh,
  zero invalid loaded snapshots, no actionable combined status. Schedule is
  `WAITING_COOLDOWN`, next format carousel, approved SHA matches live main.
  Next eligibility is **2026-10-09T18:16:07Z / 9 October 21:16:07 Istanbul**;
  nominal hourly cron follows at 21:17, subject to runtime guards/provider delivery.
  Only 8 R2 reads; zero R2 writes and no Instagram credential was loaded.
- Local engagement-audit Keychain is missing all three state fields. Collector
  has the state key pair but lacks the bucket name. The read-only probe explicitly
  selected only the seven collector R2 fields and the previously verified
  `instagram-art-bot-state` name; a client guard blocked all write methods.
  No Keychain configuration changed; no implicit profile fallback was added.

## Live integration prerequisite

GitHub repository secret names currently lack:

- `CLOUDFLARE_R2_TEST_BUCKET_NAME`
- `CLOUDFLARE_R2_TEST_ACCESS_KEY_ID`
- `CLOUDFLARE_R2_TEST_SECRET_ACCESS_KEY`

Existing production R2 secret names are present. Historical live verification
[32978505040](https://github.com/ufkalkn023-coder/instagram-art-bot/actions/runs/32978505040)
passed on 26 August using the general R2 secret names. Its workflow revision
`41a148dc6ed4aafc2b3207f9e55ef56acfb86488` predates the current dedicated-test
secret mapping; it does not prove the new queue lifecycle works.

Use a separate private test bucket and credentials scoped to that bucket. Do not
alias the TEST bindings to production. The current suite checks both production
bucket names, maps all application keys under its UUID namespace, and cleans up
only that namespace. It sends no Instagram requests.

## Authorized rollout sequence

1. Provision or locate the isolated test bucket/credential pair; install only
   the three TEST bindings through secure inputs, never chat or logs.
2. Commit the reviewed changes excluding `.mimosa/`, push the feature branch and
   open a protected-main PR. Run Python 3.10 required CI.
3. Dispatch `R2 Integration Verification` at the exact reviewed feature ref with
   `RUN_R2_INTEGRATION`. Confirm all 13 tests execute, teardown reports zero
   remaining objects and the run head SHA is the intended feature SHA.
4. Merge after green CI/live integration. Verify merged-main CI and exact code
   identity; retain feature variables off until installation is verified.
5. Explicitly prepare the private production queue through
   `Prepare private Feed queue` with `PREPARE_FEED_QUEUE`, target 3 and experiment
   false. Read back the manifest/assets and recheck current exact-ID museum rights.
   This step writes production queue data but does not publish Instagram media.
6. Enable queue consumption only after valid read-back. Refresh hourly approval
   for the exact new main SHA with a fresh state/receipt read and CAS, preserving
   cooldown, permits and existing publication evidence. Verify read-only readiness.
   No immediate manual Instagram publication is part of this rollout.

Caption experiments and notification activation remain separate optional choices.
Museum bounded rediscovery remains intentionally conservative and may reject
valid packages; normal acquisition is the fallback for exhausted/rejected content.
Do not weaken rights requirements to improve queue hit rate.

## Evidence

- `/private/tmp/artfolio-feed-rollout-python310-tests.log`
- `/private/tmp/artfolio-feed-rollout-readonly.json`
- `tests/integration/test_r2_conditional_writes.py`
- `tests/test_r2_feed_queue_integration_contract.py`
- `tests/test_r2_integration_safety.py`

No commit, push, PR, merge, workflow dispatch, secret mutation, production R2
write/delete, activation or Instagram publication occurred during the readiness
checkpoint above. Subsequent authorized deployment results are recorded below.

## Deployment execution

- User explicitly approved the prepared rollout and instructed autonomous
  completion without further routine confirmation. Optional caption experiments
  and status notifications remain off for the initial queue activation.
