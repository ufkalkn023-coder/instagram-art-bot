# Scheduled Feed control

Feed supports an operator-reviewed single-slot mode and an explicitly approved
continuous mode. Initialization, scheduler enablement and publication require
operator authorization. Existing single-slot approvals keep their original behavior.

## Continuous operation

`enable-continuous` records an append-only approval bound to the exact authenticated
main SHA. It may revoke an unconsumed older single-slot permit without erasing its
approval. It refuses unresolved owned attempts and dirty publication, receipt or
media state. Explicit continuous approval supersedes the per-attempt operator-review
requirement for this mode; it does not change publication or duplicate safeguards.

The approval's `cadence` is `daily_utc1717` (the default for existing records) or
`hourly_utc17`. Changing cron does not upgrade an existing approval. At each eligible
event, the authenticated original main run checks the active
approval, exact SHA, finite 60-minute window, clean state and 48-hour completion
gate. A run before cooldown eligibility exits successfully without reservation,
acquisition or publication. An eligible run appends one owned permit tied to the
approval and run identity. Every reservation and pre-publish CAS still rechecks its
owner, policy revocation and publication evidence. Another attempt in the same slot
is rejected, including after a definitive failure.

Successful completion keeps scheduling enabled. A definitive failure can leave
scheduling enabled only when its cleanup and reconciliation evidence is clear;
the next attempt is no earlier than 24 hours after that safe failure's recorded
outcome. This clock survives replacement of the approval or code SHA. Ambiguous or incomplete publication,
dirty state, uncertain CAS or missing outcome evidence blocks further publication.
This is not a publish retry. Explicit operator pause revokes the active approval and
the pending permit; an already sent Meta request cannot be recalled. New code with
a different main SHA needs a fresh continuous approval.

```bash
python scripts/manage_feed_schedule.py enable-continuous --apply \
  --expected-generation GENERATION --approved-sha EXACT_MAIN_SHA \
  --cadence hourly_utc17 \
  --evidence-ref EXPLICIT_CONTINUOUS_APPROVAL
```

The existing fresh manual UUID workflow can start the first Feed immediately;
manual completion advances the same cooldown clock. Later scheduled posts alternate
carousel and single through `--mode auto`.

## Eligibility and admission

Feed cron is `17 * * * *`: one lightweight eligibility check each hour at UTC
minute **17**. Hourly approvals allow the first eligible window after the 48-hour
threshold; existing daily approvals and legacy permits remain restricted to
**17:17 UTC**. Continuous windows last 60 minutes and adjacent closed windows may
meet at expiry without overlapping. The
repository variable `ARTFOLIO_PRODUCTION_SCHEDULE_ENABLED=true` is only an outer
switch. A valid durable single-slot permit or continuous approval is also mandatory. Manual UUIDv4 Feed authorization
retains its existing 60-minute freshness and reservation consumption semantics and
needs no Stage B permit. Reel scheduling and the Reel workflow are unchanged.

`python scripts/manage_feed_schedule.py status --expected-sha EXACT_MAIN_SHA`
reads authoritative state/receipts and media ownership without mutation. It reports
the last successful completion, next eligible/attempt/check times, next format and
an actionable status (`READY`, cooldown/backoff/window waiting, active attempt,
paused, mismatched SHA or blocked). Invalid or unreadable evidence returns a nonzero
exit; ordinary waiting reports `ready=false`. The scheduled workflow runs this
check before expensive validation and gates later steps on an explicit `ready=true`.
This is advisory: actual admission and publication still reload every guard.

The Feed workflow invokes `--mode auto`: a finalized carousel is followed by a
single-image post, and a finalized single is followed by a carousel. With no
successful Feed history, it starts with a carousel. Selection reads validated
successful publications, ignores Reel and unresolved attempts, and is refreshed
after reconciliation. An unreadable history stops publication. Both formats use
the same one-use authorization, reservation CAS, irreversible boundary and receipt
completion gates. Alternation does not rearm a permit or change the 48-hour gate.

The optional top-level `feed_schedule_control` in the sealed v2 safety object has
its own schema version 1. Legacy objects without it remain readable; scheduled
Feed fails closed. Malformed/unsupported control fails strict state validation.
The existing canonical payload digest covers the whole control. Additive defaults
are resealed only in memory before the next existing safety CAS.

Control records a pause flag/reason, the latest successful Feed completion and
publication identity, derived next eligibility (+48 hours), and an append-only
permit ledger. Each permit contains UUIDv4 identity, one finite UTC slot, creation
and expiry times, approved main SHA, approval reference, lifecycle, optional owner
run ID/attempt and `schedule:<run_id>` authorization, reservation identity/generation,
terminal outcome and acknowledgement. Previous permits and evidence cannot disappear
or be rewritten during ordinary writes. This small ledger is retained for audit;
it is not a second transaction or receipt store.

Only a future 17:17 UTC slot can be armed in legacy single-slot mode. Expiry must be later than the slot and
at most 60 minutes after it. Both the authenticated run's creation time and admission
must fit that window; reservation and pre-publish must still be within it. Queued,
stale or delayed runs fail closed. No catch-up publication is allowed. GitHub's
existing 60-minute run freshness remains an additional gate.

The successful Feed clock comes from durable receipt completion evidence, matched
to finalized live Feed identities. It includes manual Feed publications (including
A3), excludes Reel and does not move for failed attempts. Unknown timestamps on recovered historical
receipts cannot supersede a strict, exact post-recovery live completion. Missing or
mismatched NEW Feed completion evidence fails closed. The requested slot,
admission, reservation and pre-publish must satisfy **>=48 hours** from that completion.
Manual Feed finalization advances the clock in the existing finalization CAS even
when a permit exists; a new scheduled reservation then fails if its cooldown is stale.
Receipt synchronization must complete before any later scheduled reservation.

In single-slot mode, admission claims one permit through the existing safety-state CAS and **immediately
pauses scheduling**. Owner/run attempt 1, repository, exact Feed workflow, schedule
event, main ref, head SHA, approved SHA and finite window are bound to authenticated
GitHub run metadata. The reservation rechecks ownership on its authoritative snapshot
and writes the publication lock, consumed authorization and `RESERVED` permit together.
The pre-publish callback rechecks the current owner, revocation, window, cooldown,
receipt/cleanup/owned-media gates and conflicts, then writes `PUBLISHING` with the
existing irreversible-boundary CAS. Boundary replay cannot authorize another request.

`ONE_OPERATOR_REVIEW -> AT_MOST_ONE_SCHEDULED_PUBLICATION_ATTEMPT`

Single-slot mode has no automatic rearming and no replacement publication following an admitted
failure. Failed outcome persistence never triggers a reservation or publish retry.
The existing application `media_publish` call remains non-retrying. Reconciliation
never publishes.

## Failures and evidence

Every admitted outcome leaves the permit owned and scheduling paused. Outcomes are
`SUCCESS`, `DEFINITIVE_FAILURE`, `AMBIGUOUS`, `CANCELLED` or `INCOMPLETE`, with completion
recording time, safety/receipt generations, publication/media IDs and cleanup and
reconciliation flags. Run, attempt, approved SHA, slot and authorization live in the
same immutable permit. Full receipts remain separate.

Before-reservation failures record definitive failure. Staging/child/parent failures
preserve the existing lifecycle locks and cleanup evidence. Definitive Meta rejection
may expire only with authoritative non-publication evidence. In-flight or unknown
publish results remain ambiguous. A hard kill/crash can leave no terminal application
outcome: the prior admission CAS still leaves scheduling paused and owned. A failed
outcome-state write similarly preserves the previous paused state. Operator review
cannot acknowledge unresolved PENDING/PUBLISHING/AMBIGUOUS or dirty receipts/cleanup.

Checkout, dependency setup, tests or preflight can fail **before application code
starts**. Application code cannot record such an outcome. The permit covers only one
finite daily window: tomorrow's cron cannot inherit it. It is effectively paused at
expiry even if its stored status still says ARMED. Acknowledgement records EXPIRED
and preserves authenticated GitHub slot-run evidence, including dependency failures,
cancellation or replaced pending runs. No elapsed timer rolls the window forward.
If a run never appeared, the authenticated complete empty slot audit is retained.

## Operator interface

`scripts/manage_feed_schedule.py` defaults to GET-only inspection. It has no publish,
receipt deletion, protection deletion, ambiguity clearing, migration or silent repair
command. Mutations require `--apply` and an exact `--expected-generation`; CAS conflicts
and uncertain write/readback outcomes stop without retry. Never use these mutation
commands in production without separate authorization.

```bash
python scripts/manage_feed_schedule.py inspect
python scripts/manage_feed_schedule.py pause --apply --expected-generation GENERATION --reason EVIDENCE
python scripts/manage_feed_schedule.py revoke --apply --expected-generation GENERATION --reason EVIDENCE
python scripts/manage_feed_schedule.py acknowledge --apply --expected-generation GENERATION --evidence-ref REVIEW
python scripts/manage_feed_schedule.py arm --apply --expected-generation GENERATION \
  --approved-sha EXACT_MAIN_SHA --slot FUTURE_YYYY_MM_DD_T17_17_00Z \
  --expires-at SAME_DATE_T18_17_00Z --evidence-ref OPERATOR_APPROVAL
```

Actual timestamps use ISO-8601 UTC, e.g. `YYYY-MM-DDT17:17:00Z` (the example placeholders
above must be replaced). Read current generation after every transition. `arm` verifies
the supplied SHA against authenticated GitHub main metadata and rejects dirty safety,
receipt, active transaction, cleanup, receipt sync, recovery and owned-media state.
It requires cooldown at the requested slot and acknowledgement of the previous permit.
Acknowledgement itself never arms another slot. Arming is a separate explicit action.

`pause` prevents new admission and revokes the current permit's future reservation or
pre-publish boundary, including an admitted owner. `revoke` accepts only an unconsumed
permit. Neither operation releases publication locks or erases an outcome. A request
already sent to Meta cannot be revoked; preserve ambiguous-outcome semantics.

Acknowledgement waits for the window to close, requires clean durable state, and
performs authenticated, paginated GETs of all Feed scheduled runs created in the slot.
All returned identities must match the approval and be terminal, including duplicate
runs. The owner must be present when admitted. Unknown, nonterminal, rerun, missing,
truncated or changing evidence rejects review. GitHub filtered searches capped at
1000 are rejected. Review references and all run IDs/conclusions are retained.
The API contract is documented by [GitHub workflow run REST documentation](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow).
Credentials come only from existing state/media configuration and `GITHUB_TOKEN`;
no credential values are printed or persisted.

## Separately authorized production migration

Keep Feed/Reel disabled and scheduler false. In a separate authorized task:

1. Load validated current safety state/strong ETag and exact receipt ledger. Verify
   recovery baselines, absence of state expiration, receipt coverage, no unresolved
   transactions/sync/cleanup, and owned-media consistency with read-only probes.
2. Require absent schedule control and the operator's expected safety generation.
3. Build `initial_control(state, ledger)` (a pure function). It returns schema 1,
   PAUSED / `STAGE_B_NOT_ARMED`, no permits, and the latest Feed completion ID/time
   and next eligible time from verified receipts. The A3 verified completion is
   `2026-10-01T17:54:07Z`, publication `66a4cac3-3db7-419d-bb05-74bd16a68801`;
   use the **actual current** latest receipt if later manual Feed success exists.
4. Add only `feed_schedule_control`, increment safety generation once, reseal the
   entire payload, and call the existing `update_safety(value, expected_etag)` CAS.
   No receipt, protection, quarantine, lifecycle or authorization history is altered.
5. Reload and verify exact generation/digest and PAUSED/no-permits. An uncertain
   result requires inspection, never blind migration retry.

The operator tool deliberately cannot initialize Stage B. There is no migration or
permit in live production from this development task. Migration does not authorize
arming, scheduler enablement, workflow enablement or publication.

## Emergency stop (future authorized procedure)

1. Set `ARTFOLIO_PRODUCTION_SCHEDULE_ENABLED=false`.
2. Disable the Feed workflow.
3. Keep Reel disabled.
4. Durably pause/revoke schedule admission with expected-generation CAS.
5. Inspect queued/running publication jobs and durable boundary evidence.
6. Cancel only when safe based on that evidence; a sent HTTP request cannot be stopped.
7. Preserve all PUBLISHING/AMBIGUOUS evidence and reconcile separately.
8. Never retry `media_publish`.

**Turning the scheduler variable off does not stop an already-running job.** The
existing non-cancelling workflow concurrency is preserved; a replaced pending run
cannot make a finite permit roll forward.
