# Feed autonomy and quality — approved sequential scope

User requested recommendations2,3,4,5, in that order. No external notifications and no mobile editing work. Intended outcome: keep the already-approved autonomous bot supplied, produce a weekly evidence-backed report, improve variety across future publications, and run controlled presentation experiments. Keep Gemini disabled, no paid AI, no weakened rights/duplicate/publication/48h/receipt/CAS gates.

## 1. Early inventory replenishment

Existing DEC-035 replaces fully terminal/expired batches only. This request explicitly extends that behavior: when at most two unexpired READY packages remain, prepare three new alternating packages excluding every source already in the active manifest. Preserve each fresh READY package byte-for-byte and append the new packages after them. Before replacing the manifest, archive its complete immutable snapshot; consumed/expired entries may leave the active manifest only with that archive retained. Maximum five active packages. Any CLAIMED or unverified-outcome quarantine blocks early replenishment; malformed evidence fails closed. Recheck exact-main schedule and exact manifest ETag after preparation; no blind CAS retries or publication/state/receipt writes. Original terminal-only refill behavior remains available. Newly prepared content keeps its normal14-day maximum TTL. Existing three-flag opt-in gates authorize the scheduled early mode.

## 2. Weekly report

Generate a weekly report automatically from authoritative stored history, Insights and operational status. Show publications in the report interval, measurement completeness, equal-target-age engagement, queue inventory and system blockers. Missing measurements are unavailable, never zero; story parents count once. Retain downloadable Markdown/JSON artifacts and a GitHub summary; no email/chat destination or new notification channel. Credentials remain server-side; report collection onlyGET/HEAD/LIST. No live AI or Instagram calls for reporting.

## 3. Publication variety

Build on existing recent-history diversity rather than introducing a competing scoring model. Include already prepared future content in selection context so sequential package preparation does not independently repeat the same artist/museum/period/color. Preserve strict quality/relevance/rights minima and deterministic bounded acquisition. Missing metadata is unknown, not invented. Explain the selection context in metadata/diagnostics without fabricating successful publication history. Existing reviewed story packages are immutable.

## 4. Controlled cover/caption experiments

Reuse typed experiment assignments and matched-age reporting. Introduce a bounded cover treatment separately from the existing caption-hook test: exactly one factor changes per experiment, with factual body, rights/credits, source set and other presentation factors held fixed by the assignment. Apply only to newly generated eligible content, never rewrite sealed queued or approved story content. Deterministic assignment; no automatic winner or model promotion. Report per-arm sample counts and missing-data coverage, and mark insufficient evidence. Keep AI disabled. Activation must use explicit experiment flags and a successful final exact-main deployment.

## Delivery

Execute in order, each with failing behavior tests before implementation, targeted validation and a focused commit; run full integration checks and independent final review. Preserve canonical user edits. Deliver a reviewable PR; any production rollout preserves the exact-main approval boundary and verifies real operational status. Previously granted unattended-operation authorization does not permit bypassing safety gates or approving future unknown code.
