from abc import ABC, abstractmethod
import random
from typing import List
from src.models import NormalizedArtwork

class MuseumAdapter(ABC):
    """
    Base class for all museum API adapters.
    Each adapter must implement fetch_candidates() which returns
    a list of NormalizedArtwork objects.
    """
    
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
