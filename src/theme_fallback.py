"""Deterministic, run-local diversification for bounded carousel theme attempts."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Sequence

from src.carousel_themes import CarouselThemeDefinition, ThemeEvidenceMode


_FRAGILE_POOL_FAILURES = frozenset(
    {
        "insufficient_final_relevance_pool",
        "insufficient_relevance_pool",
    }
)


@dataclass(frozen=True)
class ThemeAttemptFailure:
    theme: CarouselThemeDefinition
    reason: str


class ThemeAttemptPlanner:
    """Choose a bounded sequence while preserving ranking within diversity tiers."""

    def __init__(
        self,
        ranked_themes: Sequence[CarouselThemeDefinition],
        *,
        attempt_limit: int,
    ) -> None:
        if attempt_limit < 1:
            raise ValueError("attempt_limit must be positive")
        unique: list[CarouselThemeDefinition] = []
        seen_ids: set[str] = set()
        for theme in ranked_themes:
            if theme.id in seen_ids:
                continue
            seen_ids.add(theme.id)
            unique.append(theme)
        self._ranked = tuple(unique)
        self._rank = {theme.id: index for index, theme in enumerate(self._ranked)}
        self._attempt_limit = attempt_limit
        self._attempted: list[CarouselThemeDefinition] = []
        self._failures: list[ThemeAttemptFailure] = []

    @property
    def failures(self) -> tuple[ThemeAttemptFailure, ...]:
        return tuple(self._failures)

    def _remaining(self) -> list[CarouselThemeDefinition]:
        attempted_ids = {theme.id for theme in self._attempted}
        return [theme for theme in self._ranked if theme.id not in attempted_ids]

    def _choose(self) -> CarouselThemeDefinition | None:
        if len(self._attempted) >= self._attempt_limit:
            return None
        remaining = self._remaining()
        if not remaining:
            return None
        if not self._attempted:
            return remaining[0]

        format_counts = Counter(theme.format for theme in self._attempted)
        family_counts = Counter(theme.family for theme in self._attempted)
        evidence_counts = Counter(theme.evidence_mode for theme in self._attempted)
        last_theme = self._attempted[-1]
        last_failure = self._failures[-1] if self._failures else None

        repeated_fragile_signatures = {
            (failure.theme.format, failure.theme.evidence_mode)
            for failure in self._failures
            if failure.reason in _FRAGILE_POOL_FAILURES
            and sum(
                other.reason == failure.reason
                and other.theme.format is failure.theme.format
                and other.theme.evidence_mode is failure.theme.evidence_mode
                for other in self._failures
            )
            >= 2
        }
        unsuppressed = [
            theme
            for theme in remaining
            if (theme.format, theme.evidence_mode) not in repeated_fragile_signatures
        ]
        if unsuppressed:
            remaining = unsuppressed

        only_one_format_used = len(format_counts) == 1
        different_format_available = any(
            theme.format is not last_theme.format for theme in remaining
        )
        metadata_not_attempted = not any(
            theme.evidence_mode is ThemeEvidenceMode.METADATA
            for theme in self._attempted
        )
        metadata_available = any(
            theme.evidence_mode is ThemeEvidenceMode.METADATA for theme in remaining
        )

        def priority(theme: CarouselThemeDefinition) -> tuple[int, ...]:
            repeats_last_fragile_failure = int(
                last_failure is not None
                and last_failure.reason in _FRAGILE_POOL_FAILURES
                and theme.format is last_failure.theme.format
                and theme.evidence_mode is last_failure.theme.evidence_mode
            )
            prevents_format_diversity = int(
                only_one_format_used
                and different_format_available
                and theme.format is last_theme.format
            )
            misses_metadata_safety_net = int(
                metadata_not_attempted
                and metadata_available
                and theme.evidence_mode is not ThemeEvidenceMode.METADATA
            )
            diversified_rank = (
                self._rank[theme.id]
                + 4 * format_counts[theme.format]
                + 3 * family_counts[theme.family]
                + 2 * evidence_counts[theme.evidence_mode]
                + 4 * int(theme.format is last_theme.format)
                + 2 * int(theme.family is last_theme.family)
            )
            return (
                repeats_last_fragile_failure,
                prevents_format_diversity,
                misses_metadata_safety_net,
                diversified_rank,
                self._rank[theme.id],
            )

        return min(remaining, key=priority)

    def next_theme(self) -> CarouselThemeDefinition | None:
        theme = self._choose()
        if theme is not None:
            self._attempted.append(theme)
        return theme

    def record_failure(self, theme: CarouselThemeDefinition, reason: str) -> None:
        if not self._attempted or self._attempted[-1].id != theme.id:
            raise ValueError("failure must correspond to the most recent theme attempt")
        self._failures.append(ThemeAttemptFailure(theme, reason))

    def preview(self) -> tuple[CarouselThemeDefinition, ...]:
        """Return the deterministic no-failure plan without mutating run state."""
        preview = ThemeAttemptPlanner(self._ranked, attempt_limit=self._attempt_limit)
        planned: list[CarouselThemeDefinition] = []
        while theme := preview.next_theme():
            planned.append(theme)
        return tuple(planned)
