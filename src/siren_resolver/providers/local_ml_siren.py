from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from rapidfuzz import fuzz
from sentence_transformers import SentenceTransformer

from ..config import S3Config
from ..duckdb_repository import DuckDBSirenRepository
from ..models import CompanyQuery
from ..ml_arbitrage import LightGBMArbitrator, MatchFeatures
from .base import SirenProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalMlSirenConfig:
    # Chemin local OU URI S3 (s3://bucket/...). Volontairement `str` : un
    # `Path` collapse le "//" d'une URI s3:// et la corrompt silencieusement.
    parquet_root: str
    model_path: Path | None = None
    bi_encoder_model_name: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    candidate_limit: int = 64
    strict_threshold: float = 0.85
    s3: S3Config | None = None


class LocalMlSirenProvider(SirenProvider):
    name = "local_ml_siren"

    def __init__(self, config: LocalMlSirenConfig):
        self._config = config
        self._repository = DuckDBSirenRepository(config.parquet_root, s3_config=config.s3)
        self._arbitrator = LightGBMArbitrator(config.model_path) if config.model_path else None
        self._encoder = SentenceTransformer(config.bi_encoder_model_name)

    @property
    def is_available(self) -> bool:
        return self._encoder is not None

    async def resolve(self, query: CompanyQuery) -> str | None:
        if query.address.is_empty:
            return None
        return await asyncio.to_thread(self._resolve_sync, query)

    def _resolve_sync(self, query: CompanyQuery) -> str | None:
        candidates = self._repository.candidate_scope(query.address, limit=self._config.candidate_limit)
        if not candidates:
            return None

        query_embedding = self._encoder.encode(query.raw_name, convert_to_numpy=True)
        best_siren: str | None = None
        best_score = 0.0

        for candidate in candidates:
            score_jw = fuzz.token_sort_ratio(query.raw_name, candidate.official_name) / 100.0
            score_lev = 1.0 - (fuzz.normalized_levenshtein(query.raw_name, candidate.official_name) / 100.0)
            candidate_emb = self._encoder.encode(candidate.official_name, convert_to_numpy=True)
            cosine = float(
                np.dot(query_embedding, candidate_emb) / (np.linalg.norm(query_embedding) * np.linalg.norm(candidate_emb) + 1e-12)
            )
            same_dept = bool(query.address.department and candidate.department == query.address.department)
            features = MatchFeatures(
                score_jaro_winkler=score_jw,
                score_levenshtein=score_lev,
                cosine_similarity_bi_encoder=cosine,
                same_department=same_dept,
                is_headquarters=candidate.is_headquarters,
                naf_semantic_score=0.0,
            )

            if self._arbitrator is None:
                score = float(np.mean([score_jw, score_lev, cosine]))
            else:
                score = self._arbitrator.score(features, candidate.naf_ape)

            if score > best_score and score >= self._config.strict_threshold:
                best_score = score
                best_siren = candidate.siren

        return best_siren
