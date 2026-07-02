from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class MatchConfidence(str, Enum):
    CACHE = "cache"          # trouvé dans le stock connu -> confiance max
    OFFICIAL_API = "official_api"
    GOOGLE_CSE = "google_cse"
    NONE = "none"


@dataclass(frozen=True)
class Address:
    """Adresse normalisée, indépendante du format source (UBL/JSON/colonnes séparées...)."""
    street: Optional[str] = None
    zip_code: Optional[str] = None
    city: Optional[str] = None
    department_hint: Optional[str] = None  # utile quand le CP est absent mais le département connu (ex: JOCAS)

    @property
    def department(self) -> Optional[str]:
        if self.zip_code and len(self.zip_code) >= 2:
            return self.zip_code[:2]
        return self.department_hint

    @property
    def is_empty(self) -> bool:
        return not any([self.street, self.zip_code, self.city])

    def as_query_string(self) -> str:
        return " ".join(p for p in [self.street, self.zip_code, self.city] if p)


@dataclass(frozen=True)
class CompanyQuery:
    """Une entité à résoudre : nom brut + adresse."""
    raw_name: str
    address: Address
    role: str = "UNKNOWN"  # "CONTRACTING" ou "AWARDED", pour traçabilité/logs

    @property
    def cache_key(self) -> tuple[str, str]:
        return (self.raw_name.strip().lower(), self.address.as_query_string().strip().lower())


@dataclass(frozen=True)
class ResolutionResult:
    query: CompanyQuery
    siren: Optional[str]
    confidence: MatchConfidence
    match_score: float = 0.0
    matched_name: Optional[str] = None

    @property
    def is_resolved(self) -> bool:
        return self.siren is not None
