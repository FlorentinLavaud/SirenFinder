from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import CompanyQuery, ResolutionResult


class SirenProvider(ABC):
    """Interface commune (Strategy pattern) : chaque fournisseur sait
    résoudre une CompanyQuery en ResolutionResult, ou lever
    ProviderUnavailable / ProviderQuotaExceeded. Le resolver orchestre
    la liste ordonnée de fournisseurs sans connaître leurs détails.
    """

    name: str

    @abstractmethod
    def resolve(self, query: CompanyQuery) -> ResolutionResult:
        ...

    @property
    @abstractmethod
    def is_available(self) -> bool:
        """False si quota épuisé ou fournisseur désactivé par config."""
