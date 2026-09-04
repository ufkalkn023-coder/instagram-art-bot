"""Deterministic bounded production carousel theme attempts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from src.carousel_themes import CarouselThemeDefinition, ThemeEvidenceMode


@dataclass(frozen=True)
class ThemeAttemptFailure:
    theme: CarouselThemeDefinition
    reason: str


class ThemeAttemptPlanner:
    """Try the highest-ranked production-safe themes in their original order."""

    def __init__(
        self,
        ranked_themes: Sequence[CarouselThemeDefinition],
        *,
        attempt_limit: int,
        ranker: Callable[
            [Sequence[CarouselThemeDefinition]], Sequence[CarouselThemeDefinition]
        ]
        | None = None,
    ) -> None:
        if attempt_limit < 1:
            raise ValueError("attempt_limit must be positive")
        unique: list[CarouselThemeDefinition] = []
        seen_ids: set[str] = set()
        for theme in ranked_themes:
            if theme.evidence_mode is not ThemeEvidenceMode.METADATA:
                continue
            if theme.id in seen_ids:
                continue
            seen_ids.add(theme.id)
            unique.append(theme)
        self._ranked = tuple(unique)
        self._attempt_limit = attempt_limit
        self._ranker = ranker
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
        if self._ranker is not None:
            remaining = list(self._ranker(remaining))
        return remaining[0] if remaining else None

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
        preview = ThemeAttemptPlanner(
            self._ranked,
            attempt_limit=self._attempt_limit,
            ranker=self._ranker,
        )
        planned: list[CarouselThemeDefinition] = []
        while theme := preview.next_theme():
            planned.append(theme)
        return tuple(planned)

    def remaining_preview(self) -> tuple[CarouselThemeDefinition, ...]:
        """Return the current dynamic remainder without mutating run state."""
        preview = ThemeAttemptPlanner(
            self._ranked,
            attempt_limit=self._attempt_limit,
            ranker=self._ranker,
        )
        preview._attempted = list(self._attempted)
        planned: list[CarouselThemeDefinition] = []
        while theme := preview.next_theme():
            planned.append(theme)
        return tuple(planned)
