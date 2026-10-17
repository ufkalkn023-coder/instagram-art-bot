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

**Files:** new src/feed_variety.py and tests/test_feed_variety.py; src/art_fetcher.py, src/feed_editorial.py, scripts/prepare_feed_queue.py, main.py, Feed/queue workflows, README.
**Interfaces:** variety_enabled() reads explicit ARTFOLIO_FEED_VARIETY_ENABLED; context_from_inventory(packages,*,now) and context_from_content(content,group_id) produce selection-only metadata rows carrying inventory_group, never published timestamps/claims. variety_adjustment(candidate,context) returns bounded negativefloat: artist4,museum2,period1,color1 per matching group, max12, recent12groups; unknown metadata neutral. Published rows retain their real publication_id and aliases; planned rows remain separate context.
Single iter_single_post_candidates gains optional inventory_context; final measured rescore preserves adjustment in SelectionScoreBreakdown optionalfield. Carousel selection passes published+planned context under flag into _select_acquired_theme_artworks and applies after validation/blending; raw quality/relevance and all acceptance gates unchanged. FeedPairPlanner remembers every prepared result and seeds retainedREADY context; Story contents observed without any modification. Main _prepare passes optional context only for real preparation. Workflow flag remains explicit; activation recorded separately.

- [ ] RED identity normalization/unknown neutrality/12group bound and deterministic pressure across artist/museum/period/color.
- [ ] RED later planned package receives earlier source context, existing ready context participates, actual single ranking changes without extra downloads; carousel adjustment preservesquality/relevance.
- [ ] Implement context and pipeline wiring, verify fixtures/backward compatibility withflagoff, target/fulltests and commit.
- [ ] RED queued context changes later selection while unknown metadata remains neutral; implement shared context with preserved minima, validate and commit.

## Task4 — Controlled presentation experiments

**Files:** src/models.py, src/editorial_experiments.py, src/carousel_cover.py, src/feed_experiment_report.py, main.py, Feed/queue workflows; tests/test_cover_experiments.py and existingexperiment/history/render tests; README.
**Interfaces:** frozen ControlledCoverExperiment(idcover_treatment_v1,variablecover_variant,armeditorial|editorial_panel,theme_id,fixedcaption_hook_typevisual_detail), optional controlled_cover_experiment alongside unchangedcaption schema. Reservation/publication validators forbid simultaneous assignments, mismatched theme/deliveredhook/cover and assignments onStoryDelivery.
assign_presentation_experiment(theme,run_seed,caption_enabled,cover_enabled) deterministically selects one family when both flags aretrue, otherwise the enabled family. Eachfamily retains deterministictwo-arm assignment, never changes bothfactors. Existingcaption flag remainsbackward compatible. Newcoverflag explicit inworkflows; no activation beforeverifieddeployment.
Renderer create_carousel_editorial_cover gains defaulteditorial cover_variant arg; neweditorial_panel alters onlybottomcopy background (opaque panel versusexistinggradient). Source, crop, text, fonts, featuredpages and factualcaption remainunchanged acrosscoverarms. Coverexperiment forces groundedvisual_detail caption identically acrossarms; captionexperiment keepseditorial cover. Main recordsactualrenderedcover inplan/finalfeatures/persistedassignment. Report branchesbytypedfamily andgroupsbytheme,targetage,fixedcounterpart; winnerNone/autopromotefalse.

- [ ] RED typedroundtrip/mismatch/bothassignment/storyrejection, deterministicfamily/arm assignment.
- [ ] RED actualrenderer variants differatbottom andpreservecentralart/sourcebytes/1080×1350 publishability; unknownvariant rejectedbeforeoutput.
- [ ] RED preparationpassesactualvariant toRenderer andmetadata, fixedcaption acrosscoverarms; persistnewassignmentthroughreservation/finalization.
- [ ] RED cohortseparationbyvariable/theme/age/fixedfactor withmissingmetriccounts/minN5, noautopromote.
- [ ] Implement compatible schema, choice/render/report/wiring, verifyalltarget/fulltests, visualpreview andcommit.
- [ ] RED deterministic mutually-exclusive assignment/real-render treatment/typed evidence/equal-age report and stale sealed-content protections; implement, validate and commit.

## Integration

- [ ] Fresh independent whole-diff review; reproduce/fix substantive findings with regression tests.
- [ ] Full locked Python3.10 suite, Ruff/compile/diff and public-source secret/path review.
- [ ] Focused PR and successful CI; document what is implemented vs activated and any remaining production step. Preserve existing exact-main operation during development.
