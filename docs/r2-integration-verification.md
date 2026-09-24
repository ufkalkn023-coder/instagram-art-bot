# Cloudflare R2 conditional-write verification

## Durable publication state

Production publication safety and documentary receipts use two fixed objects in a
private, durable R2 bucket, separate from the media bucket:

- `publication_safety_state.v2.json` contains permanent artwork protection,
  recovery quarantine, and strict active publication state.
- `publication_receipts.v2.json` contains immutable publication receipts.

`src/publication_state.py` reads both with `GetObject`, validates schema and
SHA-256 payload digests, and preserves the returned quoted ETag as an opaque
validator. Updates use `PutObject(IfMatch=<ETag>)`. A failed precondition is a
conflict; an uncertain response requires a fresh read before another mutation.
Bootstrap is a separate, explicit `PutObject(IfNoneMatch="*")` operation that
cannot replace an existing object. Production does not create missing state.
The production preflight also reads the state bucket lifecycle configuration
and rejects destructive rules or an unverifiable result.

Successful publication finalization adds artwork protection in the same safety
state CAS as the `PUBLISHED` transition. It then appends the documentary receipt
with a separate CAS. If that append fails, `receipt_sync_pending` stays in the
safety state and preflight blocks new publishing until an idempotent receipt
replay completes. Replay does not call Instagram.

Cloudflare documents conditional `PutObject` support, HTTP 412 for failed
conditions, strong consistency, and last-writer-wins behavior for unconditional
same-key writes:

- [S3 API compatibility](https://developers.cloudflare.com/r2/api/s3/api/)
- [R2 error codes](https://developers.cloudflare.com/r2/api/error-codes/)
- [Consistency model](https://developers.cloudflare.com/r2/reference/consistency/)

## Live suite isolation

The suite is disabled unless `ARTFOLIO_RUN_R2_INTEGRATION=1` is set explicitly.
It must use a dedicated **test** R2 bucket and test credentials. The test bucket
name must differ from both the production media and durable-state bucket names. Each run creates an
independent UUID namespace under `artfolio-integration-tests/`; all object
operations and cleanup are guarded to that namespace. The application-level
v2 test maps its fixed production key names into that namespace through a test
client wrapper. The suite never accesses the production state keys.

To run locally, configure the test account and bucket through
`CLOUDFLARE_R2_ACCOUNT_ID`, `CLOUDFLARE_R2_ACCESS_KEY_ID`,
`CLOUDFLARE_R2_SECRET_ACCESS_KEY`, and `CLOUDFLARE_R2_BUCKET_NAME`, then set
`CLOUDFLARE_PRODUCTION_R2_BUCKET_NAME` and
`CLOUDFLARE_PRODUCTION_STATE_R2_BUCKET_NAME` for separation checks. Run offline
checks first. Only then opt in:

```bash
ARTFOLIO_RUN_R2_INTEGRATION=1 python3 -m pytest -q -s tests/integration/test_r2_conditional_writes.py
```

The manual GitHub workflow `R2 Integration Verification` requires the
`RUN_R2_INTEGRATION` confirmation input and dedicated
`CLOUDFLARE_R2_TEST_BUCKET_NAME`, `CLOUDFLARE_R2_TEST_ACCESS_KEY_ID`, and
`CLOUDFLARE_R2_TEST_SECRET_ACCESS_KEY` secrets. It reads the two production bucket
names only to reject accidental equality. The workflow has no schedule.

The live suite checks raw PUT/GET/HEAD, conditional creation, matching and stale
ETag CAS, missing-object classification, complete repeated JSON writes, isolated
object deletion semantics, and application-level v2 safety CAS. It does not call
Instagram. Teardown deletes only exact keys in the run's UUID namespace and
verifies that they are absent. This suite has **not** been run during local
recovery implementation; running it is an operator decision after provisioning
an isolated test bucket.
