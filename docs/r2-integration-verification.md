# Cloudflare R2 concurrency verification

## Production architecture

`src/history_tracker.py` creates a boto3 S3 client for
`https://<CLOUDFLARE_R2_ACCOUNT_ID>.r2.cloudflarestorage.com`, region `auto`,
using the existing access key, secret key, and bucket environment variables.
The client has a 10 second connect timeout, a 30 second read timeout, and one
total SDK attempt.

All selection history, reservation state, publication lifecycle state, publish
receipts, and reconciliation results are rows in the single
`posted_history.json` object. Reconciliation does not have a separate R2
object. History is read with `GetObject`; no production history path uses copy,
rename, multipart upload, or delete.

`src/image_processor.py` creates an equivalent bounded client and uploads media
under `images/<timestamp>_<uuid>.<suffix>` with boto3 `upload_file`. The pinned
boto3 transfer default switches to multipart at 8 MiB. Media ETags are not used
for concurrency or checksum decisions. After upload, a bounded public HTTP
`HEAD` validates length and media type. Transient upload failures receive at
most three application attempts; permanent failures stop immediately. There
is currently no production R2 media deletion, including when the public health
check ultimately fails.

## History compare-and-swap contract

The exact production history write contract is:

1. `GetObject` reads the complete JSON document and its quoted, opaque `ETag`.
2. If the object was absent, `PutObject(IfNoneMatch="*")` creates it only while
   it remains absent.
3. If the object existed, `PutObject(IfMatch=<exact quoted ETag>)` replaces it
   only while that validator still matches.
4. R2 `PreconditionFailed` / HTTP 412 becomes `ConcurrentWriteError` and is not
   classified as a transient network failure.
5. Publication lifecycle mutations reload and re-evaluate after a conflict,
   with at most three complete load/mutate/CAS attempts. They never retry the
   same stale ETag blindly.
6. Initial reservation and stale-reservation recovery use the same CAS write
   but intentionally abort on a conflict instead of merging speculatively.
7. A missing or malformed ETag on an existing object fails closed. The ETag is
   never interpreted as an MD5 digest and its quotes are not removed.

Consequently, exactly one writer can win from a shared ETag. An unconditional
overwrite would otherwise be last-writer-wins on R2.

## Cloudflare contract checked

Cloudflare's current documentation lists `If-Match` and `If-None-Match` as
implemented conditional operations for S3 `PutObject`, and documents failed
conditions as `PreconditionFailed` with HTTP 412. Its S3 API is strongly
consistent for write/read, overwrite/read, deletion, and object listing. The
same documentation notes that unconditional writes to the same key are
last-writer-wins.

- [S3 API compatibility](https://developers.cloudflare.com/r2/api/s3/api/)
- [R2 error codes](https://developers.cloudflare.com/r2/api/error-codes/)
- [Consistency model](https://developers.cloudflare.com/r2/reference/consistency/)
- [Conditional header example](https://developers.cloudflare.com/r2/examples/aws/custom-header/)
- [Upload and multipart ETags](https://developers.cloudflare.com/r2/objects/upload-objects/)

## Live suite safety and execution

The live suite is skipped unless `ARTFOLIO_RUN_R2_INTEGRATION` is exactly `1`.
Credentials alone never enable it. Every run generates an independent UUID
namespace:

```text
artfolio-integration-tests/<run_uuid>/
```

Every key and cleanup operation passes a fail-closed namespace guard. Fixture
teardown lists and deletes only exact keys beneath that run UUID, verifies the
listing is empty, and verifies every known key is absent. A teardown failure
prints only the safe prefix and integration keys for manual recovery.

Run normal offline tests first. Then, in an environment where the existing R2
variables are configured, opt in explicitly:

```bash
ARTFOLIO_RUN_R2_INTEGRATION=1 python3 -m pytest -q -s tests/integration/test_r2_conditional_writes.py
```

After the workflow is committed and pushed, it can instead be run against the
repository's configured secrets from **Actions → R2 Integration Verification →
Run workflow**. Enter `RUN_R2_INTEGRATION` in the
`confirm_r2_integration` field. This dedicated workflow has only a
`workflow_dispatch` trigger, runs the offline safety preflight before exposing
the live opt-in, and never invokes production publication code.

The suite proves raw PUT/GET/HEAD behavior, real ETag shape, create-if-absent,
matching and stale `If-Match`, deterministic two-writer CAS, missing-object and
precondition exception classification, application-level lifecycle conflict
reload/re-evaluation, reconciliation-shaped state, repeated complete JSON
writes, and cleanup. It does not call Instagram or use production identifiers.

## Remaining limitation

History is a single object and therefore a same-key concurrency hotspot.
Cloudflare documents rate limiting for repeated writes to one key. History
operations have bounded SDK behavior but no application-level transient retry;
they fail safely, and publication does not cross its durable boundary after a
failed history write. The integration helper spaces repeated writes so a 429
does not obscure the conditional-write property being tested.
