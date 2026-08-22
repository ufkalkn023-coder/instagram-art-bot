from src import art_fetcher


def test_same_seed_and_candidate_produce_the_same_bonus_in_range():
    first = art_fetcher.calculate_serendipity_bonus("seed-alpha", "aic_123")
    second = art_fetcher.calculate_serendipity_bonus("seed-alpha", "aic_123")

    assert first == second
    assert 0.0 <= first <= 5.0


def test_known_seed_and_candidate_pairs_have_stable_distinct_values():
    first = art_fetcher.calculate_serendipity_bonus("seed-alpha", "aic_123")
    second = art_fetcher.calculate_serendipity_bonus("seed-beta", "aic_123")
    third = art_fetcher.calculate_serendipity_bonus("seed-alpha", "met_456")

    assert first == 1.188597535893784
    assert second == 3.633284645703003
    assert third == 0.7856761406603258


def test_candidate_serendipity_is_order_independent():
    candidate_ids = ["aic_1", "met_2", "cleveland_3"]
    first_order = {candidate_id: art_fetcher.calculate_serendipity_bonus("run-42", candidate_id) for candidate_id in candidate_ids}
    second_order = {
        candidate_id: art_fetcher.calculate_serendipity_bonus("run-42", candidate_id)
        for candidate_id in reversed(candidate_ids)
    }

    assert first_order == second_order


def test_seed_precedence_prefers_explicit_over_github_and_ignores_retry_attempt():
    explicit = art_fetcher.resolve_selection_run_seed(
        {
            art_fetcher.SELECTION_SEED_ENV: "test-123",
            "GITHUB_RUN_ID": "github-run",
            "GITHUB_RUN_ATTEMPT": "3",
        }
    )
    github = art_fetcher.resolve_selection_run_seed(
        {"GITHUB_RUN_ID": "github-run", "GITHUB_RUN_ATTEMPT": "3"}
    )

    assert (explicit.value, explicit.source) == ("test-123", "explicit")
    assert (github.value, github.source) == ("github-run", "github_run")


def test_local_seed_uses_single_injected_entropy_value_when_no_environment_seed_exists():
    seed = art_fetcher.resolve_selection_run_seed({}, entropy_source=lambda bytes_count: "local-seed")

    assert (seed.value, seed.source) == ("local-seed", "local")
    assert len(seed.fingerprint) == 12
