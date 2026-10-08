# Remaining Feed improvements — implementation ledger

Continuation of the user-approved six-stage roadmap; base `240f4a7`.
Existing Stage 1 and format-scoped Stage 2 are deployed. Implementation is
local on `codex/feed-remaining-improvements`; deployment is a separate action.

## Contract and sequence

1. Complete Stage 3: private durable R2 queue, bounded immutable assets,
   conditional manifest ownership, never rearm crashed/uncertain claims.
   Reuse local package validation and existing publication guards; fresh museum
   metadata must confirm exact artwork identity and rights before reservation.
   Empty/exhausted queues fall back to normal acquisition; unreadable state fails.
   Add opt-in Actions preparation/consumption, default disabled.
2. Stage 4: metadata-grounded carousel/single pairs within prepared batches,
   using the theme registry, existing bounded selection and artwork exclusion.
   Failed/missing thematic match falls back explicitly without invented linkage.
   Persist pair identity in reservations and confirmed publication records.
3. Stage 5: opt-in deterministic caption-hook experiment using the existing
   taxonomy, one variable and fixed cover. Persist assignment before publication;
   equal-format/theme/age evaluation with metric coverage and uncertainty, no
   automated winner promotion. Incomplete generation must not claim delivery.
4. Stage 6: read-only combined schedule, queue and analytics status; durable
   meaningful-change deduplication for blocked/recovered states in Actions summaries.
   Normal waiting and unchanged state remain quiet. No new external recipient.

## Validation and review

Write failing regressions before implementation; focused tests and full suite
after each stage, compile/diff checks, final independent read-only review.
Preserve `.mimosa/`, Reel behavior, +48h cooldown, format alternation, strict rights,
exact-SHA authorization, reservation/receipt/CAS and ambiguity safeguards.
No commits, pushes, merges, production R2 mutations or publication in this task.

## Rulings

- Use a local branch in the canonical checkout; no extra worktree required.
- Archive sealed terminal/expired manifests before queue replenishment; fresh
  READY and all CLAIMED packages cannot be overwritten. Retain immutable assets
  for operator review. No automatic deletion; retention needs operator review.
- Fresh rights use existing adapters' bounded searches and exact canonical ID
  matching. Failure to rediscover the exact artwork is a safe rejection, never
  confirmation of rights. This can reduce queue hit rate.
- Actions job summaries are the notification surface. No email/Slack/Codex
  automation is created or activated by local implementation.

## Progress

- Baseline full suite: **1478 passed, 10 skipped**.
- Stage 3/4 full suite: **1502 passed, 10 skipped**; fixed an import captured
  during a runtime test monkeypatch before this green verification.
- Stage 5 full suite: **1507 passed, 10 skipped**.
- Stage 6/full integration: **1519 passed, 10 skipped** before the final expiry regression.
- Focused expiry/revalidation runtime regression: **5 passed**.
- Independent read-only safety review: no actionable findings in consequential
  queue ownership, refill, rights/expiry/history/publication boundaries,
  metadata finalization, experimental identity or opt-in Actions controls.
- Fresh source query rediscovery remains conservative and may reject valid
  content. No live R2 integration or publication performed; live activation
  requires a separate reviewed rollout and exact-new-SHA approval.
- Final validation: **1520 passed, 10 skipped in 46.45s**; final workflow/secret checks **16 passed**. Compileall, affected-file Ruff, diff checks and offline CLI smoke tests passed.
- PROJECT_DURABLE checkpoint prepared for UfukOS CURRENT.md and a Codex work log. Production rollout/Python 3.10 CI/live R2 interoperability remain separate operations.

## Rollout preparation continuation — 2026-10-08

- Python 3.10.21 with hash-locked dependencies now verified locally:
  **1525 passed, 13 skipped**; focused queue/integration/workflow checks **45 passed**.
- Added three gated live queue lifecycle/CAS/lost-response scenarios, checked
  their contracts offline, and guarded the namespaced test client's bucket.
  Independent continuation review found no actionable issues.
- Fresh guarded production GET/LIST confirmed matching live main approval,
  WAITING_COOLDOWN, empty queue and fresh analytics; no writes or Instagram calls.
- Dedicated TEST secret names are missing today. Historical August integration
  used the general R2 names, before today's dedicated test mapping. Live queue
  writes/interoperability remain unverified; no production activation occurred.
- Concrete pending rollout and evidence: `2026-10-08-feed-rollout.md`.
