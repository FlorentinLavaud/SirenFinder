"""Configuration centralisée du resolver SIREN.

Toute constante "magique" (timeouts, quotas, chemins) vit ici plutôt que
dispersée dans le code métier. Les valeurs peuvent être surchargées par
variables d'environnement pour ne jamais committer de secrets.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class GoogleCSEConfig:
    api_key: str = field(default_factory=lambda: os.getenv("GOOGLE_CSE_API_KEY", ""))
    cse_id: str = field(default_factory=lambda: os.getenv("GOOGLE_CSE_ID", ""))
    # Le quota gratuit standard de Google Programmable Search est de 100
    # requêtes/jour. Ne pas supposer 9990 sans avoir un quota payant vérifié.
    daily_quota: int = int(os.getenv("GOOGLE_CSE_DAILY_QUOTA", "100"))
    requests_per_second: float = 1.0
    enabled: bool = field(default_factory=lambda: bool(os.getenv("GOOGLE_CSE_API_KEY")))


@dataclass(frozen=True)
class RechercheEntreprisesConfig:
    base_url: str = "https://recherche-entreprises.api.gouv.fr/search"
    # API publique sans clé, mais on reste raisonnable pour ne pas se faire
    # jeter par un WAF / rate limit implicite.
    requests_per_second: float = 5.0
    timeout_seconds: float = 10.0
    max_retries: int = 4
    backoff_base_seconds: float = 0.5


@dataclass(frozen=True)
class PipelineConfig:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("SIREN_DATA_DIR", ".")))
    min_match_score: float = 0.55  # seuil de confiance pour accepter un SIREN
    cache_prune_probability: float = 0.01  # renouvellement aléatoire du stock
    cache_prune_seed: int = 42
    log_level: str = os.getenv("SIREN_LOG_LEVEL", "INFO")


@dataclass(frozen=True)
class ResolverConfig:
    google_cse: GoogleCSEConfig = field(default_factory=GoogleCSEConfig)
    recherche_entreprises: RechercheEntreprisesConfig = field(default_factory=RechercheEntreprisesConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
