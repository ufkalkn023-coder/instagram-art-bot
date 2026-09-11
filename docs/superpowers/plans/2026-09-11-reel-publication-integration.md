# Reel Publication Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a manual, production-safe path that publishes exactly one deeply verified `artfolio-reels` release package to Instagram Reels without changing feed publication semantics.

**Architecture:** Extend the existing `posted_history.json` document with strictly validated Reel-only records and route feed and Reel reservation decisions through one global canonical-artwork protection predicate under the existing quoted-ETag CAS contract. Add a local release-package trust boundary that invokes the authoritative deep verifier and snapshots verified bytes, then compose the existing Reel R2 staging and Instagram publishing APIs behind Reel-specific lifecycle, reconciliation, and cleanup operations. Expose only a standalone manual one-release command; do not add Reel publishing to the feed runner or any scheduler.

**Tech Stack:** Python 3, Pydantic 2.12.5, pytest 9.0.2, boto3/R2 conditional writes, `subprocess`, `hashlib`, `pathlib`, existing Instagram Graph and R2 adapters.

**Spec:** `docs/superpowers/specs/2026-09-11-reel-publication-integration-design.md`

## Global Constraints

- The approved design is authoritative; do not redesign feed publication.
- Keep `posted_artworks`, `publications`, `grid_publication_count`, `active_color_tone`, feed lifecycle rules, and legacy feed validation behavior unchanged.
- Store Reel state only as additive `reel_reservations`, `reel_publications`, `reel_publication_count`, and `reel_staging_cleanup_queue` keys in the existing `posted_history.json` object. Do not create another history store.
- Every history mutation must preserve unknown top-level keys and use `load_history_with_etag()` plus `_upload_history()` with `If-None-Match: *` or the exact quoted `If-Match` ETag. A 412 remains `ConcurrentWriteError`; every bounded retry reloads and revalidates the whole document.
- Canonical artwork protection is global across feed and Reel state and uses `normalize_artwork_id()`, including `artic_` to `aic_` and `cma_` to `cleveland_` aliases.
- `PENDING`, `PUBLISHING`, `AMBIGUOUS`, and `PUBLISHED` protect Reel artwork. `AMBIGUOUS` never expires or becomes publishable automatically. Only `EXPIRED` and safely stale pre-Meta `PENDING` reservations are reusable.
- The release intake accepts a release ID or release directory, invokes `npm run reels:verify-release -- <release> --deep --json` in the configured `artfolio-reels` repository, and stages only a private verified snapshot.
- Reuse `r2_media.stage_reel_mp4()` and `r2_media.cleanup_publication_reels()` unchanged. Do not change `stage_temp_media()`, `cleanup_publication_media()`, or any `images/publications/` behavior.
- Reuse `instagram_poster.post_to_instagram_graph_api(..., media_type="REELS", before_publish=...)` unchanged. `media_publish` may run at most once per intent.
- A successful Reel changes only Reel records and `reel_publication_count`; it must not change feed rows, `grid_publication_count`, or grid tone.
- Do not add a scheduler, workflow trigger, automatic 4+4 behavior, automatic repurposing, or a live-publish test.
- Do not modify, move, recreate, or delete the existing `stable-instagram-art-bot-v1` tag or otherwise change the approved stable checkpoint.
- Each task below is one review and one commit boundary. Do not fold unrelated refactors into a task.

## Planned File Map

- Modify `src/models.py`: strict immutable Reel history value models.
- Modify `src/history_tracker.py`: Reel schema validation, global protection, Reel reservation, lifecycle CAS mutations, Reel reconciliation state, and Reel cleanup-queue operations over the existing history object.
- Create `src/reel_release.py`: authoritative verifier invocation, strict local package validation, hashing, and private snapshot lifecycle.
- Create `src/reel_reconciliation.py`: bounded Reel-only reconciliation and operator media-ID recovery.
- Create `src/reel_publication.py`: manual one-release orchestration and Reel-specific failure mapping.
- Create `scripts/publish_reel.py`: explicit manual CLI only.
- Modify `tests/test_publication_history.py`: feed compatibility and Reel-to-feed collision regressions.
- Create `tests/test_reel_history.py`: Reel schema, reservation, global deduplication, and CAS tests.
- Create `tests/test_reel_release.py`: verifier/package/snapshot trust-boundary tests.
- Create `tests/test_reel_publication_lifecycle.py`: Reel lifecycle, receipt, finalization, and counter-isolation tests.
- Create `tests/test_reel_reconciliation.py`: conservative reconciliation, operator recovery, and Reel-only cleanup tests.
- Create `tests/test_reel_publication.py`: orchestration, failure mapping, CLI, and no-live-call tests.
- Re-run existing `tests/test_r2_reel_media.py`, `tests/test_r2_media_cleanup.py`, `tests/test_instagram_poster.py`, `tests/test_production_workflow.py`, and `tests/test_ambiguous_publish_lifecycle.py` without modifying them.

Files intentionally outside the change set: `main.py`, `src/r2_media.py`, `src/instagram_poster.py`, `src/publication_reconciliation.py`, all scheduler/workflow files, dependency manifests/locks, and Git tags.

---

### Task 1: Reel History Schema and Global Feed/Reel Artwork Reservation

**Files:**

- Modify: `src/models.py`
- Modify: `src/history_tracker.py`
- Modify: `tests/test_publication_history.py`
- Create: `tests/test_reel_history.py`

**Interfaces:**

- Consumes:
  - `src.models.normalize_artwork_id(artwork_id: str) -> str`
  - `src.history_tracker.load_history_with_etag() -> tuple[dict[str, Any], str | None]`
  - `src.history_tracker._upload_history(history: dict[str, Any], etag: str | None) -> None`
  - Existing `ConcurrentWriteError`, `CorruptedHistoryError`, `PublicationStatus`, `PENDING_RESERVATION_TTL`, and `HISTORY_CONDITIONAL_WRITE_ATTEMPTS`.
- Produces in `src.models`:
  - `ReelPublicationStatus(str, Enum)` with exactly `PENDING`, `PUBLISHING`, `PUBLISHED`, `AMBIGUOUS`, and `EXPIRED`.
  - `ReelReleaseIdentity(BaseModel)` with frozen/forbid-extra fields `version`, `reel_id`, `created_at`, `manifest_sha256`, and exact `files_sha256` keys `reel.mp4`, `caption.txt`, `metadata.json`, `qc/contact-sheet.png`.
  - `ReelStagingRecord(BaseModel)` with frozen/forbid-extra fields `object_key`, `public_url`, and `staged_at`.
  - `ReelReservationRecord(BaseModel)` implementing the state-dependent field rules from the spec.
  - `ReelPublicationRecord(BaseModel)` with `id`, `artwork_id`, `media_id`, `posted_at`, optional `permalink`, and `release_identity`.
  - `ReelCleanupQueueEntry(BaseModel)` with `publication_id`, `eligible_at`, and `reason`.
  - `ValidatedReelHistory(dataclass)` containing typed tuples `reservations`, `publications`, `cleanup_queue`, plus `publication_count`.
- Produces in `src.history_tracker`:
  - `_validated_reel_history(history: Mapping[str, Any]) -> ValidatedReelHistory`
  - `globally_protected_artwork_ids(history: Mapping[str, Any], *, now: datetime) -> set[str]`
  - `artwork_is_globally_protected(history: Mapping[str, Any], artwork_id: str, *, now: datetime) -> bool`
  - `reserve_reel(artwork_id: str, release_identity: ReelReleaseIdentity, publication_id: str | None = None) -> str`
  - Existing `reserve_artworks(...)` and `get_posted_ids()` updated to consume the same global predicate while preserving their public signatures and feed outputs.

- [ ] **Step 1: Write the failing strict-schema tests**

  Add table-driven tests in `tests/test_reel_history.py` that construct a valid reservation/publication pair, then independently corrupt status-specific fields, aware timestamps, HTTPS URLs, UUID publication IDs, SHA-256 values, exact hash keys, `reel_id == normalize_artwork_id(artwork_id)`, duplicate Reel publication IDs/media IDs, reservation/publication identity parity, and cross-format publication/media-ID uniqueness. Include these legacy assertions:

  ```python
  def test_legacy_feed_only_history_has_empty_reel_state_without_mutation():
      history = {"posted_artworks": [{"id": "artic_84774"}]}
      original = copy.deepcopy(history)

      state = history_tracker._validated_reel_history(history)

      assert state.reservations == ()
      assert state.publications == ()
      assert state.publication_count == 0
      assert state.cleanup_queue == ()
      assert history == original


  def test_nonempty_reel_publications_require_exact_counter():
      history = valid_reel_history()
      history.pop("reel_publication_count")

      with pytest.raises(history_tracker.CorruptedHistoryError, match="reel_publication_count"):
          history_tracker._validated_reel_history(history)
  ```

  Assert absent Reel fields read as empty/zero, while any present Reel field has the exact required container type. Assert malformed/contradictory Reel state fails before `_upload_history`, R2 staging, or Instagram mocks can be called.

- [ ] **Step 2: Run the schema tests and confirm the red state**

  Run: `pytest tests/test_reel_history.py -q`

  Expected: collection or test failure because the Reel models and `_validated_reel_history()` do not exist.

- [ ] **Step 3: Implement the minimal additive Reel models and validation**

  Add strict Pydantic models in `src/models.py`. Use `ConfigDict(extra="forbid", frozen=True)`, `datetime.fromisoformat(value.replace("Z", "+00:00"))` for aware timestamps, `urlsplit()` for HTTPS URLs, `uuid.UUID()` for application UUIDs, and a lowercase 64-hex validator. Enforce lifecycle shapes rather than accepting a union of arbitrary optional fields:

  ```python
  class ReelReleaseIdentity(BaseModel):
      model_config = ConfigDict(extra="forbid", frozen=True)
      version: Literal["artfolio-release-v1"]
      reel_id: str
      created_at: str
      manifest_sha256: str
      files_sha256: dict[str, str]


  @dataclass(frozen=True)
  class ValidatedReelHistory:
      reservations: tuple[ReelReservationRecord, ...]
      publications: tuple[ReelPublicationRecord, ...]
      publication_count: int
      cleanup_queue: tuple[ReelCleanupQueueEntry, ...]
  ```

  `_validated_reel_history()` must not insert defaults into `history`. If every Reel field is absent, return the empty typed state without invoking new feed validation, so legacy feed-only reads keep their current behavior. If `reel_publications` is empty or absent, an absent count reads as zero; if it is non-empty, the count must be present and equal its length. Validate reservation/publication matches and cross-format publication/media IDs when Reel state is present without changing `_validated_publications()` or `_grid_publication_count()` behavior.

- [ ] **Step 4: Run schema tests to green**

  Run: `pytest tests/test_reel_history.py -q`

  Expected: all schema/legacy tests pass.

- [ ] **Step 5: Write the failing global-deduplication and CAS tests**

  In `tests/test_reel_history.py`, cover feed-active/feed-published blocking `reserve_reel()`, Reel-active/Reel-published blocking `reserve_artworks()`, legacy aliases colliding in both directions, exact-identity idempotent replay for the same Reel `publication_id`, conflicting replay rejection, `AMBIGUOUS` protection, and replacement after `EXPIRED` or safely stale pre-Meta `PENDING`. In `tests/test_publication_history.py`, prove feed reservation records and return values remain byte-for-byte equivalent when Reel fields are absent.

  Add a deterministic in-memory R2 double whose first two readers receive the same quoted ETag and whose second conditional write raises `ConcurrentWriteError`; assert a feed/Reel race for one canonical ID has exactly one winner after bounded reload/re-evaluation. Also assert first creation uses `etag=None`, updates retain the exact quoted ETag, stale ETags are not retried, and exhausted conflicts raise.

  ```python
  def test_reel_ambiguous_blocks_feed_reservation(monkeypatch):
      history = history_with_reel_reservation(status="AMBIGUOUS", artwork_id="artic_84774")
      install_history(monkeypatch, history)

      with pytest.raises(RuntimeError, match="already protected"):
          history_tracker.reserve_artwork(artwork("aic_84774"))
  ```

- [ ] **Step 6: Run deduplication tests and confirm the red state**

  Run: `pytest tests/test_reel_history.py tests/test_publication_history.py -q`

  Expected: failures show feed reservation/get-posted IDs do not yet see Reel locks and `reserve_reel()` is absent.

- [ ] **Step 7: Implement the shared predicate and bounded CAS reservations**

  `globally_protected_artwork_ids()` must validate Reel state, then combine:

  - every non-expired, non-stale feed `posted_artworks` ID, retaining legacy missing-status locks;
  - every individually valid feed `publications[*].artwork_ids` ID, including a confirmed publication whose `posted_artworks` reservation is absent;
  - Reel reservation IDs in `PENDING`, `PUBLISHING`, `AMBIGUOUS`, or `PUBLISHED`, excluding only safely stale `PENDING` and `EXPIRED`;
  - every confirmed Reel publication artwork ID, which always protects even if its reservation is absent.

  Make `get_posted_ids()` return this set. In both `reserve_artworks()` and `reserve_reel()`, load the whole document, validate, check collisions, mutate, and conditionally write inside a maximum-three-attempt loop. On conflict, restore any in-place test-visible snapshot, reload, and repeat all validation/collision checks. Preserve feed stale-row replacement exactly; append Reel attempts so expired Reel audit records remain. An existing same-UUID Reel reservation is idempotent only if normalized artwork and the entire frozen release identity match.

- [ ] **Step 8: Run focused compatibility verification**

  Run: `pytest tests/test_reel_history.py tests/test_publication_history.py tests/test_r2_integration_safety.py tests/integration/test_r2_conditional_writes.py -q`

  Expected: all pass; feed-only fixtures are unchanged, exact quoted ETags are preserved, and the race has one winner.

- [ ] **Step 9: Review and commit Task 1 only**

  Inspect: `git diff -- src/models.py src/history_tracker.py tests/test_publication_history.py tests/test_reel_history.py`

  Commit boundary:

  ```bash
  git add src/models.py src/history_tracker.py tests/test_publication_history.py tests/test_reel_history.py
  git commit -m "feat: add global Reel publication reservations"
  ```

---

### Task 2: Verified `artfolio-reels` Release-Package Intake

**Files:**

- Create: `src/reel_release.py`
- Create: `tests/test_reel_release.py`

**Interfaces:**

- Consumes:
  - `src.models.ReelReleaseIdentity`
  - `src.models.normalize_artwork_id(artwork_id: str) -> str`
  - The external command `npm run reels:verify-release -- <release> --deep --json`, executed with `cwd` set to the selected `artfolio-reels` repository.
- Produces:
  - `ReleaseIntakeError(ValueError)` for all verifier, containment, schema, identity, hash, and snapshot failures.
  - `VerifiedReelRelease(dataclass, frozen=True)` with `release_directory: Path`, `snapshot_directory: Path`, `video_path: Path`, `caption_path: Path`, `caption: str`, `artwork_id: str`, and `release_identity: ReelReleaseIdentity`.
  - `verified_reel_release_snapshot(release: str | Path, *, reels_repository: str | Path, snapshot_root: str | Path | None = None) -> ContextManager[VerifiedReelRelease]`.

- [ ] **Step 1: Write the failing verifier and package-contract tests**

  Build releases under `tmp_path` with exactly `reel.mp4`, `caption.txt`, `metadata.json`, `manifest.json`, and `qc/contact-sheet.png`. Monkeypatch `subprocess.run`; never invoke Node, ffprobe, ffmpeg, R2, or Meta in these unit tests. Cover:

  - non-zero verifier exit, timeout/OSError, empty/multiple/trailing stdout, invalid JSON, non-object JSON, `valid != true`, non-empty errors, wrong directory, wrong Reel ID, missing `media`, `media.deep != true`, missing audio, and missing/non-finite `maxVolumeDb`;
  - only `artfolio-release-v1` and the exact manifest `files` mapping;
  - no extra/missing package files or manifest hash entries;
  - lowercase 64-character SHA-256 only;
  - no symlink at any package component, no non-regular/empty file, no path escape;
  - manifest `reelId`, metadata `canonicalId`, metadata `reelId`, verifier `reelId`, release-directory/ID selection, and normalized artwork identity must agree;
  - metadata must be strict and must tie `generatedAt` to manifest `createdAt`;
  - each listed file hash and raw `manifest.json` hash is recomputed after verifier success;
  - source mutation during MP4/caption snapshot fails and cleans the private directory.

  ```python
  def test_invalid_deep_verifier_result_creates_no_snapshot(monkeypatch, tmp_path):
      release = build_release(tmp_path)
      monkeypatch.setattr(
          subprocess,
          "run",
          lambda *args, **kwargs: CompletedProcess(args[0], 0, '{"valid":false,"errors":["decode failed"]}', ""),
      )

      with pytest.raises(reel_release.ReleaseIntakeError, match="deep verifier"):
          with reel_release.verified_reel_release_snapshot(
              release, reels_repository=tmp_path / "artfolio-reels", snapshot_root=tmp_path / "snapshots"
          ):
              pytest.fail("invalid intake must not yield")

      assert not (tmp_path / "snapshots").exists() or not any((tmp_path / "snapshots").iterdir())
  ```

- [ ] **Step 2: Run intake tests and confirm the red state**

  Run: `pytest tests/test_reel_release.py -q`

  Expected: import/collection failure because `src.reel_release` does not exist.

- [ ] **Step 3: Implement strict verifier execution and parsing**

  Resolve the repository and release without shell interpolation. For a directory input, pass its absolute path; for an ID, require one safe `[A-Za-z0-9][A-Za-z0-9_-]*` component and expect `<reels_repository>/output/releases/<id>`. Execute exactly:

  ```python
  subprocess.run(
      ["npm", "run", "reels:verify-release", "--", verifier_target, "--deep", "--json"],
      cwd=reels_repository,
      stdin=subprocess.DEVNULL,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
      timeout=300,
      check=False,
  )
  ```

  Bound stdout/stderr to 2 MiB before parsing or reporting, parse exactly one JSON object with no non-whitespace suffix, and expose only sanitized failure class/context. Require return code zero, `valid is True`, `errors == []`, the exact resolved `directory`, expected normalized `reelId`, and deep media evidence whose `path` is the package `reel.mp4`, `sizeBytes` is positive, `deep is True`, video is H.264 at 1080x1920 and 30 FPS, duration is positive and at most 60.5 seconds, audio codec is non-empty, and `maxVolumeDb` is finite and greater than -90.

- [ ] **Step 4: Implement containment, schema, hashing, and immutable snapshotting**

  Use `lstat()` for every directory/file component and reject symlinks before following them. Compare the recursive relative-file inventory to the exact five-file set and the directory inventory to the release root plus `qc` only. Parse `manifest.json` and `metadata.json` with strict key sets matching the external package contract. Resolve each manifest path and verify it remains under the release directory.

  Recompute all four manifest-listed hashes plus the raw manifest hash. Create the snapshot with `tempfile.mkdtemp(dir=snapshot_root)`, mode `0700`; copy MP4 and caption through no-follow source file descriptors into exclusive destination files while hashing; require the copied hashes to match the manifest; decode caption as UTF-8 and require non-whitespace text; chmod snapshot files `0400`. Yield the frozen `VerifiedReelRelease` and remove only its private snapshot directory in the context manager's `finally`. Never persist `release_directory` in history.

- [ ] **Step 5: Prove invalid intake cannot cross external boundaries**

  Add a parametrized test that wraps all invalid fixtures with `Mock` objects for `history_tracker.reserve_reel`, `r2_media.stage_reel_mp4`, and `instagram_poster.post_to_instagram_graph_api`; assert each has zero calls. This is a trust-boundary test, not an orchestrator test.

- [ ] **Step 6: Run focused intake verification**

  Run: `pytest tests/test_reel_release.py -q`

  Expected: all pass; valid intake yields read-only snapshotted bytes and invalid intake leaves no snapshot/history/R2/Meta effects.

- [ ] **Step 7: Review and commit Task 2 only**

  Inspect: `git diff -- src/reel_release.py tests/test_reel_release.py`

  Commit boundary:

  ```bash
  git add src/reel_release.py tests/test_reel_release.py
  git commit -m "feat: verify Reel release packages"
  ```

---

### Task 3: Reel Publish Lifecycle Primitives

**Files:**

- Modify: `src/history_tracker.py`
- Create: `tests/test_reel_publication_lifecycle.py`
- Re-run unchanged: `tests/test_publication_history.py`
- Re-run unchanged: `tests/test_ambiguous_publish_lifecycle.py`

**Interfaces:**

- Consumes:
  - `ReelReleaseIdentity`, `ReelReservationRecord`, `ReelPublicationRecord`, `ReelStagingRecord`, and `ReelPublicationStatus` from Task 1.
  - `r2_media.TempReelUpload` from the existing staging API.
  - Existing history load/write/CAS primitives and exact quoted ETags.
- Produces in `src.history_tracker`:
  - `get_reel_reservation(publication_id: str) -> ReelReservationRecord`
  - `record_reel_staging(publication_id: str, release_identity: ReelReleaseIdentity, upload: r2_media.TempReelUpload, *, now: datetime | None = None) -> ReelReservationRecord`
  - `start_reel_publication_attempt(publication_id: str, release_identity: ReelReleaseIdentity, container_id: str, *, now: datetime | None = None) -> ReelReservationRecord`
  - `record_reel_publish_response(publication_id: str, release_identity: ReelReleaseIdentity, media_id: str) -> ReelReservationRecord`
  - `mark_reel_ambiguous(publication_id: str, release_identity: ReelReleaseIdentity, reason: str, *, now: datetime | None = None, reconciliation_result: str | None = None, reconciliation_evidence: str | None = None) -> ReelReservationRecord`
  - `finalize_reel_publication(publication_id: str, release_identity: ReelReleaseIdentity, media_id: str, *, permalink: str | None = None, now: datetime | None = None, reconciliation_result: str | None = None, reconciliation_evidence: str | None = None) -> ReelPublicationRecord`
  - `record_reel_permalink(publication_id: str, media_id: str, permalink: str) -> ReelPublicationRecord`

- [ ] **Step 1: Write failing staging and `PENDING -> PUBLISHING` tests**

  Test that `record_reel_staging()` accepts only the exact `TempReelUpload` owner, writes its Reel namespace key/public URL/staged timestamp while status stays `PENDING`, and is exact-idempotent. Test conflicts on release identity, upload owner/key/URL, non-PENDING state, or duplicate staging. Then call `start_reel_publication_attempt()` and assert one CAS atomically persists the exact container ID, `PUBLISHING`, and `publish_started_at`; require staging to exist first.

  Use an event list to prove the history write completes before a fake publisher appends `media_publish`:

  ```python
  def test_before_publish_boundary_is_durable_before_media_publish(monkeypatch):
      events = install_reel_history(monkeypatch, status="PENDING", staged=True)

      history_tracker.start_reel_publication_attempt(
          PUBLICATION_ID, RELEASE_IDENTITY, "container-1", now=NOW
      )
      events.append("media_publish")

      assert events == ["history_put:PUBLISHING:container-1", "media_publish"]
  ```

- [ ] **Step 2: Run lifecycle boundary tests and confirm the red state**

  Run: `pytest tests/test_reel_publication_lifecycle.py -q`

  Expected: failures because the Reel lifecycle mutation functions do not exist.

- [ ] **Step 3: Implement one reusable Reel CAS mutation helper and boundary primitives**

  Add a private `_conditional_reel_update(publication_id, release_identity, mutation)` that reloads and calls `_validated_reel_history()` on every attempt, selects exactly one reservation by UUID, verifies its immutable artwork/release identity, permits an exact idempotent result, rejects conflicts, preserves unrelated/unknown keys, and never retries the same stale ETag. Use it for staging, `PUBLISHING`, receipt, ambiguity, and permalink operations.

  `start_reel_publication_attempt()` must permit only `PENDING -> PUBLISHING` or the exact existing `PUBLISHING/container_id` replay. It must never return to `PENDING` and must not write feed rows.

- [ ] **Step 4: Write failing receipt/finalization tests**

  Cover durable `publish_response_media_id`, conflicting media IDs, finalization from `PUBLISHING` and `AMBIGUOUS` only, exact idempotent replay, release/artwork mismatch, cross-feed/Reel publication/media-ID conflict, optional permalink, and later permalink enrichment. Snapshot all feed keys before finalization and assert exact equality afterward:

  ```python
  feed_before = {
      key: copy.deepcopy(history[key])
      for key in ("posted_artworks", "publications", "grid_publication_count", "active_color_tone")
  }
  publication = history_tracker.finalize_reel_publication(
      PUBLICATION_ID, RELEASE_IDENTITY, "media-reel-1", now=NOW
  )
  assert history["reel_publication_count"] == 1
  assert len(history["reel_publications"]) == 1
  assert {key: history[key] for key in feed_before} == feed_before
  ```

  Assert exact idempotent finalization does not increment the counter again. Assert a permalink failure cannot undo an already finalized Reel.

- [ ] **Step 5: Run finalization tests and confirm the red state**

  Run: `pytest tests/test_reel_publication_lifecycle.py -q`

  Expected: boundary tests pass after Step 3; receipt/finalization tests fail because those mutations are incomplete.

- [ ] **Step 6: Implement durable receipt and atomic idempotent finalization**

  `record_reel_publish_response()` accepts `PUBLISHING`, `AMBIGUOUS`, or the exact `PUBLISHED` replay and persists the media ID before finalization. `finalize_reel_publication()` must, in one conditional write:

  - verify current reservation, artwork, release identity, status, and any durable receipt;
  - reject any publication/media ID used by feed or a different Reel;
  - set the reservation to `PUBLISHED`, with `media_id`, `publish_response_media_id`, and `posted_at`;
  - append exactly one matching immutable `reel_publications` row;
  - set `reel_publication_count` to the previous validated count plus one;
  - leave all feed fields and grid tone byte-for-byte unchanged.

  If the exact success row already exists, return it without writing or incrementing. Any partial or conflicting success state raises `CorruptedHistoryError`. `record_reel_permalink()` may only add the same valid HTTPS permalink to the reservation/publication pair and is independently idempotent.

- [ ] **Step 7: Run focused lifecycle and feed-regression verification**

  Run: `pytest tests/test_reel_publication_lifecycle.py tests/test_publication_history.py tests/test_ambiguous_publish_lifecycle.py -q`

  Expected: all pass; feed lifecycle behavior, including its existing parsed-4xx semantics, remains unchanged.

- [ ] **Step 8: Review and commit Task 3 only**

  Inspect: `git diff -- src/history_tracker.py tests/test_reel_publication_lifecycle.py`

  Commit boundary:

  ```bash
  git add src/history_tracker.py tests/test_reel_publication_lifecycle.py
  git commit -m "feat: add Reel publication lifecycle"
  ```

---

### Task 4: Reel Reconciliation and Cleanup

**Files:**

- Modify: `src/history_tracker.py`
- Create: `src/reel_reconciliation.py`
- Create: `tests/test_reel_reconciliation.py`
- Re-run unchanged: `tests/test_r2_reel_media.py`
- Re-run unchanged: `tests/test_r2_media_cleanup.py`
- Re-run unchanged: `tests/test_publication_reconciliation.py`

**Interfaces:**

- Consumes:
  - Task 3 Reel lookup, ambiguity, finalization, and permalink functions.
  - `instagram_poster.get_container_status(container_id: str, access_token: str) -> str`
  - `instagram_poster.get_instagram_media_id(media_id: str, access_token: str) -> str`
  - `instagram_poster.get_instagram_permalink(media_id: str, access_token: str) -> str | None`
  - `r2_media.cleanup_publication_reels(publication_id: str, *, reason: str) -> MediaCleanupSummary`
- Produces in `src.history_tracker`:
  - `list_unresolved_reel_reservations(*, limit: int, now: datetime | None = None, max_age: timedelta | None = None, publication_id: str | None = None) -> list[ReelReservationRecord]`
  - `expire_reel_before_media_publish(publication_id: str, release_identity: ReelReleaseIdentity, *, reason: str, expected_status: ReelPublicationStatus, expected_container_id: str | None = None, now: datetime | None = None) -> ReelReservationRecord`
  - `record_reel_reconciliation_evidence(publication_id: str, release_identity: ReelReleaseIdentity, *, result: str, evidence: str, now: datetime | None = None) -> ReelReservationRecord`
  - `list_reel_staging_cleanup_publication_ids(*, limit: int) -> list[str]`
  - `acknowledge_reel_staging_cleanup(publication_id: str) -> bool`
- Produces in `src.reel_reconciliation`:
  - `ReelReconciliationOutcome(str, Enum)` with `CONFIRMED_PUBLISHED`, `CONFIRMED_NOT_PUBLISHED`, `STILL_AMBIGUOUS`, and `RECONCILIATION_ERROR`.
  - `ReelReconciliationResult` and `ReelReconciliationSummary` frozen dataclasses analogous to the existing feed result types but Reel-specific.
  - `reconcile_reel_publications(*, access_token: str, limit: int = 20, max_age: timedelta | None = timedelta(days=30), now: datetime | None = None) -> ReelReconciliationSummary`
  - `recover_reel_media_id(*, publication_id: str, media_id: str, access_token: str, now: datetime | None = None) -> ReelReconciliationResult`

- [ ] **Step 1: Write failing unresolved-state and conservative-reconciliation tests**

  Cover bounded newest-first selection, stale `PENDING` expiry, the existing 50-minute publishing grace, receipt-driven finalization, missing container, every container status (`FINISHED`, `IN_PROGRESS`, `ERROR`, `EXPIRED`, `PUBLISHED`, and unknown), lookup exceptions, and malformed identity. Assert that every case without a verified media ID remains or becomes `AMBIGUOUS`, retains global protection, records `last_reconciled_at`, increments `reconciliation_attempt_count`, and stores result/evidence. Assert reconciliation never calls artwork selection, staging, container creation, `post_to_instagram_graph_api`, or `_publish_container`.

  ```python
  @pytest.mark.parametrize("container_status", ["FINISHED", "IN_PROGRESS", "ERROR", "EXPIRED", "PUBLISHED", "UNKNOWN"])
  def test_container_status_without_media_identity_stays_ambiguous(monkeypatch, container_status):
      install_unresolved_reel(monkeypatch, status="PUBLISHING", container_id="container-1")
      monkeypatch.setattr(instagram_poster, "get_container_status", lambda *args: container_status)
      publish = Mock(side_effect=AssertionError("reconciliation must not publish"))
      monkeypatch.setattr(instagram_poster, "_publish_container", publish)

      reel_reconciliation.reconcile_reel_publications(access_token="token", now=AFTER_GRACE)

      assert current_reel_status() == "AMBIGUOUS"
      publish.assert_not_called()
  ```

- [ ] **Step 2: Run reconciliation tests and confirm the red state**

  Run: `pytest tests/test_reel_reconciliation.py -q`

  Expected: import/collection failure because `src.reel_reconciliation` and Reel reconciliation history operations do not exist.

- [ ] **Step 3: Implement bounded reconciliation and safe operator recovery**

  Mirror the existing feed module's bounded scan/result accounting, but do not reuse its definitive `ERROR`/`EXPIRED` transition. For Reels:

  - stale `PENDING` is authoritatively pre-Meta and expires;
  - fresh `PUBLISHING` is skipped during the grace window;
  - a durable `publish_response_media_id` finalizes idempotently;
  - all container-only evidence remains/turns `AMBIGUOUS`;
  - `AMBIGUOUS` never expires by age and is never returned to a reservation candidate.

  `recover_reel_media_id()` must select exactly one unresolved `PUBLISHING`/`AMBIGUOUS` UUID, require its durable container, require `get_container_status(...) == "PUBLISHED"`, require `get_instagram_media_id(media_id, token) == media_id`, then finalize exactly once. Fetch permalink best-effort after success; a permalink failure leaves `PUBLISHED` intact.

- [ ] **Step 4: Write failing Reel-only cleanup queue tests**

  Test that stale `PENDING`, staging failure, container creation/processing failure, and proven `InstagramPrePublishBoundaryError` paths can atomically set `EXPIRED` plus one unique `reel_staging_cleanup_queue` entry. A `PUBLISHING -> EXPIRED` call must require the exact durable container ID as direct local evidence that the publisher stopped before `media_publish`. Assert `AMBIGUOUS`/`PUBLISHED` never enter the queue.

  For queue processing, inject both Reel and image-looking keys and assert only `cleanup_publication_reels()` is called, only `reels/publications/<publication_id>/` is listed, complete cleanup acknowledges only the Reel queue, and incomplete cleanup leaves it intact. Assert active/PUBLISHED Reel state overrides a stale queue entry.

- [ ] **Step 5: Run cleanup tests and confirm the red state**

  Run: `pytest tests/test_reel_reconciliation.py tests/test_r2_reel_media.py tests/test_r2_media_cleanup.py -q`

  Expected: reconciliation tests fail on absent Reel queue operations; existing R2 Reel namespace tests remain green.

- [ ] **Step 6: Implement atomic expiry/queue and bounded Reel cleanup**

  `expire_reel_before_media_publish()` must set `EXPIRED`, `expired_at`, and `expiration_reason` and append the unique queue row in the same CAS. It accepts only `PENDING`, or `PUBLISHING` with an exact `expected_container_id`; reject `AMBIGUOUS` and `PUBLISHED`. Queue selection validates every entry and skips any publication with a non-`EXPIRED` reservation or success row. Queue acknowledgement uses its own bounded CAS and never reads/writes `staging_media_cleanup_queue`.

  At the end of `reconcile_reel_publications()`, process at most `limit` eligible entries with `cleanup_publication_reels()`. Acknowledge only `summary.complete is True`; keep failed entries for retry. Do not call `cleanup_publication_media()` anywhere in the Reel module.

- [ ] **Step 7: Run focused reconciliation and namespace verification**

  Run: `pytest tests/test_reel_reconciliation.py tests/test_r2_reel_media.py tests/test_r2_media_cleanup.py tests/test_publication_reconciliation.py -q`

  Expected: all pass; feed reconciliation behavior and feed cleanup queue remain unchanged.

- [ ] **Step 8: Review and commit Task 4 only**

  Inspect: `git diff -- src/history_tracker.py src/reel_reconciliation.py tests/test_reel_reconciliation.py`

  Commit boundary:

  ```bash
  git add src/history_tracker.py src/reel_reconciliation.py tests/test_reel_reconciliation.py
  git commit -m "feat: reconcile Reel publication state"
  ```

---

### Task 5: Manual One-Release Reel Orchestrator

**Files:**

- Create: `src/reel_publication.py`
- Create: `scripts/publish_reel.py`
- Create: `tests/test_reel_publication.py`
- Re-run unchanged: `tests/test_instagram_poster.py`
- Re-run unchanged: `tests/test_production_workflow.py`
- Re-run unchanged: `tests/test_ambiguous_publish_lifecycle.py`

**Interfaces:**

- Consumes:
  - `reel_release.verified_reel_release_snapshot(...)`
  - `history_tracker.reserve_reel(...)`
  - `history_tracker.record_reel_staging(...)`
  - `history_tracker.start_reel_publication_attempt(...)`
  - `history_tracker.record_reel_publish_response(...)`
  - `history_tracker.finalize_reel_publication(...)`
  - `history_tracker.mark_reel_ambiguous(...)`
  - `history_tracker.expire_reel_before_media_publish(...)`
  - `r2_media.stage_reel_mp4(snapshot_path: str, publication_id: str) -> TempReelUpload`
  - `r2_media.cleanup_temp_reel_upload(upload: TempReelUpload, *, reason: str) -> bool`
  - `r2_media.cleanup_publication_reels(publication_id: str, *, reason: str) -> MediaCleanupSummary`
  - `instagram_poster.post_to_instagram_graph_api(...) -> str`
  - `instagram_poster.get_instagram_permalink(media_id: str, access_token: str) -> str | None`
- Produces in `src.reel_publication`:
  - `ReelPublicationPersistenceError(RuntimeError)` for a Meta success whose receipt/finalization cannot be durably completed.
  - `publish_verified_reel(*, release: str | Path, reels_repository: str | Path, account_id: str, access_token: str) -> ReelPublicationRecord`
- Produces in `scripts/publish_reel.py`:
  - `_default_reels_root() -> Path`, using nonblank `ARTFOLIO_REELS_ROOT` or the existing sibling-project default used by `scripts/collect_insights.py`.
  - `main(argv: Sequence[str] | None = None) -> int` with one positional release ID/directory and optional `--artfolio-reels-root`; no batch/schedule option.

- [ ] **Step 1: Write the failing happy-path orchestration test**

  Use a real temporary snapshot fixture and mocks for history, R2, and Instagram. Record call order and exact arguments:

  ```python
  assert events == [
      "verify_and_snapshot",
      "reserve_reel",
      "stage_reel_mp4:snapshot/reel.mp4",
      "record_reel_staging",
      "history_put:PUBLISHING:container-1",
      "media_publish",
      "record_reel_publish_response:media-1",
      "get_permalink:media-1",
      "finalize_reel_publication:media-1",
  ]
  assert publisher_kwargs == {
      "media_url": "https://media.example/reels/publications/publication-id/reel.mp4",
      "caption": verified.caption,
      "account_id": "account",
      "access_token": "token",
      "media_type": "REELS",
      "before_publish": ANY,
  }
  ```

  The fake publisher must invoke `before_publish("container-1", ())` before recording its one publish event. Assert staging receives only the snapshot path, never the mutable release path.

- [ ] **Step 2: Run the orchestration test and confirm the red state**

  Run: `pytest tests/test_reel_publication.py -q`

  Expected: import/collection failure because `src.reel_publication` does not exist.

- [ ] **Step 3: Implement the minimal happy-path orchestrator**

  Implement this exact ordering:

  1. enter `verified_reel_release_snapshot()`;
  2. reserve its normalized artwork with its exact `ReelReleaseIdentity`;
  3. call `stage_reel_mp4(str(verified.video_path), publication_id)`;
  4. durably `record_reel_staging()` before any Meta container call;
  5. call `post_to_instagram_graph_api()` with the staged URL, verified caption, credentials, `media_type="REELS"`, and a callback that calls `start_reel_publication_attempt()`;
  6. durably record the returned media ID;
  7. fetch permalink best-effort;
  8. idempotently finalize and return the Reel publication.

  Validate credentials through the existing publisher boundary; do not log tokens, local package contents, or caption text.

- [ ] **Step 4: Write failing failure-matrix tests**

  Cover every spec row and assert final durable state plus forbidden calls:

  - intake invalid: no reservation, R2, or Meta;
  - reservation collision/CAS exhaustion: no staging or Meta;
  - staging/health failure: `EXPIRED`, queued Reel cleanup, no Meta;
  - staging-handle persistence failure: exact-handle cleanup attempt, no Meta; if history is reachable, durable expiry/queue precedes prefix cleanup;
  - container creation/processing failure before callback: `EXPIRED`, durable queue, Reel-prefix cleanup, no `media_publish`;
  - callback persistence failure: publisher raises `InstagramPrePublishBoundaryError` and media-publish mock has zero calls; re-read state and expire only when PENDING or the exact container-backed PUBLISHING state proves pre-Meta failure;
  - every exception after the callback begins, including parsed 4xx, timeout, reset, malformed response, missing ID, 5xx, `InstagramPublishAmbiguousError`, and unexpected exception: best-effort `AMBIGUOUS`, never `EXPIRED`, never a second publisher call;
  - returned media ID with receipt failure: raise `ReelPublicationPersistenceError`, preserve PUBLISHING/AMBIGUOUS protection, never republish;
  - receipt succeeds but finalization fails: preserve receipt, best-effort mark AMBIGUOUS, raise `ReelPublicationPersistenceError`, never republish;
  - permalink lookup/persistence failure: return `PUBLISHED` success and do not change its counter again.

  Add a regression proving the Reel parsed-4xx mapping does not alter the existing feed orchestrator's definitive-error behavior.

- [ ] **Step 5: Run failure tests and confirm the red state**

  Run: `pytest tests/test_reel_publication.py -q`

  Expected: happy path passes after Step 3; failure-matrix tests fail until Reel-specific cleanup/quarantine mapping is implemented.

- [ ] **Step 6: Implement Reel-specific failure handling**

  Track three local facts: whether a reservation exists, whether the current invocation owns a `TempReelUpload`, and whether `before_publish` completed durably. Apply pre-Meta expiry only when the existing publisher guarantees `media_publish` was not called. After the durable callback, classify every unverified result as ambiguous regardless of feed exception subtype. Cleanup ordering must be durable expiry/queue first, then exact-handle or Reel-prefix cleanup; never clean active, ambiguous, or published media.

  If receipt/finalization persistence fails after Meta returns an ID, raise `ReelPublicationPersistenceError` with the publication ID but not credentials or caption. Do not call the publisher again inside this function or any caller.

- [ ] **Step 7: Write and implement the manual CLI tests**

  Test `scripts.publish_reel.main()` with a monkeypatched `publish_verified_reel()`. Require a single release argument, resolve `ARTFOLIO_REELS_ROOT` consistently with the existing collector helper, read `INSTAGRAM_ACCOUNT_ID`/`INSTAGRAM_ACCESS_TOKEN` from the environment, return zero only on durable success, and return non-zero with a sanitized log on failure. Assert there are no batch count, schedule, interval, workflow, or automatic-discovery options.

  Do not create a dry-run path that weakens intake validation, and do not invoke the real orchestrator in a test. All R2, Meta, verifier, and subprocess boundaries remain mocked/networkless.

- [ ] **Step 8: Run focused orchestrator verification**

  Run: `pytest tests/test_reel_publication.py tests/test_instagram_poster.py tests/test_production_workflow.py tests/test_ambiguous_publish_lifecycle.py -q`

  Expected: all pass; the publisher receives `media_type="REELS"`, `media_publish` is called at most once, and feed production behavior is unchanged.

- [ ] **Step 9: Run complete repository verification without live publication**

  Run:

  ```bash
  pytest -q
  python3 -m compileall src scripts tests
  ruff check src/models.py src/history_tracker.py src/reel_release.py src/reel_reconciliation.py src/reel_publication.py scripts/publish_reel.py tests/test_publication_history.py tests/test_reel_history.py tests/test_reel_release.py tests/test_reel_publication_lifecycle.py tests/test_reel_reconciliation.py tests/test_reel_publication.py
  git diff --check
  ```

  Expected: the full existing suite and all new networkless tests pass; compileall, focused Ruff, and diff checks pass. Do not run a live smoke. A live one-release publish requires a separate explicit authorization after this implementation is reviewed.

- [ ] **Step 10: Review scope and commit Task 5 only**

  Inspect `git status --short` and `git diff --stat`. Confirm there are no changes to `main.py`, `src/r2_media.py`, `src/instagram_poster.py`, `src/publication_reconciliation.py`, workflow/scheduler files, dependency locks, or Git tags.

  Commit boundary:

  ```bash
  git add src/reel_publication.py scripts/publish_reel.py tests/test_reel_publication.py
  git commit -m "feat: add manual Reel publication command"
  ```

## Plan Self-Review Against the Approved Spec

- **Schema and compatibility:** Task 1 covers all four additive keys, absent-field legacy behavior, strict count equality, lifecycle-shaped records, release identity, uniqueness, cross-record consistency, global canonical deduplication, both reservation directions, stale replacement, unknown-key preservation, and exact quoted-ETag CAS. Feed fields and validation are regression-tested rather than redesigned.
- **Release trust boundary:** Task 2 covers deep verifier success and evidence, exact package/schema/file/hash validation, containment and symlink defenses, identity parity, immutable private snapshot bytes, strict subprocess parsing, and zero history/R2/Meta calls on invalid intake.
- **Publish lifecycle:** Task 3 covers staged-handle durability, `PENDING -> PUBLISHING`, the durable callback boundary, media-ID receipt, monotonic/idempotent success, optional permalink enrichment, cross-format identity uniqueness, and Reel-only counter mutation.
- **Reconciliation and cleanup:** Task 4 covers stale pre-Meta expiry, the publishing grace window, receipt recovery, permanent ambiguity without verified media identity, exact operator media-ID verification, Reel-only queueing/acknowledgement, bounded cleanup, and strict Reel namespace use.
- **Manual orchestration:** Task 5 wires verified intake, global reservation, existing Reel staging, existing publisher with `media_type="REELS"`, durable lifecycle/failure handling, and a single explicit CLI. It includes no scheduler, 4+4 automation, automatic selection, automatic repurposing, or live test.
- **Placeholder scan:** The plan contains no deferred implementation markers, unnamed handlers, or undefined task dependencies; every produced public interface has one exact name and signature.
- **Type/name consistency:** `ReelReleaseIdentity`, `ReelReservationRecord`, `ReelPublicationRecord`, `VerifiedReelRelease`, lifecycle function names, reconciliation function names, and cleanup function names are introduced once and consumed with the same spelling in later tasks.
- **Feed isolation:** Existing feed modules and image-staging functions are either unchanged or covered by focused regressions. Reel exception mapping is isolated in `src/reel_publication.py`; feed 4xx behavior, counters, grid tone, cleanup queue, and image namespace are not changed.
- **Stable checkpoint:** No task contains a tag command; `stable-instagram-art-bot-v1` remains untouched.

No architectural blocker is known. The implementation assumes the current external verifier JSON contract from `artfolio-reels` continues to return `valid`, `directory`, `reelId`, `errors`, and deep `media` evidence; Task 2 fails closed if that contract changes.
