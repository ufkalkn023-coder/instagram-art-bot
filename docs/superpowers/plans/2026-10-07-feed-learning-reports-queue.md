# Feed Learning, Reports and Prepared Queue Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans and the existing test-first workflow. Implement the user-approved sequence inline; one whole-change review at the end.

**Goal:** Learn from both Feed formats, explain missing collection windows, compare equal-age results, and prepare a bounded local pilot queue of reusable Feed packages. Distributed production persistence remains a later integration step.

**Architecture:** Extend the canonical learning feature contract and build independent format models from exact publication/media matches. Reuse persisted Insights attempts and missed-slot markers for read-only reports. Extract the existing preparation paths into reusable package production before any reservation or publication boundary.

**Tech Stack:** Existing Python 3.10+, Pydantic, Pillow and storage utilities. No dependency additions.

**Spec:** User-approved outputs 1 → 2 → 3 → 4 in this chat; existing Feed improvements plan and DEC-019/031/032/033 safeguards.

## Global Constraints

- Preserve minimum 48-hour completion cooldown, successful-format alternation, exact-SHA approval, original-run identity, strict rights, reservation/CAS and ambiguous-result protections.
- The initial implementation request did not authorize Git or production changes. The subsequent direct user request “geliştirmeleri canlıya alalım” authorizes the focused commit/PR/CI/protected merge and matching hourly approval rollout below. No additional live post, token change or blind workflow retry is included.
- Preserve `.mimosa/` and keep Reel behavior unchanged.
- Missing metrics remain missing; zero outcomes with positive reach are valid. Match authoritative publication/media identities; avoid combining ages in format comparison.
- Prepared content is not a publication permit. Revalidate format, expiry, bytes, rights and current history before reservation; uncertainty cannot make a package reusable.

## Tasks

### Task 1: Single Feed learning

Files: `src/engagement_features.py`, `src/engagement_learning.py`, relevant serving/audit code and tests.
Interface: `EngagementModel.for_format(publication_format)` returns an independently fitted model or cold start; audit counts include both Feed types. Add authoritative `publication_format` context feature, separate from carousel editorial format.
- [x] Failing tests: single inclusion, cross-format isolation, cold start, partial metrics, identity mismatch and feature parity.
- [x] Implement format-scoped models, truthful Feed audit counts, and explicit serving model selection.
- [x] Targeted regressions and full suite pass before advancing.

### Task 2: Missing measurement report

Files: new read-only Feed analytics module/CLI and focused tests.
Interface: report per publication/media and target age, with complete/partial/unavailable/missed/pending/due states, reason and recoverability under existing collector policy.
- [x] Fail tests for distinct missed windows vs publications, invalid identities, duplicate markers, expiry and unavailable/partial data.
- [x] Reuse collector target windows and snapshot completion classifications without writes or new API requests.
- [x] Verify deterministic JSON/local CLI output and full suite.

### Task 3: Equal-age format comparison

Files: same report module/CLI and focused tests.
Interface: per-format, per-target-age metric counts, coverage and ratios; require sufficient comparable evidence and disclose absence rather than promote a winner.
- [x] Fail tests for independent metric denominators, missing vs zero, unavailable data, equal ages, duplicate attempts and small cohorts.
- [x] Implement read-only cohort summary and sample-aware comparison.
- [x] Verify output and full suite.

### Task 4: Prepared Feed queue — local pilot

Files: reusable preparation seam, new queue module/CLI and tests; existing reservation/publishing functions remain authoritative.
- [x] Write concrete queue contract after the preparation seam is mapped; ledger any scope ruling.
- [x] Fail tests for 3–5 bound, format alternation, unique artwork IDs, byte digest/expiry/stored rights revalidation, concurrency and uncertain consumption.
- [x] Reuse existing acquisition and render paths without publishing during preparation; integrate opt-in consumption before existing reservation.
- [x] Verify no preparation publication/history writes, authoritative single reservation behavior, exhaustion/stale fallback and full suite.
- [ ] Complete shared R2 storage/distributed ownership and GitHub Actions integration before claiming the production queue is complete.

## Rulings and Verification Ledger

- 2026-10-07: Work in a focused local development branch; no new worktree or commits needed. User-approved sequence is sufficient authorization for reversible implementation; deployment remains a separate operation.
- Existing temporal backtest must remain explicitly carousel-scoped until format-specific temporal evaluation is added; do not silently pool singles into historical calibration results.
- The ten-output list and current user request define the implementation brief. No new UX, infrastructure, notification channel or Reel activation is included.
- Queue contract: new local directory, atomic sealed manifest, 3–5 alternating packages, canonical unique artwork IDs, JPEG/digest and stored confirmed rights checks, bounded expiry, nonblocking process lock. READY → CLAIMED → CONSUMED only after confirmed history/receipt; uncertainty → QUARANTINED without automatic rearming. Corrupt storage fails closed. Empty/stale queue uses normal acquisition.
- Scope ruling: this local pilot requires Mac/Linux and a persistent shared filesystem. GitHub ephemeral runners cannot retain it across runs. Shared R2 persistence/distributed ownership and fresh museum rights queries are not implemented; original roadmap Stage 3 remains incomplete.
- Final review: two findings fixed with regression tests (late attempts displacing valid window measurements; doctor excluding usable singles). Follow-up review found no remaining actionable issue in the local pilot scope.
- Final local validation: **1478 passed, 10 skipped in 44.76s**; focused follow-ups **78 passed**; compileall and diff checks passed. External API/storage calls were mocked in publication tests; no new live publication, R2 mutation, commit/push/merge or deployment occurred. Python 3.10 CI awaits a separately authorized rollout; local validation used Python 3.13.

## Authorized Production Rollout

The direct user request “geliştirmeleri canlıya alalım” authorizes deployment of this concrete reviewed change. Queue consumption remains opt-in; the Feed workflow continues ordinary acquisition because no shared persistent queue is configured.

1. Recheck local full tests and focused diff; commit only owned changes, preserving `.mimosa/`.
2. Push the focused branch and create/attach a PR against main. Require Python 3.10 CI; merge normally through branch protection.
3. Verify exact merged main tree and post-merge CI, then fresh authoritative state/receipts/media guards and active Feed-run inventory.
4. Transfer existing continuous `hourly_utc17` approval once using fresh generation and exact new main SHA. Preserve completion/cooldown, permit ledger, protections, receipts and all publication guards; read back the result.
5. Verify deployed read-only learning/report paths and update UfukOS with source, CI and state evidence. Do not dispatch an additional publication or retry an old workflow.
