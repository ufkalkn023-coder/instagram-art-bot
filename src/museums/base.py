from abc import ABC, abstractmethod
import random
from typing import List

from src.models import NormalizedArtwork
from src.source_health import normalize_source_failure_category

class MuseumAdapter(ABC):
    """
    Base class for all museum API adapters.
    Each adapter must implement fetch_candidates() which returns
    a list of NormalizedArtwork objects.
    """

    source_failure_category: str | None = None

    def _clear_source_failure(self) -> None:
        self.source_failure_category = None

    def _record_source_failure(self, category: object) -> None:
        self.source_failure_category = normalize_source_failure_category(category)

    @property
    @abstractmethod
    def source_id(self) -> str:
        """The internal string identifier for this museum (e.g. 'aic', 'met')"""
        pass
        
    @abstractmethod
    def fetch_candidates(
        self,
        limit: int = 20,
        query: str = None,
        rng: random.Random | None = None,
    ) -> List[NormalizedArtwork]:
        """
        Fetches candidates from the museum API.
        Does NOT apply quality filtering or duplicate filtering; 
        only normalizes the raw responses into NormalizedArtwork. ``rng`` is
        optional so direct adapter use retains normal random exploration.
        """
        pass
