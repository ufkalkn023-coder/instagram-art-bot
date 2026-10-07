import copy
import importlib
from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime(2026, 10, 7, 19, tzinfo=timezone.utc)


def publication(index=0, format_name="single", age=80):
    return {"id": f"p-{index}", "media_id": f"m-{index}", "type": format_name,
            "artwork_ids": [f"aic_{index}"] if format_name == "single" else [f"aic_{index}", f"met_{index}"],
            "posted_at": (NOW - timedelta(hours=age)).isoformat()}


def snapshot(pub, target=72, metrics=None, status=None, failure=None, capture_age=None):
    captured_at = datetime.fromisoformat(pub["posted_at"]) + timedelta(hours=capture_age or target)
    return {"publication_id": pub["id"], "media_id": pub["media_id"],
            "target_age_hours": target, "captured_at": captured_at.isoformat(),
            "metrics": {"reach": 100, "saved": 0} if metrics is None else metrics,
            **({"completion_status": status} if status else {}),
            **({"failure_category": failure} if failure else {})}


def report(history, snapshots, **kwargs):
    assert importlib.util.find_spec("src.feed_analytics") is not None, "Feed analytics report is not implemented"
    return importlib.import_module("src.feed_analytics").build_feed_analytics_report(history, snapshots, now=NOW, **kwargs)


def test_missing_windows_are_not_missing_posts_and_closed_windows_cannot_be_retried():
    pub = publication(age=80)
    result = report({"publications": [pub]}, [])
    windows = result["windows"]
    assert [row["status"] for row in windows] == ["missed", "missed", "missed", "due", "pending"]
    assert result["summary"]["missed_windows"] == 3
    assert result["summary"]["publications_with_missed_windows"] == 1
    assert [row["recoverable"] for row in windows] == [False, False, False, True, False]


def test_append_only_attempts_and_duplicate_missed_markers_count_one_window():
    pub = publication(age=80)
    marker = snapshot(pub, target=24, metrics={}, status="permanently_unavailable",
                      failure="missed_collection_window", capture_age=80)
    result = report({"publications": [pub]}, [marker, copy.deepcopy(marker),
                    snapshot(pub, metrics={"reach": 100}), snapshot(pub)])
    row = next(row for row in result["windows"] if row["target_age_hours"] == 72)
    assert row["status"] == "complete"
    assert row["metrics"]["saved"] == 0
    assert result["summary"]["missed_windows"] == 3


def test_partial_unavailable_and_future_attempts_have_distinct_reasons():
    pub = publication(age=80)
    partial = snapshot(pub, metrics={"reach": 100}, status="partial")
    result = report({"publications": [pub]}, [partial])
    row = next(row for row in result["windows"] if row["target_age_hours"] == 72)
    assert row["status"] == "partial" and row["recoverable"]
    terminal = snapshot(pub, metrics={}, status="permanently_unavailable", failure="unsupported_media")
    result = report({"publications": [pub]}, [partial, terminal])
    row = next(row for row in result["windows"] if row["target_age_hours"] == 72)
    assert row["status"] == "unavailable" and not row["recoverable"]
    future = snapshot(pub, capture_age=100)
    assert report({"publications": [pub]}, [future])["diagnostics"]["future_snapshots"] == 1


def test_conflicting_media_identity_is_excluded_and_inputs_are_unchanged():
    pub = publication()
    wrong = {**snapshot(pub), "media_id": "wrong"}
    history, snapshots = {"publications": [pub]}, [wrong]
    before = copy.deepcopy((history, snapshots))
    result = report(history, snapshots)
    assert result["summary"]["complete_windows"] == 0
    assert result["diagnostics"]["snapshot_identity_mismatches"] == 1
    assert (history, snapshots) == before


def test_duplicate_publication_or_media_owners_are_not_arbitrarily_selected():
    pub = publication()
    duplicate = {**publication(1), "media_id": pub["media_id"]}
    result = report({"publications": [pub, duplicate]}, [snapshot(pub)])
    assert result["windows"] == []
    assert result["diagnostics"]["conflicting_publications"] == 2


def test_age_boundaries_follow_existing_collector_and_naive_now_is_rejected():
    pub = publication(age=72)
    rows = report({"publications": [pub]}, [])["windows"]
    assert next(row for row in rows if row["target_age_hours"] == 24)["status"] == "missed"
    assert next(row for row in rows if row["target_age_hours"] == 72)["status"] == "due"
    with pytest.raises(ValueError):
        importlib.import_module("src.feed_analytics").build_feed_analytics_report({}, [], now=NOW.replace(tzinfo=None))


def test_format_cohorts_use_metric_specific_denominators_and_preserve_zero():
    pubs = [publication(0), publication(1), publication(2, "carousel")]
    attempts = [snapshot(pubs[0], metrics={"reach": 100, "saved": 0}),
                snapshot(pubs[1], metrics={"reach": 200, "likes": 20}),
                snapshot(pubs[2], metrics={"reach": 300, "saved": 30})]
    result = report({"publications": pubs}, attempts)
    assert "cohorts" in result, "Equal-age format comparison is missing"
    single = next(c for c in result["cohorts"] if c["publication_format"] == "single" and c["target_age_hours"] == 72)
    assert single["usable_publications"] == 2
    assert single["rates"]["save_rate"]["observations"] == 1
    assert single["rates"]["save_rate"]["mean_rate"] == 0
    assert single["rates"]["like_rate"]["mean_rate"] == .1
    assert single["rates"]["share_rate"]["mean_rate"] is None
    assert single["rates"]["like_rate"]["weighted_rate"] == .1
    comparison = next(c for c in result["comparisons"] if c["target_age_hours"] == 72)
    assert comparison["status"] == "insufficient_data"
    assert comparison["winner"] is None


def test_multiple_slots_and_duplicate_attempts_do_not_inflate_same_age_sample():
    pubs = [publication(i, "single" if i < 2 else "carousel", age=200) for i in range(4)]
    attempts = [snapshot(p) for p in pubs] + [snapshot(p, target=168) for p in pubs]
    attempts.append(copy.deepcopy(attempts[0]))
    result = report({"publications": pubs}, attempts, minimum_cohort_size=2)
    for age in (72, 168):
        cohorts = [c for c in result["cohorts"] if c["target_age_hours"] == age]
        assert [c["usable_publications"] for c in cohorts] == [2, 2]
        comparison = next(c for c in result["comparisons"] if c["target_age_hours"] == age)
        assert comparison["status"] == "descriptive_only"
        assert comparison["winner"] is None
    assert next(c for c in result["comparisons"] if c["target_age_hours"] == 24)["status"] == "insufficient_data"


def test_same_target_but_different_capture_ages_are_flagged_instead_of_promoting_a_winner():
    pubs = [publication(i, "single" if i < 2 else "carousel", age=200) for i in range(4)]
    attempts = [snapshot(p, capture_age=73 if p["type"] == "single" else 160) for p in pubs]
    result = report({"publications": pubs}, attempts, minimum_cohort_size=2)
    comparison = next(c for c in result["comparisons"] if c["target_age_hours"] == 72)
    assert comparison["status"] == "capture_age_mismatch"
    assert comparison["winner"] is None


def test_late_labeled_snapshots_cannot_enter_equal_age_comparison():
    pub = publication(age=200)
    result = report({"publications": [pub]}, [snapshot(pub, target=72, capture_age=190)])
    cohort = next(c for c in result["cohorts"] if c["publication_format"] == "single" and c["target_age_hours"] == 72)
    assert cohort["usable_publications"] == 0
    assert result["summary"]["complete_windows"] == 0
    row = next(row for row in result["windows"] if row["target_age_hours"] == 72)
    assert row["status"] == "missed"
    assert row["reason"] == "late_snapshot_outside_collection_window"


def test_late_attempt_does_not_displace_valid_in_window_measurement():
    pub = publication(age=200)
    result = report({"publications": [pub]}, [
        snapshot(pub, capture_age=73, metrics={"reach": 100, "saved": 10}),
        snapshot(pub, capture_age=190, metrics={"reach": 1000, "saved": 900}),
    ])
    row = next(row for row in result["windows"] if row["target_age_hours"] == 72)
    assert row["comparable"] is True
    assert row["captured_age_hours"] == 73
    assert row["attempt_count"] == 2
    cohort = next(c for c in result["cohorts"] if c["publication_format"] == "single" and c["target_age_hours"] == 72)
    assert cohort["usable_publications"] == 1
    assert cohort["rates"]["save_rate"]["mean_rate"] == .1


@pytest.mark.parametrize("field,value", [("failure_category", []), ("completion_status", []),
                                        ("completion_status", "unknown")])
def test_malformed_attempt_classification_is_counted_and_excluded(field, value):
    pub = publication()
    result = report({"publications": [pub]}, [{**snapshot(pub), field: value}])
    assert result["diagnostics"]["invalid_snapshots"] == 1
    assert result["summary"]["complete_windows"] == 0
