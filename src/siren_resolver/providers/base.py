from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import CompanyQuery


class SirenProvider(ABC):
    """Interface commune (Strategy pattern) : chaque fournisseur sait
    résoudre une CompanyQuery en siren ou lever ProviderUnavailable /
    ProviderQuotaExceeded.
    """

    name: str

    @abstractmethod
    async def resolve(self, query: CompanyQuery) -> str | None:
        ...

    @property
    @abstractmethod
    def is_available(self) -> bool:
        """False si quota épuisé ou fournisseur désactivé par config."""
