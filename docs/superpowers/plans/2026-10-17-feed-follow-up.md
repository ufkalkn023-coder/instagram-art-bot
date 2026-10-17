# Feed follow-up implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline, in the requested order.

**Goal:** Restore timely Insights collection, deliver the existing crop editor, and add a refreshable read-only dashboard with a visual queue calendar and story comparisons.

**Architecture:** Preserve the existing collector and revisioned editor. Extend the pure dashboard renderer and reuse its guarded input loading through a loopback-only server. No browser credentials, production authorization changes, new paid services or dependencies.

**Tech stack:** Python 3.10, standard-library HTTP server, Pillow, existing Insights/R2 clients, pytest, vanilla browser JavaScript.

**Spec:** User-approved three-step scope in this chat (2026-10-17); details below.

## Constraints

- Complete the three deliveries sequentially, as separate focused PRs.
- Preserve the five existing uncommitted crop-editor files and unrelated `.mimosa`.
- Keep Gemini disabled. Do not publish media or mutate queue, receipts or safety state.
- Collector recovery may update its existing analytics objects and local health log only.
- Never widen credential roles, expose secrets or invent missed historical measurements.
- Dashboard network mode is explicit, expected-SHA checked and GET/LIST-only.
- Keep standalone/offline HTML and existing CLI arguments backward compatible.
- Current production remains on approved main until a separately authorized integration.

## Review focus

- A daily collection gap crosses early windows; replay actual publication timing.
- Completed Insights slots must not be fetched repeatedly on hourly runs.
- Cancelled/invalid crop selection must preserve valid saved revisions.
- Refresh failures must preserve the last successful dashboard and give visible feedback.
- Dashboard HTTP access must reject foreign hosts/origins and unsafe filesystem routes.

## 1. Insights reliability

Files: `.github/workflows/instagram_insights.yml`, `tests/test_insights_workflow.py`, `tests/test_insights_collector.py`, relevant README section.

- [x] Identify actual cloud run times and local LaunchAgent failure.
- [x] Add a failing real-publication replay that exercises configured cadence.
- [ ] Run cloud collection hourly at minute43; retain existing slot idempotency and least-privilege secrets.
- [ ] Repair only the missing, verified non-secret local state-bucket setting; verify GET access and a successful normal collection.
- [ ] Run targeted tests, full locked Python3.10 suite, Ruff and diff checks; publish focused PR.

## 2. Existing visual crop editor

Files: existing changes in `src/story_review.py`, `src/story_editor.py`, `tests/test_story_review.py`, `tests/fixtures/story_editor_harness.cjs`, README.

- [ ] Review the existing patch against recorded requirements; do not rewrite it.
- [ ] Run focused and full tests and desktop/mobile browser QA on a copied demo project.
- [ ] Resolve only discovered correctness issues and publish a separate PR.

## 3. Refreshable dashboard, calendar and story comparisons

Files: `src/feed_dashboard.py`, a small dashboard server module and CLI, dashboard tests and README. Extract shared data loading only where both CLIs need it.

- [ ] Add renderer tests for source-backed calendar cards, safe thumbnails, actual story cohorts and insufficient-data states.
- [ ] Add a bounded loopback server with a manual refresh button; requests rebuild the snapshot from configured local files or explicit read-only R2 mode.
- [ ] Protect refresh with same-origin session validation; reject arbitrary assets, traversal, oversized bodies and foreign hosts. Keep last successful report after failure.
- [ ] Keep thumbnail sources local/bounded and source-backed; use placeholders if unavailable. Never expose private R2 or API credentials.
- [ ] Show actual queue order, format, expiry and provisional earliest dates under the48h rule; flag expiry/claim/quarantine and do not promise exact publication times.
- [ ] Show story narrative/cover/page-count cohorts at matched ages with sample counts, coverage and missing values; never count pages as independent observations or select an automatic winner.
- [ ] Verify browser refresh/error states and responsive layouts; run full tests and publish third PR.

## Evidence ledger

- Baseline main: `1c60f243e0514bc3af04f4830054ffd4d7146471`.
- Cloud collector daily03:00; actual Orchid captures at08:30 Oct14 then08:05 Oct15 missed the6h window.
- Local collector: hourly LaunchAgent loaded, last exit1; missing only non-secret state-bucket name. Existing token/key values retained.
- Cadence replay and workflow tests failed before the workflow change (two failures).
- Ruling: use feature branches in the existing checkout for the first small workflow change; selectively stage files/hunks so unrelated editor work remains intact. No stash/reset/cleanup.
- Ruling: deliver code as PRs as requested. Do not silently merge or transfer exact-SHA production approval.
