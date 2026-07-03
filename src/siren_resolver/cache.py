from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .models import CompanyQuery
from .text_utils import normalize_address_repr

logger = logging.getLogger(__name__)


class ParquetSirenCache:
    """Stock persistant de correspondances (nom, adresse) -> SIREN.

    Remplace le dict global + fonctions libres du script d'origine par un
    objet à responsabilité unique : charger, interroger, mettre à jour,
    purger et sauvegarder le cache. Le renouvellement aléatoire (purge
    d'1% des entrées pour forcer une re-vérification périodique) est
    conservé mais explicite et paramétrable.
    """

    def __init__(
        self,
        path: Path,
        name_col: str,
        address_col: str,
        siren_col: str,
        prune_probability: float = 0.01,
        prune_seed: int = 42,
    ):
        self._path = Path(path)
        self._name_col = name_col
        self._address_col = address_col
        self._siren_col = siren_col
        self._prune_probability = prune_probability
        self._prune_seed = prune_seed
        self._df = self._load()
        self._index = self._build_index(self._df)

    def _load(self) -> pd.DataFrame:
        if self._path.exists():
            df = pd.read_parquet(self._path)
            logger.info("Cache chargé : %s (%d entrées)", self._path, len(df))
            return df
        logger.warning("Aucun cache existant à %s, démarrage à vide.", self._path)
        return pd.DataFrame(columns=[self._name_col, self._address_col, self._siren_col])

    def _build_index(self, df: pd.DataFrame) -> dict[tuple[str, str], str]:
        index: dict[tuple[str, str], str] = {}
        for _, row in df.iterrows():
            siren = row[self._siren_col]
            if siren in (None, "nan", "") or pd.isna(siren):
                continue
            key = self._key(str(row[self._name_col]), row[self._address_col])
            index[key] = str(siren)
        return index

    @staticmethod
    def _key(name: str, raw_address) -> tuple[str, str]:
        return (name.strip().lower(), normalize_address_repr(raw_address))

    def lookup(self, query: CompanyQuery) -> str | None:
        key = self._key(query.raw_name, query.address.as_query_string())
        return self._index.get(key)

    def upsert_many(self, resolved: list[tuple[CompanyQuery, str]]) -> None:
        """Ajoute les nouvelles résolutions au cache en mémoire (pas encore
        persisté -> appeler `save()` explicitement, séparation lecture/écriture)."""
        rows = [
            {self._name_col: q.raw_name, self._address_col: q.address.as_query_string(), self._siren_col: siren}
            for q, siren in resolved
            if siren
        ]
        if not rows:
            return
        new_df = pd.DataFrame(rows)
        self._df = pd.concat([self._df, new_df], ignore_index=True).drop_duplicates()
        self._index = self._build_index(self._df)

    def prune_random(self) -> None:
        """Force une re-vérification périodique d'une fraction du stock,
        pour éviter de figer des erreurs indéfiniment."""
        if self._df.empty or self._prune_probability <= 0:
            return
        rng = np.random.default_rng(self._prune_seed)
        keep_mask = rng.random(len(self._df)) > self._prune_probability
        removed = (~keep_mask).sum()
        self._df = self._df[keep_mask].reset_index(drop=True)
        logger.info("Purge aléatoire du cache : %d entrées retirées.", removed)

    def save(self) -> None:
        self._df.astype(str).to_parquet(self._path, index=False)
        logger.info("Cache sauvegardé : %s (%d entrées)", self._path, len(self._df))

    def __len__(self) -> int:
        return len(self._df)
