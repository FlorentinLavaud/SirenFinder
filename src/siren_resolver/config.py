"""Configuration centralisée du resolver SIREN.

Toute constante "magique" (timeouts, quotas, chemins) vit ici plutôt que
dispersée dans le code métier. Les valeurs peuvent être surchargées par
variables d'environnement pour ne jamais committer de secrets.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _is_s3_uri(value: str) -> bool:
    return value.startswith("s3://")


@dataclass(frozen=True)
class S3Config:
    """Identifiants et endpoint S3/MinIO (injectés par Onyxia sur SSP Cloud).

    Onyxia expose ces variables automatiquement dans l'environnement du
    service ; on ne les committe donc jamais en dur.
    """

    endpoint: str = field(default_factory=lambda: os.getenv("AWS_S3_ENDPOINT", "minio.lab.sspcloud.fr"))
    access_key_id: str | None = field(default_factory=lambda: os.getenv("AWS_ACCESS_KEY_ID"))
    secret_access_key: str | None = field(default_factory=lambda: os.getenv("AWS_SECRET_ACCESS_KEY"))
    session_token: str | None = field(default_factory=lambda: os.getenv("AWS_SESSION_TOKEN"))
    url_style: str = field(default_factory=lambda: os.getenv("AWS_S3_URL_STYLE", "path"))
    region: str = field(default_factory=lambda: os.getenv("AWS_DEFAULT_REGION", "us-east-1"))

    @property
    def is_configured(self) -> bool:
        return bool(self.access_key_id and self.secret_access_key)


@dataclass(frozen=True)
class PipelineConfig:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("SIREN_DATA_DIR", ".")))
    # Chemin local OU URI S3 (ex: s3://mon-bucket/sirene/clean). Gardé en `str`
    # (et non `Path`) car `Path("s3://bucket/x")` collapse le "//" et casse l'URI.
    siren_reference_root: str = field(default_factory=lambda: os.getenv("SIREN_REFERENCE_ROOT", "."))
    s3: S3Config = field(default_factory=S3Config)
    local_ml_model_path: Path | None = field(default_factory=lambda: Path(os.getenv("LOCAL_ML_MODEL_PATH", "")) if os.getenv("LOCAL_ML_MODEL_PATH") else None)
    local_ml_bi_encoder_model_name: str = field(default_factory=lambda: os.getenv("LOCAL_ML_BI_ENCODER_MODEL_NAME", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"))
    local_ml_candidate_limit: int = int(os.getenv("LOCAL_ML_CANDIDATE_LIMIT", "64"))
    local_ml_strict_threshold: float = float(os.getenv("LOCAL_ML_STRICT_THRESHOLD", "0.85"))
    cache_prune_probability: float = 0.01  # renouvellement aléatoire du stock
    cache_prune_seed: int = 42
    log_level: str = os.getenv("SIREN_LOG_LEVEL", "INFO")

    @property
    def siren_reference_is_s3(self) -> bool:
        return _is_s3_uri(self.siren_reference_root)


@dataclass(frozen=True)
class ResolverConfig:
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
