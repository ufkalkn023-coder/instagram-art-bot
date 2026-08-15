from abc import ABC, abstractmethod
from typing import Optional, List, Set
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
    def fetch_candidates(self, limit: int = 20, query: str = None) -> List[NormalizedArtwork]:
        """
        Fetches candidates from the museum API.
        Does NOT apply quality filtering or duplicate filtering; 
        only normalizes the raw responses into NormalizedArtwork.
        """
        pass
