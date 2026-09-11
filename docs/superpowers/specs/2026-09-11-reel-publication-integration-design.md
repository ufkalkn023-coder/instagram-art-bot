# Artfolio Reel Publication Integration Design

**Status:** Approved architecture, implementation not started
**Date:** 2026-09-11

## Goal

Add a manual, production-safe path that publishes one verified Artfolio Reel release package to Instagram while preserving the feed publisher's existing history, counters, lifecycle, and behavior.

## Scope

This design covers release intake, global artwork reservation, Reel staging, Instagram publication, durable lifecycle state, reconciliation, and cleanup. It does not cover Reel production, automatic repurposing, or the daily 4+4 scheduler. Publication remains an explicit one-release operation until this path is proven.

## Non-negotiable invariants

- Feed state remains backward compatible: `posted_artworks`, `publications`, and `grid_publication_count` keep their current meaning and validation.
- Reel state is additive and separate: `reel_reservations`, `reel_publications`, and `reel_publication_count`.
- Canonical artwork protection is global across feed and Reel reservations and publications.
- A protected or published canonical artwork cannot be automatically selected for the other format. There is no repurposing override in this phase.
- All history mutations use the existing `posted_history.json` R2 object and its exact quoted ETag compare-and-swap contract. No unconditional overwrite or second history store is introduced.
- `media_publish` is called at most once for a publication intent. Unknown outcomes are quarantined, never retried automatically.
- A Reel success changes only Reel state and `reel_publication_count`; it never changes `publications`, `grid_publication_count`, or grid tone.
- Reel cleanup operates only below `reels/publications/<publication_id>/` and can never list or delete `images/publications/` feed objects.

## Architecture and flow

The integration is a separate Reel publication orchestrator that reuses three existing boundaries:

1. The `artfolio-reels` release verifier establishes release-package trust.
2. `src.r2_media.stage_reel_mp4()` stages the verified MP4 in the Reel-only namespace.
3. `src.instagram_poster.post_to_instagram_graph_api()` publishes with `media_type="REELS"` and the existing `before_publish` callback.

For one explicit release request, the orchestrator:

1. Deep-verifies and snapshots the release package.
2. Extracts and pins its canonical artwork, release, package, render, and caption identities.
3. Atomically creates one global Reel reservation.
4. Stages the snapshotted MP4 with the existing Reel R2 API.
5. Creates and waits for the Instagram container through the existing publisher.
6. Durably records `container_id` and `PUBLISHING` in `before_publish`; only then may `media_publish` run.
7. Persists the returned `media_id`, fetches a permalink best-effort, and atomically finalizes the Reel publication.
8. If the publish outcome is unknown, preserves the global lock and reconciles without issuing another publish request.

## History schema

The existing R2 JSON document gains three additive fields and one Reel-specific cleanup queue:

```json
{
  "posted_artworks": [],
  "publications": [],
  "grid_publication_count": 0,
  "reel_reservations": [],
  "reel_publications": [],
  "reel_publication_count": 0,
  "reel_staging_cleanup_queue": []
}
```

Absent Reel fields mean empty Reel history for legacy documents. If `reel_publications` is non-empty, `reel_publication_count` must exist, be a non-negative integer, and equal `len(reel_publications)`. Malformed or contradictory Reel state fails closed before any Meta call. Existing feed validation remains unchanged.

### Reel reservation

Each `reel_reservations` entry is the durable lock and lifecycle record for one single-artwork Reel:

```json
{
  "publication_id": "<application UUID>",
  "artwork_id": "met_123",
  "status": "PENDING",
  "reserved_at": "2026-09-11T12:00:00Z",
  "release_identity": {
    "version": "artfolio-release-v1",
    "reel_id": "met_123",
    "created_at": "2026-09-11T11:55:00.000Z",
    "manifest_sha256": "<sha256 of manifest.json bytes>",
    "files_sha256": {
      "reel.mp4": "<sha256>",
      "caption.txt": "<sha256>",
      "metadata.json": "<sha256>",
      "qc/contact-sheet.png": "<sha256>"
    }
  },
  "staging": {
    "object_key": "reels/publications/<publication_id>/<timestamp>_<uuid>.mp4",
    "public_url": "https://<public-r2-host>/<object_key>",
    "staged_at": "2026-09-11T12:01:00Z"
  },
  "container_id": "<instagram creation container id>",
  "publish_started_at": "2026-09-11T12:02:00Z",
  "publish_response_media_id": "<instagram media id>",
  "media_id": "<instagram media id>",
  "posted_at": "2026-09-11T12:03:00Z",
  "permalink": "https://www.instagram.com/reel/.../"
}
```

Only fields valid for the current lifecycle state are present. `staging` is added after successful public R2 health validation. `container_id` and `publish_started_at` appear together at `PUBLISHING`. Ambiguity and expiry use the existing timestamp/reason conventions: `ambiguous_at` plus `ambiguity_reason`, or `expired_at` plus `expiration_reason`. Reconciliation appends `last_reconciled_at`, `reconciliation_attempt_count`, `reconciliation_result`, and `reconciliation_evidence`.

`release_identity` is immutable after reservation. `reel_id` must equal the normalized `artwork_id` in this single-artwork phase. The manifest digest identifies the exact package, while `files_sha256["reel.mp4"]` identifies the rendered bytes supplied to staging. A repeated operation must match the entire pinned identity or fail.

### Reel publication

Each `reel_publications` entry is an immutable success index analogous to, but separate from, a feed `PublicationRecord`:

```json
{
  "id": "<publication_id>",
  "artwork_id": "met_123",
  "media_id": "<instagram media id>",
  "posted_at": "2026-09-11T12:03:00Z",
  "permalink": "https://www.instagram.com/reel/.../",
  "release_identity": { "...": "exact copy of the pinned identity" }
}
```

`permalink` is optional and may be added later if the best-effort lookup initially fails. Publication IDs and Instagram media IDs must be unique within Reel history and must not conflict with feed publication IDs or media IDs. Finalization verifies that the reservation and success record have identical artwork and release identities.

### Reel cleanup queue

`reel_staging_cleanup_queue` contains unique `{publication_id, eligible_at, reason}` entries. It is deliberately separate from the existing feed `staging_media_cleanup_queue`, so an acknowledgement cannot accidentally clear work for the wrong namespace.

## Global deduplication and reservation

One shared predicate defines whether a normalized canonical artwork ID is protected. It checks both formats in the same loaded history snapshot:

- Feed: matching `posted_artworks` rows and confirmed `publications`.
- Reel: matching `reel_reservations` rows and confirmed `reel_publications`.

`PENDING`, `PUBLISHING`, `AMBIGUOUS`, and `PUBLISHED` protect an artwork. A confirmed publication always protects it even if its reservation row is missing or malformed; contradictory state fails closed. `EXPIRED` and safely stale pre-Meta `PENDING` reservations do not protect because no publication occurred. Stale replacement must be decided and written in the same CAS mutation as the new reservation.

Both feed and Reel reservation entry points must call this predicate. A Reel reservation therefore fails if feed owns the artwork, and a feed reservation fails if Reel owns it. The check and insertion are one conditional R2 write; a check followed by a separate write is forbidden. Canonicalization continues to use `normalize_artwork_id()`, including existing legacy-prefix aliases.

There is no automatic or manual repurposing flag in this phase. In particular, `PUBLISHED` and `AMBIGUOUS` are permanent automatic-selection exclusions. An `EXPIRED` pre-Meta attempt may be retried because it did not publish content.

## Release-package trust boundary

The publication integration accepts a local `artfolio-reels` release directory or release ID, never an arbitrary MP4/caption pair.

Before reservation it must:

1. Invoke the authoritative `artfolio-reels` verifier in its repository with `reels:verify-release -- <release> --deep --json`.
2. Require process success, parse JSON strictly, and require `valid: true`, no errors, and the expected release directory/reel ID.
3. Accept only `artfolio-release-v1` with the exact package contract: `reel.mp4`, `caption.txt`, `metadata.json`, `manifest.json`, and `qc/contact-sheet.png`.
4. Reject symlinks, non-regular/empty files, path escapes, unexpected manifest file names, missing or extra hash entries, invalid SHA-256 values, and any manifest/metadata/reel identity mismatch.
5. Recompute every manifest-listed hash and the raw `manifest.json` hash after deep verification.
6. Copy `reel.mp4` and read `caption.txt` into a private per-operation snapshot while hashing them. The snapshot hashes must still match the manifest before reservation and staging. Staging uses this snapshot, not a path that a later release overwrite could change.

The deep verifier remains the sole owner of codec, dimensions, frame rate, duration, full decode, required audio, audibility, source ReelData, and release metadata verification. The Art Bot intake owns only package containment, immutable byte identity, strict subprocess output handling, and conversion into its internal typed model. Invalid intake produces no history mutation, R2 upload, or Meta call.

The local package path is diagnostic context, not durable identity, and is not stored in R2. The pinned hashes and release metadata are the durable identity.

## Lifecycle and failure semantics

```text
PENDING -> PUBLISHING -> PUBLISHED
                       -> AMBIGUOUS
PENDING/pre-Meta failure -> EXPIRED
AMBIGUOUS --verified media identity--> PUBLISHED
```

Allowed transitions are monotonic:

| From | To | Required evidence |
| --- | --- | --- |
| none | `PENDING` | Deep-verified immutable package snapshot and successful global CAS reservation. |
| `PENDING` | `PUBLISHING` | Exact container ID durably written by `before_publish` before `media_publish`. |
| `PUBLISHING` | `PUBLISHED` | Returned or independently verified Instagram media ID. |
| `PUBLISHING` | `AMBIGUOUS` | `media_publish` may have run but no reliable media ID was obtained. |
| `PENDING` | `EXPIRED` | Failure or TTL expiry occurred before `media_publish`. |
| `PUBLISHING` | `EXPIRED` | Only direct local evidence that `before_publish` persisted but the publisher did not invoke `media_publish`; this is still a pre-Meta failure. |
| `AMBIGUOUS` | `PUBLISHED` | Reconciliation verifies a specific media ID. |

`PUBLISHED` and `EXPIRED` are terminal for that reservation. `AMBIGUOUS` never expires by age and never returns to `PENDING`. A parsed 4xx, timeout, connection reset, malformed response, missing response ID, 5xx, or unexpected exception after `media_publish` begins is conservative `AMBIGUOUS` for Reels, even where the feed path currently treats a parsed 4xx as definitive. This format-specific rule preserves the approved no-auto-republish contract without changing feed behavior.

If a lifecycle CAS write loses a race, it reloads the current document and re-evaluates the transition. It may accept an exact idempotent result; it must reject a conflicting state, container, media ID, artwork, or release identity.

## R2 staging and cleanup

Staging calls the existing `stage_reel_mp4(snapshot_path, publication_id)` API. Its current contract remains authoritative:

- Keys are generated only under `reels/publications/<publication_id>/<timestamp>_<uuid>.mp4`.
- Upload content type is `video/mp4`.
- The returned immutable `TempReelUpload` supplies the exact object key, public URL, and owning publication ID.
- Public validation requires HTTP 200, `video/mp4`, and positive `Content-Length`.
- A failed public health check attempts exact-key cleanup.

After successful staging, the handle is persisted on the still-`PENDING` reservation with CAS before container creation. Failure to persist it is a pre-Meta failure and no container is created.

Authoritative pre-Meta expiry and cleanup-queue insertion are one history CAS. Cleanup runs only after that state is durable, calls `cleanup_publication_reels()`, and acknowledges the Reel queue only after complete cleanup. The existing object/page bounds and ownership revalidation remain in force. Failed cleanup leaves the queue entry for a later bounded retry and never reopens lifecycle state.

Cleanup retains media for `PENDING`, `PUBLISHING`, `AMBIGUOUS`, and `PUBLISHED`. Only `EXPIRED` is eligible. Exact-handle rollback is allowed before any Meta container operation when the current invocation owns the handle. Prefix cleanup always uses the Reel validator and Reel prefix builder; it never calls `cleanup_publication_media()` and never lists `images/publications/`.

## Instagram publish boundary

The orchestrator calls:

```python
post_to_instagram_graph_api(
    media_url=staged.public_url,
    caption=verified_caption,
    account_id=account_id,
    access_token=access_token,
    media_type="REELS",
    before_publish=before_publish,
)
```

The existing publisher creates the Reel container, waits for `FINISHED`, invokes `before_publish(container_id, ())`, then calls the non-retrying `media_publish` operation. `before_publish` must atomically persist `PUBLISHING`, `container_id`, and `publish_started_at` against the pinned reservation. If that callback does not complete durably, the publisher raises `InstagramPrePublishBoundaryError` and does not call `media_publish`.

On a successful response, the orchestrator first persists `publish_response_media_id` as a durable receipt. It then fetches the permalink best-effort and performs one idempotent CAS finalization that:

- moves the reservation to `PUBLISHED`;
- persists `media_id`, `posted_at`, and the optional permalink;
- appends exactly one matching `reel_publications` record;
- increments `reel_publication_count` by exactly one;
- leaves all feed fields and grid tone unchanged.

If the permalink lookup or later permalink persistence fails, the Reel remains published and reconciliation may add the permalink later. If finalization fails after Meta success, the durable receipt or container ID keeps the reservation protected for reconciliation; the operation reports incomplete persistence and never republishes.

## Ambiguous recovery

Reel reconciliation is a separate bounded path and never selects artwork, creates containers, stages media, or calls `media_publish`.

For each unresolved Reel reservation:

1. Expire stale `PENDING` entries because the publish boundary was never crossed, queueing Reel cleanup atomically.
2. Respect the existing publishing grace window before inspecting a `PUBLISHING` entry.
3. If `publish_response_media_id` exists, idempotently finalize `PUBLISHED` without any Meta publish call.
4. Otherwise query the durable `container_id` with the existing bounded container-status API.
5. Without a verified media ID, record evidence and keep or move the entry to `AMBIGUOUS`, including for `FINISHED`, `IN_PROGRESS`, `ERROR`, `EXPIRED`, `PUBLISHED`, lookup errors, and unknown identity. Container state alone is not enough to create a `reel_publications` row.
6. Support explicit operator media-ID recovery by requiring an unresolved `PUBLISHING`/`AMBIGUOUS` reservation, a durable container whose status is `PUBLISHED`, and a Graph identity lookup that returns the exact supplied media ID. Only then finalize and fetch the permalink best-effort.

This conservative rule means an ambiguous attempt cannot become automatically eligible for another publish. If Meta says the container is published but its media identity is unavailable, the lock remains `AMBIGUOUS` until operator evidence resolves it.

## CAS and concurrency guarantees

- Reads preserve the exact quoted R2 ETag as an opaque validator.
- First creation uses `If-None-Match: *`; updates use `If-Match: <exact ETag>`.
- HTTP 412 becomes `ConcurrentWriteError`. There is no last-writer-wins fallback.
- Every retry reloads the whole document and repeats validation, global collision checks, and transition preconditions. The same stale ETag is never retried blindly.
- Reservation, pre-publish transition, expiry plus cleanup enqueue, durable receipt, finalization, permalink addition, and cleanup acknowledgement are separate bounded CAS mutations.
- Finalization is idempotent: the exact existing Reel publication is success with no counter change; any mismatch fails closed.
- The finalization CAS is the atomic boundary for `PUBLISHED`, `reel_publications`, and `reel_publication_count`.
- Feed and Reel writers preserve unknown top-level keys. A concurrent feed/Reel reservation for the same artwork can have at most one winner because both check both domains before a conditional write.
- `PUBLISHING` and `AMBIGUOUS` are durable quarantine states. Process crashes, retry storms, or scheduler delivery cannot bypass them.

## Failure handling summary

| Failure point | Durable result | External action |
| --- | --- | --- |
| Deep verification, package parsing, hash, or snapshot failure | No reservation | No R2 or Meta call. |
| Global reservation collision/CAS exhaustion | Existing owner unchanged | No R2 media or Meta call. |
| Reel staging/health validation fails | `EXPIRED` when reservation was created | Exact cleanup plus queued Reel-prefix recovery. |
| Staging handle cannot be persisted | `EXPIRED` | No Meta container; cleanup Reel namespace. |
| Container creation or processing fails before callback | `EXPIRED` | No `media_publish`; cleanup Reel namespace. |
| `before_publish` cannot be confirmed durable | Protected until state is re-read, then pre-Meta `EXPIRED` when provable | Publisher does not call `media_publish`. |
| Publish response is uncertain or any post-boundary error lacks a verified media ID | `AMBIGUOUS` (or conservatively remains `PUBLISHING` if history is unavailable) | Never retry publish; reconcile only. |
| Media ID returned but history receipt/finalization fails | `PUBLISHING`/`AMBIGUOUS` with best available evidence | Never retry publish; reconcile using receipt/container/operator ID. |
| Permalink lookup/persistence fails | `PUBLISHED` | Keep success; retry only permalink enrichment. |
| Cleanup fails | `EXPIRED` plus queue entry | Retry bounded Reel cleanup later; do not alter publication state. |

## Test strategy

All tests are networkless unless an explicitly approved integration smoke is run after implementation.

### Schema and compatibility

- Legacy feed-only history loads with absent Reel fields as empty/zero.
- Reel count must equal Reel publication rows; malformed identities, statuses, timestamps, URLs, hashes, and cross-record mismatches fail closed.
- Existing feed models, publication counts, grid tone, and history fixtures remain unchanged.

### Global deduplication and CAS

- Feed-active/feed-published blocks Reel reservation; Reel-active/Reel-published blocks feed reservation.
- Legacy canonical aliases collide after `normalize_artwork_id()`.
- `EXPIRED` and safely stale pre-Meta reservations can be replaced; `AMBIGUOUS` cannot.
- Deterministic two-writer tests prove concurrent feed/Reel reservation has one winner.
- Create-if-absent, matching `If-Match`, stale ETag, bounded reload/re-evaluation, and conflict exhaustion retain current R2 semantics.

### Release intake

- Require successful deep verifier execution and strict valid JSON output.
- Reject invalid package version, missing/extra hash entries, traversal, symlinks, non-regular files, identity mismatch, hash mismatch, missing audio/decode verification, and package changes during snapshotting.
- Prove the snapshotted MP4 and caption hashes equal the pinned reservation identity.

### Publish lifecycle

- Assert the existing publisher receives `media_type="REELS"` and the verified caption/R2 URL.
- Assert `before_publish` durably writes the exact container ID before the mocked `media_publish` call.
- Assert boundary-write failure makes zero `media_publish` calls.
- Cover successful receipt/finalization, idempotent replay, permalink absence, each ambiguous publisher error, and history failure after Meta success.
- Prove Reel success increments only `reel_publication_count`; feed publication rows, `grid_publication_count`, and grid tone are unchanged.

### Reconciliation and cleanup

- Reconciliation never calls container creation or `media_publish`.
- Durable receipt and verified operator media ID finalize exactly once.
- Container status without media identity remains ambiguous and never becomes automatically publishable.
- Only authoritatively expired pre-Meta reservations enter `reel_staging_cleanup_queue`.
- Cleanup lists/deletes only the exact Reel prefix, fails closed on image or cross-publication keys, respects bounds, and acknowledges only complete cleanup.

Run focused tests first, then the full existing test suite, and finally `git diff --check`. A live smoke is a separate, explicitly authorized step because it publishes externally.

## Sequential implementation decomposition

1. **Add typed Reel history models and validators.** Introduce reservation, release identity, publication, counter, and Reel cleanup-queue validation with legacy-field compatibility tests. No publish entry point yet.
2. **Add the shared global protection predicate and CAS reservation mutation.** Route existing feed reservation checks and new Reel reservations through it; prove both collision directions and the two-writer race while preserving feed outputs.
3. **Add release intake and immutable snapshotting.** Wrap the existing deep JSON verifier, validate package containment/identity, compute hashes, and return a typed verified snapshot. Keep it read-only and networkless outside the verifier.
4. **Add Reel staging state and cleanup queue operations.** Persist `TempReelUpload` ownership, pre-Meta expiry, Reel-only queue selection/acknowledgement, and bounded cleanup using the existing R2 Reel APIs.
5. **Add the Reel publication orchestrator.** Wire verified snapshot -> global reservation -> staging -> existing Instagram publisher, enforcing the durable callback and Reel-specific failure mapping. Expose only an explicit manual release command; add no schedule.
6. **Add idempotent Reel receipt and finalization.** Atomically append the success index and increment only `reel_publication_count`, with best-effort permalink enrichment and feed-regression tests.
7. **Add Reel reconciliation and operator media-ID recovery.** Reuse bounded status/identity lookups, forbid publish calls, preserve ambiguous locks, and process the Reel cleanup queue independently.
8. **Run compatibility verification.** Execute focused Reel tests, the complete existing suite, static checks used by the repository, and `git diff --check`; only then request separate approval for any live publish smoke.

Each task is reviewable and testable before the next begins. The daily 4+4 scheduler remains a separate design after this manual publication path has passed deterministic tests and an explicitly approved live smoke.
