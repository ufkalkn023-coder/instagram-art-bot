"""Private prepared packages shared by ephemeral runners, fenced by manifest CAS.

Claims have no expiring lease: a crashed/uncertain owner remains quarantined
operationally until reviewed. Publication authorization remains in safety state.
"""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from src.feed_queue import MAX_MANIFEST_BYTES, PreparedFeedQueue, _now
from src.insights_storage import parse_aware_timestamp
from src import publication_state
from src.publication_state import StateConfiguration, canonical_bytes, seal

QUEUE_PREFIX = "feed-queue/v1"
MANIFEST_KEY = f"{QUEUE_PREFIX}/manifest.json"
MAX_ASSET_BYTES = 8_000_000


class R2PreparedFeedQueue(PreparedFeedQueue):
    manifest_key = MANIFEST_KEY

    def __init__(self, directory: Path | str, *, config: StateConfiguration | None = None, client=None):
        super().__init__(directory)
        if config is not None and client is not None:
            self.config, self.client = config, client
        else:
            store = publication_state.PublicationStateStore(config=config, client=client)
            self.config, self.client = store.config, store.client
        self._snapshot = None
        self._etag = None

    def _read_object(self, key: str, maximum: int, *, allow_missing: bool = False):
        try:
            response = self.client.get_object(Bucket=self.config.bucket, Key=key)
        except ClientError as error:
            if allow_missing and error.response.get("Error", {}).get("Code") == "NoSuchKey":
                return None, None
            raise RuntimeError("Prepared R2 object is unreadable") from error
        except (BotoCoreError, OSError) as error:
            raise RuntimeError("Prepared R2 object is unreadable") from error
        body = response.get("Body")
        try:
            headers = response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
            if "Expiration" in response or any(str(name).lower() == "x-amz-expiration" for name in headers):
                raise RuntimeError("Prepared R2 object has an expiration policy")
            etag = response.get("ETag")
            if not isinstance(etag, str) or len(etag) < 3 or not etag.startswith('"') or not etag.endswith('"'):
                raise RuntimeError("Prepared R2 object has no strong ETag")
            length = response.get("ContentLength")
            if length is not None and (type(length) is not int or not 0 <= length <= maximum):
                raise RuntimeError("Prepared R2 object exceeds its size bound")
            data = body.read(maximum + 1)
            if not isinstance(data, bytes) or len(data) > maximum:
                raise RuntimeError("Prepared R2 object exceeds its size bound")
            return data, etag
        except (BotoCoreError, OSError) as error:
            raise RuntimeError("Prepared R2 object read failed") from error
        finally:
            if body is not None:
                body.close()

    def _read_manifest(self):
        data, etag = self._read_object(self.manifest_key, MAX_MANIFEST_BYTES, allow_missing=True)
        if data is None:
            return {"schema_version": 1, "packages": []}, None
        try:
            document = self.validate_document(json.loads(data))
        except (ValueError, TypeError, KeyError) as error:
            raise RuntimeError("Prepared R2 manifest failed validation") from error
        return document, etag

    def _load(self):
        return copy.deepcopy(self._snapshot) if self._snapshot is not None else self._read_manifest()[0]

    @contextmanager
    def _lock(self):
        if self._snapshot is not None:
            raise RuntimeError("Prepared R2 operation is already active")
        self._snapshot, self._etag = self._read_manifest()
        try:
            yield
        finally:
            self._snapshot, self._etag = None, None

    def _put(self, key: str, data: bytes, *, etag: str | None, content_type: str):
        condition = {"IfMatch": etag} if etag is not None else {"IfNoneMatch": "*"}
        try:
            self.client.put_object(Bucket=self.config.bucket, Key=key, Body=data,
                                   ContentType=content_type, **condition)
        except ClientError as error:
            if (error.response.get("Error", {}).get("Code") in {"PreconditionFailed", "412"}
                    or error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 412):
                raise RuntimeError("Prepared R2 ownership conflict") from error
            raise RuntimeError("Prepared R2 write outcome is uncertain; do not retry") from error
        except (BotoCoreError, OSError) as error:
            raise RuntimeError("Prepared R2 write outcome is uncertain; do not retry") from error

    def _write(self, document: dict):
        if self._snapshot is None:
            raise RuntimeError("Prepared R2 mutation requires an observed snapshot")
        encoded = canonical_bytes(seal(document))
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise ValueError("Prepared R2 manifest exceeds its bound")
        self.validate_document(json.loads(encoded))
        self._put(self.manifest_key, encoded, etag=self._etag, content_type="application/json")
        observed, etag = self._read_manifest()
        if canonical_bytes(observed) != encoded:
            raise RuntimeError("Prepared R2 read-after-write is uncertain; do not retry")
        self._snapshot, self._etag = observed, etag

    def _content(self, package: dict):
        assets = package["content"].get("assets")
        if not isinstance(assets, list) or not 1 <= len(assets) <= 9:
            raise ValueError("Invalid prepared R2 asset list")
        for asset in assets:
            path = self._asset_path(package["id"], asset["path"])
            key = f"{QUEUE_PREFIX}/packages/{asset['path']}"
            data, _ = self._read_object(key, MAX_ASSET_BYTES)
            if hashlib.sha256(data).hexdigest() != asset["sha256"]:
                raise ValueError("Prepared R2 media digest mismatch")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return super()._content(package)

    def build(self, **kwargs):
        raise RuntimeError("Build a local validated batch, then install it explicitly")

    def install(self, local: PreparedFeedQueue, *, now: datetime | None = None):
        timestamp = _now(now)
        document = local._load()
        if not document["packages"]:
            raise ValueError("A prepared batch is required")
        # Validate the entire batch before any upload, not only its first entry.
        assets = []
        ids = set()
        previous_format = None
        for package in document["packages"]:
            created = parse_aware_timestamp(package.get("created_at"))
            expiry = parse_aware_timestamp(package.get("expires_at"))
            if (package["state"] != "READY" or package.get("owner") is not None
                    or created is None or expiry is None or created > timestamp or expiry <= timestamp
                    or expiry <= created or (expiry - created).total_seconds() > 14 * 24 * 3600):
                raise ValueError("Prepared batch is expired or not fresh and READY")
            content = local._content(package)
            if previous_format == content.publication_format or ids.intersection(content.publication_ids):
                raise ValueError("Prepared batch must alternate and exclude repeated artwork")
            previous_format = content.publication_format
            ids.update(content.publication_ids)
            assets.extend((f"{QUEUE_PREFIX}/packages/{asset['path']}",
                           local._asset_path(package["id"], asset["path"]).read_bytes())
                          for asset in package["content"]["assets"])
        with self._lock():
            existing = self._load()
            for item in existing["packages"]:
                expiry = parse_aware_timestamp(item.get("expires_at"))
                if item["state"] == "READY" and expiry is not None and expiry <= timestamp:
                    item.update(state="QUARANTINED", reason="expired")
            if any(item["state"] in {"READY", "CLAIMED"} for item in existing["packages"]):
                raise RuntimeError("Prepared R2 batch contains unfinished packages")
            if existing["packages"]:
                archive = canonical_bytes(seal(existing))
                key = f"{QUEUE_PREFIX}/archives/{hashlib.sha256(archive).hexdigest()}.json"
                observed, _ = self._read_object(key, MAX_MANIFEST_BYTES, allow_missing=True)
                if observed is None:
                    self._put(key, archive, etag=None, content_type="application/json")
                elif observed != archive:
                    raise RuntimeError("Prepared R2 archive identity conflict")
            for key, data in assets:
                self._put(key, data, etag=None, content_type="image/jpeg")
            self._write(document)
