"""Bootstrap CLI safety checks run without forensic files or cloud access."""

from types import SimpleNamespace

import pytest

from scripts import bootstrap_publication_recovery as bootstrap
from src import publication_state


def _candidate(monkeypatch):
    monkeypatch.setattr(
        bootstrap, "build_candidate",
        lambda _directory: ({"safety": True}, {"receipts": True}, {"protected_ids": 634}),
    )


def test_bootstrap_defaults_to_local_dry_run(monkeypatch):
    _candidate(monkeypatch)
    monkeypatch.setattr(
        bootstrap, "PublicationStateStore",
        lambda: pytest.fail("dry-run accessed R2"),
    )
    assert bootstrap.main(["--evidence-dir", "unused"]) == 0


@pytest.mark.parametrize(
    "flags, expected",
    [
        (["--write"], "affirmative confirmation"),
        (["--write", "--confirm-production-write", "I_AUTHORIZE_PRODUCTION_RECOVERY_BOOTSTRAP"],
         "--target-bucket"),
        (["--write", "--target-bucket", "state", "--confirm-production-write",
          "I_AUTHORIZE_PRODUCTION_RECOVERY_BOOTSTRAP"], "lifecycle audit source"),
    ],
)
def test_bootstrap_refuses_write_without_confirmation_and_target(monkeypatch, flags, expected):
    _candidate(monkeypatch)
    monkeypatch.setattr(
        bootstrap, "PublicationStateStore",
        lambda: pytest.fail("invalid flags accessed R2"),
    )
    with pytest.raises(publication_state.StateValidationError, match=expected):
        bootstrap.main(["--evidence-dir", "unused", *flags])


def test_bootstrap_checks_target_and_existing_objects_before_any_write(monkeypatch):
    _candidate(monkeypatch)
    calls = []

    class Store:
        config = SimpleNamespace(bucket="state")

        def require_uninitialized(self):
            calls.append("absent-check")
            raise publication_state.StateConflictError("existing object")

        def create_initial(self, *_args):
            pytest.fail("existing target was overwritten")

    monkeypatch.setattr(bootstrap, "PublicationStateStore", Store)
    flags = ["--evidence-dir", "unused", "--write", "--confirm-production-write",
             "I_AUTHORIZE_PRODUCTION_RECOVERY_BOOTSTRAP", "--lifecycle-audit-source",
             "dashboard", "--confirm-dashboard-lifecycle",
             "I_VERIFIED_NO_OBJECT_EXPIRATION_FOR_state"]
    with pytest.raises(publication_state.StateValidationError, match="differs"):
        mismatched = flags.copy()
        mismatched[-1] = "I_VERIFIED_NO_OBJECT_EXPIRATION_FOR_wrong"
        bootstrap.main([*mismatched, "--target-bucket", "wrong"])
    assert calls == []
    with pytest.raises(publication_state.StateConflictError, match="existing"):
        bootstrap.main([*flags, "--target-bucket", "state"])
    assert calls == ["absent-check"]


def test_bootstrap_creates_receipts_before_activating_safety(monkeypatch):
    _candidate(monkeypatch)
    calls = []

    class Store:
        config = SimpleNamespace(bucket="state")

        def require_uninitialized(self):
            calls.append("absent-check")

        def create_initial(self, key, _payload):
            calls.append(key)
            if key == bootstrap.RECEIPTS_KEY:
                raise publication_state.StateWriteUncertainError("lost response")

    monkeypatch.setattr(bootstrap, "PublicationStateStore", Store)
    with pytest.raises(publication_state.StateWriteUncertainError):
        bootstrap.main([
            "--evidence-dir", "unused", "--write", "--target-bucket", "state",
            "--confirm-production-write", "I_AUTHORIZE_PRODUCTION_RECOVERY_BOOTSTRAP",
            "--lifecycle-audit-source", "dashboard", "--confirm-dashboard-lifecycle",
            "I_VERIFIED_NO_OBJECT_EXPIRATION_FOR_state",
        ])
    assert calls == ["absent-check", bootstrap.RECEIPTS_KEY]


def test_dashboard_lifecycle_attestation_is_required_before_bootstrap_store(monkeypatch):
    _candidate(monkeypatch)
    monkeypatch.setattr(
        bootstrap, "PublicationStateStore",
        lambda: pytest.fail("missing lifecycle attestation reached R2"),
    )
    with pytest.raises(publication_state.StateValidationError, match="dashboard lifecycle audit"):
        bootstrap.main([
            "--evidence-dir", "unused", "--write", "--target-bucket", "state",
            "--confirm-production-write", "I_AUTHORIZE_PRODUCTION_RECOVERY_BOOTSTRAP",
            "--lifecycle-audit-source", "dashboard",
        ])


def test_control_plane_lifecycle_audit_never_reuses_state_key(monkeypatch):
    _candidate(monkeypatch)
    calls = []
    runtime_config = SimpleNamespace(bucket="state", account_id="account", access_key="state-key")

    class RuntimeStore:
        config = runtime_config

        def require_uninitialized(self):
            calls.append("absent-check")
            raise publication_state.StateConflictError("existing object")

        def create_initial(self, *_args):
            pytest.fail("bootstrap wrote despite existing target")

    def store_factory(config=None):
        if config is None:
            return RuntimeStore()
        calls.append((config.account_id, config.bucket, config.access_key))
        return SimpleNamespace(config=config)

    monkeypatch.setattr(bootstrap, "PublicationStateStore", store_factory)
    monkeypatch.setattr(
        bootstrap, "validate_state_bucket_lifecycle",
        lambda store: calls.append(("audit", store.config.access_key)),
    )
    monkeypatch.setenv("CLOUDFLARE_R2_CONTROL_PLANE_ACCESS_KEY_ID", "admin-read-key")
    monkeypatch.setenv("CLOUDFLARE_R2_CONTROL_PLANE_SECRET_ACCESS_KEY", "admin-read-secret")

    with pytest.raises(publication_state.StateConflictError, match="existing"):
        bootstrap.main([
            "--evidence-dir", "unused", "--write", "--target-bucket", "state",
            "--confirm-production-write", "I_AUTHORIZE_PRODUCTION_RECOVERY_BOOTSTRAP",
            "--lifecycle-audit-source", "control-plane-s3",
        ])
    assert calls == [
        ("account", "state", "admin-read-key"),
        ("audit", "admin-read-key"),
        "absent-check",
    ]


def test_control_plane_audit_requires_explicit_credentials(monkeypatch):
    _candidate(monkeypatch)
    monkeypatch.delenv("CLOUDFLARE_R2_CONTROL_PLANE_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_R2_CONTROL_PLANE_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setattr(
        bootstrap, "PublicationStateStore",
        lambda: pytest.fail("missing control-plane credentials reached R2"),
    )
    with pytest.raises(publication_state.StateValidationError, match="Separate control-plane"):
        bootstrap.main([
            "--evidence-dir", "unused", "--write", "--target-bucket", "state",
            "--confirm-production-write", "I_AUTHORIZE_PRODUCTION_RECOVERY_BOOTSTRAP",
            "--lifecycle-audit-source", "control-plane-s3",
        ])


def test_control_plane_audit_rejects_runtime_key_reuse(monkeypatch):
    _candidate(monkeypatch)
    monkeypatch.setenv("CLOUDFLARE_R2_CONTROL_PLANE_ACCESS_KEY_ID", "state-key")
    monkeypatch.setenv("CLOUDFLARE_R2_CONTROL_PLANE_SECRET_ACCESS_KEY", "separate-secret")
    monkeypatch.setattr(
        bootstrap, "PublicationStateStore",
        lambda: SimpleNamespace(config=SimpleNamespace(bucket="state", access_key="state-key")),
    )
    monkeypatch.setattr(
        bootstrap, "validate_state_bucket_lifecycle",
        lambda _store: pytest.fail("runtime key reached lifecycle audit"),
    )
    with pytest.raises(publication_state.StateValidationError, match="separate R2 credential"):
        bootstrap.main([
            "--evidence-dir", "unused", "--write", "--target-bucket", "state",
            "--confirm-production-write", "I_AUTHORIZE_PRODUCTION_RECOVERY_BOOTSTRAP",
            "--lifecycle-audit-source", "control-plane-s3",
        ])


def test_bootstrap_store_rejects_partially_initialized_target():
    class Client:
        def get_object(self, *, Bucket, Key):
            if Key == publication_state.SAFETY_KEY:
                raise publication_state.ClientError({
                    "Error": {"Code": "NoSuchKey"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                }, "GetObject")
            return {"Body": b"existing"}

    store = publication_state.PublicationStateStore(
        publication_state.StateConfiguration("account", "state", "key", "secret"),
        Client(),
    )
    with pytest.raises(publication_state.StateConflictError, match="already contains"):
        store.require_uninitialized()


def test_bootstrap_store_does_not_treat_missing_bucket_as_empty():
    class Client:
        def get_object(self, **_kwargs):
            raise publication_state.ClientError({
                "Error": {"Code": "NoSuchBucket"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            }, "GetObject")

    store = publication_state.PublicationStateStore(
        publication_state.StateConfiguration("account", "state", "key", "secret"),
        Client(),
    )
    with pytest.raises(publication_state.StateValidationError, match="cannot be verified empty"):
        store.require_uninitialized()
