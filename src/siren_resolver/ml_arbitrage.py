from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import lightgbm as lgb
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .duckdb_repository import SirenCandidate
from .text_utils import normalize_company_name

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MatchFeatures:
    score_jaro_winkler: float
    score_levenshtein: float
    cosine_similarity_bi_encoder: float
    same_department: bool
    is_headquarters: bool
    naf_semantic_score: float

    def to_vector(self) -> list[float]:
        return [
            self.score_jaro_winkler,
            self.score_levenshtein,
            self.cosine_similarity_bi_encoder,
            1.0 if self.same_department else 0.0,
            1.0 if self.is_headquarters else 0.0,
            self.naf_semantic_score,
        ]


class LightGBMArbitrator:
    def __init__(self, model_path: Path | str | None = None):
        self._model_path = Path(model_path) if model_path else None
        self._model: lgb.Booster | None = None
        self._vectorizer: Optional[Pipeline] = None
        if self._model_path is not None:
            self._load_model()

    def _load_model(self) -> None:
        self._model = lgb.Booster(model_file=str(self._model_path))
        self._vectorizer = Pipeline(
            [
                ("tfidf", TfidfVectorizer(max_features=512, stop_words="french")),
                ("scale", StandardScaler(with_mean=False)),
            ]
        )

    def score(self, features: MatchFeatures, naf_label: str | None) -> float:
        if self._model is None:
            raise RuntimeError("LightGBM model not loaded")

        x_numeric = np.array([features.to_vector()])
        naf_score = self._naf_score(naf_label)
        x = np.hstack([x_numeric, np.array([[naf_score]])])
        probability = self._model.predict(x)
        if isinstance(probability, np.ndarray):
            return float(probability[0])
        return float(probability)

    def _naf_score(self, naf_label: str | None) -> float:
        if naf_label is None:
            return 0.0
        if self._vectorizer is None:
            return 0.0
        transformed = self._vectorizer.transform([naf_label])
        return float(np.asarray(transformed.sum(axis=1)).squeeze())
