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
class PipelineConfig:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("SIREN_DATA_DIR", ".")))
    siren_reference_root: Path = field(default_factory=lambda: Path(os.getenv("SIREN_REFERENCE_ROOT", ".")))
    local_ml_model_path: Path | None = field(default_factory=lambda: Path(os.getenv("LOCAL_ML_MODEL_PATH", "")) if os.getenv("LOCAL_ML_MODEL_PATH") else None)
    local_ml_bi_encoder_model_name: str = field(default_factory=lambda: os.getenv("LOCAL_ML_BI_ENCODER_MODEL_NAME", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"))
    local_ml_candidate_limit: int = int(os.getenv("LOCAL_ML_CANDIDATE_LIMIT", "64"))
    local_ml_strict_threshold: float = float(os.getenv("LOCAL_ML_STRICT_THRESHOLD", "0.85"))
    cache_prune_probability: float = 0.01  # renouvellement aléatoire du stock
    cache_prune_seed: int = 42
    log_level: str = os.getenv("SIREN_LOG_LEVEL", "INFO")


@dataclass(frozen=True)
class ResolverConfig:
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
