import ast
import inspect
import json
from pathlib import Path

from PIL import Image

from src.models import NormalizedArtwork
from src.quality_filter import ImageValidationResult
import src.reel_candidate_acquisition as acquisition
from src.reel_candidate_acquisition import acquire_reel_candidate_pool
from src.reel_handoff import export_reel_handoff


class FakeAdapter:
    def __init__(self, source_id, candidates=(), error=None):
        self.source_id = source_id
        self.candidates = list(candidates)
        self.error = error
        self.calls = 0

    def fetch_candidates(self, **_kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.candidates)


def artwork(source: str, source_id: str, **overrides) -> NormalizedArtwork:
    values = {
        "source": source,
        "source_id": source_id,
        "title": f"Artwork {source_id}",
        "artist_name": f"Artist {source_id}",
        "creation_date": "1900",
        "medium": "Oil on canvas",
        "classification": "Painting",
        "museum_name": f"{source} Museum",
        "image_url": f"https://images.example.test/{source_id}.jpg?never=manifested",
        "is_public_domain": True,
        "rights_status": "CONFIRMED_PUBLIC_DOMAIN",
    }
    values.update(overrides)
    return NormalizedArtwork(**values)


def image_downloader(size=(1800, 1200), corrupt=False, calls=None):
    def download(_url: str, output_path: str) -> ImageValidationResult:
        if calls is not None:
            calls.append(output_path)
        if corrupt:
            Path(output_path).write_bytes(b"not an image")
        else:
            Image.new("RGB", size, color=(50, 80, 120)).save(output_path, "JPEG")
        return ImageValidationResult(True, width=size[0], height=size[1], image_format="JPEG", reason="ok")
    return download


def run_pool(tmp_path, *, adapters, pool_size, attempt_limit, downloader=None, excluded_canonical_ids=(), environment=None):
    resolved_environment = {"REEL_SELECTION_TARGET": "2", "REEL_BATCH_CANDIDATE_LIMIT": "2"}
    if environment:
        resolved_environment.update(environment)
    return acquire_reel_candidate_pool(
        pool_size=pool_size,
        attempt_limit=attempt_limit,
        adapters=adapters,
        handoff_directory=tmp_path / "handoffs",
        manifest_path=tmp_path / "acquisition.json",
        work_directory=tmp_path / "work",
        downloader=downloader or image_downloader(),
        excluded_canonical_ids=excluded_canonical_ids,
        environment=resolved_environment,
    )


def reusable_handoff(root, source_id: str):
    handoffs = root / "handoffs"
    asset = root / "assets" / f"met_{source_id}.jpg"
    asset.parent.mkdir(exist_ok=True)
    Image.new("RGB", (1800, 1200), color=(1, 2, 3)).save(asset, "JPEG")
    export_reel_handoff(artwork("met", source_id, image_width=1800, image_height=1200), asset, handoffs)


def populate_reusable_handoffs(root, count: int = 24):
    for index in range(count):
        reusable_handoff(root, f"existing_{index}")


def test_fills_pool_with_round_robin_source_traversal_and_stops_at_target(tmp_path):
    aic = FakeAdapter("aic", [artwork("aic", "1"), artwork("aic", "2")])
    met = FakeAdapter("met", [artwork("met", "1"), artwork("met", "2")])

    result = run_pool(tmp_path, adapters=[aic, met], pool_size=3, attempt_limit=4)

    manifest = result.manifest
    assert [entry["canonicalId"] for entry in manifest["acceptedCandidates"]] == ["aic_1", "met_1", "aic_2"]
    assert manifest["attemptedCount"] == 3
    assert manifest["safeCandidateCount"] == 3
    assert manifest["shortfall"] == 0
    assert manifest["sourceFailures"] == {}
    assert json.loads((tmp_path / "acquisition.json").read_text()) == manifest


def test_attempt_limit_rights_metadata_download_corruption_resolution_and_duplicates_are_isolated(tmp_path):
    calls = []
    candidates = [
        artwork("met", "open", rights_status="CONFIRMED_OPEN_ACCESS"),
        artwork("met", "metadata", medium=None),
        artwork("met", "low"),
        artwork("met", "corrupt"),
        artwork("met", "duplicate"),
        artwork("met", "duplicate"),
        artwork("met", "good"),
    ]
    adapter = FakeAdapter("met", candidates)

    # The first download is low resolution; subsequent valid downloads use the
    # normal helper.  This keeps every rejection networkless.
    def downloader(url, output_path):
        if url.endswith("low.jpg?never=manifested"):
            return image_downloader((640, 500))(url, output_path)
        if url.endswith("corrupt.jpg?never=manifested"):
            return image_downloader(corrupt=True)(url, output_path)
        return image_downloader(calls=calls)(url, output_path)

    result = run_pool(tmp_path, adapters=[adapter], pool_size=2, attempt_limit=7, downloader=downloader)

    manifest = result.manifest
    assert manifest["safeCandidateCount"] == 2
    assert manifest["attemptedCount"] == 7
    assert manifest["shortfall"] == 0
    assert {item["reasonCode"] for item in manifest["rejections"]} >= {
        "RIGHTS_REJECTED", "METADATA_INCOMPLETE", "RESOLUTION_TOO_LOW", "IMAGE_VALIDATION_FAILED", "DUPLICATE_CANONICAL_ID",
    }
    assert manifest["sources"] == [{
        "source": "met", "attempted": 7, "accepted": 2, "rejected": 5, "failed": 0,
        "rejectionReasons": {
            "DUPLICATE_CANONICAL_ID": 1,
            "IMAGE_VALIDATION_FAILED": 1,
            "METADATA_INCOMPLETE": 1,
            "RESOLUTION_TOO_LOW": 1,
            "RIGHTS_REJECTED": 1,
        },
    }]
    assert [candidate["canonicalId"] for candidate in manifest["acceptedCandidates"]] == ["met_duplicate", "met_good"]
    assert calls  # only the eligible-looking candidate reaches the final valid downloader


def test_reuses_valid_handoff_and_acquires_only_missing_capacity(tmp_path):
    handoffs = tmp_path / "handoffs"
    original = artwork("aic", "reused", image_width=1800, image_height=1200)
    local_image = tmp_path / "reused.jpg"
    Image.new("RGB", (1800, 1200), color=(1, 2, 3)).save(local_image, "JPEG")
    export_reel_handoff(original, local_image, handoffs)
    downloads = []

    result = run_pool(
        tmp_path,
        adapters=[FakeAdapter("met", [artwork("met", "fresh")])],
        pool_size=2,
        attempt_limit=2,
        downloader=image_downloader(calls=downloads),
    )

    assert result.manifest["safeCandidateCount"] == 2
    assert result.manifest["attemptedCount"] == 1
    assert result.manifest["acceptedCandidates"][0]["reused"] is True
    assert result.manifest["acceptedCandidates"][1]["reused"] is False
    assert len(downloads) == 1


def test_full_usable_existing_pool_needs_no_fresh_acquisition(tmp_path):
    populate_reusable_handoffs(tmp_path)
    adapter = FakeAdapter("met", [artwork("met", "unused")])

    result = run_pool(tmp_path, adapters=[adapter], pool_size=24, attempt_limit=24)

    manifest = result.manifest
    assert adapter.calls == 0
    assert manifest["existingSafeCount"] == 24
    assert manifest["historyExcludedCount"] == 0
    assert manifest["usableExistingCount"] == 24
    assert manifest["newlyAcquiredCount"] == 0
    assert manifest["usableSafeCandidateCount"] == 24
    assert manifest["shortfall"] == 0


def test_production_history_exclusions_reduce_capacity_and_acquire_replacements(tmp_path):
    populate_reusable_handoffs(tmp_path)
    excluded = tuple(f"met_existing_{index}" for index in range(8))
    downloads = []

    result = run_pool(
        tmp_path,
        adapters=[FakeAdapter("met", [artwork("met", f"fresh_{index}") for index in range(8)])],
        pool_size=24,
        attempt_limit=24,
        downloader=image_downloader(calls=downloads),
        excluded_canonical_ids=excluded,
    )

    manifest = result.manifest
    assert manifest["existingSafeCount"] == 24
    assert manifest["historyExcludedCount"] == 8
    assert manifest["usableExistingCount"] == 16
    assert manifest["newlyAcquiredCount"] == 8
    assert manifest["safeCandidateCount"] == manifest["usableSafeCandidateCount"] == 24
    assert manifest["shortfall"] == 0
    assert len(downloads) == 8
    assert all(candidate["reused"] for candidate in manifest["acceptedCandidates"][:16])


def test_history_duplicate_fetched_candidate_is_skipped_before_download(tmp_path):
    downloads = []

    result = run_pool(
        tmp_path,
        adapters=[FakeAdapter("met", [artwork("met", "produced"), artwork("met", "fresh_one"), artwork("met", "fresh_two")])],
        pool_size=2,
        attempt_limit=3,
        downloader=image_downloader(calls=downloads),
        excluded_canonical_ids=("met_produced",),
    )

    manifest = result.manifest
    assert manifest["attemptedCount"] == 3
    assert manifest["newlyAcquiredCount"] == 2
    assert [candidate["canonicalId"] for candidate in manifest["acceptedCandidates"]] == ["met_fresh_one", "met_fresh_two"]
    assert any(rejection["reasonCode"] == "PRODUCTION_HISTORY_DUPLICATE" for rejection in manifest["rejections"])
    assert len(downloads) == 2


def test_real_shortfall_regression_replenishes_when_only_two_of_twenty_four_are_usable(tmp_path):
    populate_reusable_handoffs(tmp_path)
    excluded = tuple(f"met_existing_{index}" for index in range(22))
    adapter = FakeAdapter("met", [artwork("met", f"replacement_{index}") for index in range(22)])

    result = run_pool(
        tmp_path,
        adapters=[adapter],
        pool_size=24,
        attempt_limit=24,
        excluded_canonical_ids=excluded,
        environment={"REEL_SELECTION_TARGET": "4", "REEL_BATCH_CANDIDATE_LIMIT": "4"},
    )

    manifest = result.manifest
    assert manifest["existingSafeCount"] == 24
    assert manifest["historyExcludedCount"] == 22
    assert manifest["usableExistingCount"] == 2
    assert manifest["newlyAcquiredCount"] == 22
    assert manifest["usableSafeCandidateCount"] == 24
    assert manifest["shortfall"] == 0
    assert adapter.calls == 1


def test_source_failure_does_not_stop_other_source_and_manifest_excludes_url_secrets(tmp_path):
    result = run_pool(
        tmp_path,
        adapters=[FakeAdapter("aic", error=RuntimeError("token=private")), FakeAdapter("met", [artwork("met", "1")])],
        pool_size=2,
        attempt_limit=2,
    )

    manifest_text = json.dumps(result.manifest)
    assert result.manifest["safeCandidateCount"] == 1
    assert result.manifest["sourceFailureCount"] == 1
    assert result.manifest["shortfall"] == 1
    assert "token=private" not in manifest_text
    assert "never=manifested" not in manifest_text


def test_aic_http_failure_has_bounded_diagnostics_without_changing_other_sources(tmp_path):
    def downloader(_url, _output_path):
        return ImageValidationResult(False, reason="http_status", http_status=403)

    result = run_pool(
        tmp_path,
        adapters=[FakeAdapter("aic", [artwork("aic", "blocked")]), FakeAdapter("met", [artwork("met", "blocked")])],
        pool_size=2,
        attempt_limit=2,
        downloader=downloader,
    )

    rejections = {entry["canonicalId"]: entry for entry in result.manifest["rejections"]}
    assert rejections["aic_blocked"]["downloadFailureCategory"] == "http_status"
    assert rejections["aic_blocked"]["httpStatus"] == 403
    assert "downloadFailureCategory" not in rejections["met_blocked"]
    assert "httpStatus" not in rejections["met_blocked"]


def test_aic_cloudflare_challenge_is_source_wide_and_fails_fast_for_this_run(tmp_path):
    calls = []

    def downloader(url, output_path):
        calls.append(url)
        if "blocked-" in url:
            return ImageValidationResult(
                False,
                reason="http_status",
                http_status=403,
                cloudflare_challenge=True,
            )
        return image_downloader()(url, output_path)

    aic = FakeAdapter("aic", [artwork("aic", "blocked-one"), artwork("aic", "blocked-two")])
    result = run_pool(
        tmp_path,
        adapters=[aic, FakeAdapter("met", [artwork("met", "good")])],
        pool_size=2,
        attempt_limit=3,
        downloader=downloader,
    )

    assert result.manifest["sourceFailures"] == {"aic": {"category": "CLOUDFLARE_CHALLENGE"}}
    assert result.manifest["sourceFailureCount"] == 1
    assert [entry["canonicalId"] for entry in result.manifest["acceptedCandidates"]] == ["met_good"]
    assert calls == [
        "https://images.example.test/blocked-one.jpg?never=manifested",
        "https://images.example.test/good.jpg?never=manifested",
    ]
    assert "blocked-two.jpg" not in "\n".join(calls)


def test_source_health_is_reset_for_the_next_independent_acquisition_run(tmp_path):
    calls = []

    def cloudflare_downloader(url, _output_path):
        calls.append(url)
        return ImageValidationResult(False, reason="http_status", http_status=403, cloudflare_challenge=True)

    aic = FakeAdapter("aic", [artwork("aic", "blocked")])
    first = run_pool(
        tmp_path / "first",
        adapters=[aic],
        pool_size=2,
        attempt_limit=2,
        downloader=cloudflare_downloader,
    )
    second = run_pool(
        tmp_path / "second",
        adapters=[aic],
        pool_size=2,
        attempt_limit=2,
        downloader=cloudflare_downloader,
    )

    assert first.manifest["sourceFailures"] == second.manifest["sourceFailures"] == {
        "aic": {"category": "CLOUDFLARE_CHALLENGE"}
    }
    assert calls == [
        "https://images.example.test/blocked.jpg?never=manifested",
        "https://images.example.test/blocked.jpg?never=manifested",
    ]


def test_missing_credential_is_a_deterministic_source_failure_without_a_request(tmp_path):
    rijksmuseum = FakeAdapter("rijksmuseum", [artwork("rijksmuseum", "unused")])

    result = run_pool(tmp_path, adapters=[rijksmuseum], pool_size=2, attempt_limit=2)

    assert rijksmuseum.calls == 0
    assert result.manifest["sourceFailures"] == {"rijksmuseum": {"category": "MISSING_CREDENTIAL"}}


def test_artwork_level_rights_and_download_rejections_do_not_disable_a_source(tmp_path):
    rights_source = FakeAdapter("met", [artwork("met", "restricted", is_public_domain=False), artwork("met", "good")])
    rights_result = run_pool(tmp_path / "rights", adapters=[rights_source], pool_size=2, attempt_limit=2)

    assert rights_result.manifest["sourceFailures"] == {}
    assert rights_result.manifest["safeCandidateCount"] == 1

    def failed_first_download(url, output_path):
        if "download-failed" in url:
            return ImageValidationResult(False, reason="network_error")
        return image_downloader()(url, output_path)

    image_source = FakeAdapter("met", [artwork("met", "download-failed"), artwork("met", "good")])
    image_result = run_pool(
        tmp_path / "downloads",
        adapters=[image_source],
        pool_size=2,
        attempt_limit=2,
        downloader=failed_first_download,
    )

    assert image_result.manifest["sourceFailures"] == {}
    assert image_result.manifest["safeCandidateCount"] == 1


def test_acquisition_has_no_gemini_or_publishing_dependency():
    imports = []
    for node in ast.walk(ast.parse(inspect.getsource(acquisition))):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)

    assert not any("gemini" in module for module in imports)
    assert not any("poster" in module or "publish" in module for module in imports)
    assert not any("history_tracker" in module for module in imports)
