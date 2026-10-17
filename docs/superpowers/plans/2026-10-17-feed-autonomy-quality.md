# Sequential Feed autonomy and quality implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline; complete tasks in order.

**Goal:** Implement early inventory, weekly reports, queue-aware diversity and one-factor presentation experiments without notifications/mobile work.
**Architecture:** Extend existing queue CAS, analytics, diversity and typed experiment flows. Use one isolated branch and focused sequential commits. Publication safety/rights/cooldown remain authoritative.
**Tech Stack:** Python3.10, existing Pillow/boto3/pytest, GitHub Actions; no new dependency or paidAI.
**Spec:** docs/superpowers/specs/2026-10-17-feed-autonomy-quality.md

## Global constraints

- User order2→3→4→5; no notifications or mobile work.
- Preserve original canonical five editor edits and.mimosa; currentmain a0e353aea67cee18103896203af89c7b31be928f.
- Gemini false; no live AI. No forced Feed publication or weakened approval, receipt, CAS, history, rights or48h gate.
- Reuse existing sources; missing metrics and metadata remain unavailable.

## Review focus

- Concurrent claim/consumption during early acquisition must reject stale installation without altering active ownership.
- New packages must exclude retained source IDs and preserve alternating order and all fresh READY bytes.
- Weekly boundaries, duplicate parent identities, missing metrics and recent-post maturity must not manufacture trends.
- Planned inventory must influence variety without becoming fabricated successful history or lowering quality minima.
- Caption experiments hold cover fixed; cover experiments hold caption hook fixed; sealed stories remain unchanged.

## Task1 — Early stock

**Files:** src/r2_feed_queue.py, scripts/prepare_feed_queue.py, .github/workflows/prepare_feed_queue.yml, tests/test_feed_queue_stock.py, README.md.
**Interfaces:** R2PreparedFeedQueue.stock_status(now=None)→dict returns refill_needed/reason/queue_etag/excluded_artwork_ids and retained format information. replenish(local,expected_etag,now=None) validates and appends under manifest CAS. Existing install/refill APIs remain compatible. CLI adds --early only with guarded refill/check-refill modes.

- [ ] RED: twoREADY plus oneCONSUMED requests low-stock replenishment; threeREADY does not; CLAIMED/unverified quarantine block; invalid dates fail.
- [ ] RED: replenish preserves both fresh entries byte-for-byte, archives previous document and appends3new (total5); excludes overlapping sources; rejects wrong format/ETag/racing claim/uncertain write/overflow before active-manifest mutation.
- [ ] Implement shared local asset validation and bounded early install while leaving ordinary install semantics unchanged.
- [ ] CLI --early uses exact-SHA schedule, matching first retained format and tail alternation; excludes active IDs during acquisition; exact manifest recheck before upload/CAS. Workflow supplies --early on scheduled checks and refill only.
- [ ] Target queue tests, workflow tests, Ruff/diff, fullsuite; commit focused early-stock change with docs.

## Task2 — Weekly report

**Files:** new src/feed_weekly_report.py, scripts/report_feed_weekly.py, .github/workflows/feed_weekly_report.yml, tests/test_feed_weekly_report.py; shared scripts/report_feed_dashboard.py loader keyword and README.
**Interfaces:** build_weekly_report(schedule,queue,history,snapshots,*,now:datetime)→dict validates all publication identities before selecting interval [now−7days,now); retains operational health from all stored history and cohort evidence only for interval publications. render_weekly_markdown(report)→str discloses unavailable metrics, counts and minimum-N5. CLI reuses existing bounded dashboard input parser/loaders with include_thumbnails=False, creates --output Markdown plus same-stem JSON exclusively, without overwriting inputs/existing output.
**Schedule:** Monday06:23UTC, main-only, contents:read, existingR2 credentials only, no Instagram/Gemini secrets. Generate outputs in RUNNER_TEMP, append Markdown to Actions summary and upload report artifacts for30days. This is a report, not a new notification channel.

- [ ] RED interval boundary and offset-aware timestamps, duplicate publication/media conflict globally before interval filtering, one parent regardless of pages, missing observations remainNone, matched target-age cohorts and N5 caveat.
- [ ] RED CLI offline input preservation/output collision and guarded R2 reads without thumbnails; workflow main/schedule/permission/secret checks.
- [ ] Implement pure report, shared loader option, CLI and weekly workflow; no analytics/state writes.
- [ ] RED time-window/parent/coverage/equal-age and read-only CLI/workflow tests, implement scheduled artifact/summary report, validate and commit.

## Task3 — Queue-aware variety

- [ ] Map existing history-based diversity and preparation boundaries; detail minimal interface before implementation.
- [ ] RED queued context changes later selection while unknown metadata remains neutral; implement shared context with preserved minima, validate and commit.

## Task4 — Controlled presentation experiments

- [ ] Map actual cover rendering, experiment schema and report consumers; detail compatible one-factor contracts before implementation.
- [ ] RED deterministic mutually-exclusive assignment/real-render treatment/typed evidence/equal-age report and stale sealed-content protections; implement, validate and commit.

## Integration

- [ ] Fresh independent whole-diff review; reproduce/fix substantive findings with regression tests.
- [ ] Full locked Python3.10 suite, Ruff/compile/diff and public-source secret/path review.
- [ ] Focused PR and successful CI; document what is implemented vs activated and any remaining production step. Preserve existing exact-main operation during development.
