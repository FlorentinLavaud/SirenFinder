from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from .cache import ParquetSirenCache
from .config import ResolverConfig
from .models import Address, CompanyQuery
from .text_utils import parse_address, _normalize_address_component
from .resolver import SirenResolver

logger = logging.getLogger(__name__)

AddressBuilder = Callable[[dict], Address]


@dataclass(frozen=True)
class EntityColumns:
    """Décrit les noms de colonnes propres à une entité (CONTRACTING,
    AWARDED, ou toute autre source comme JOCAS), pour réutiliser le même
    pipeline sans dupliquer de code.

    Deux façons de fournir l'adresse :
      - `address_col` : une colonne unique contenant du JSON/dict brut
        (format des fichiers d'origine) ;
      - `address_builder` : une fonction row_dict -> Address, pour les
        sources avec des colonnes d'adresse séparées (ex: JOCAS avec
        location_label / location_zipcode / location_departement).
    Exactement l'un des deux doit être fourni.
    """
    name_col: str
    siren_col: str
    role: str
    address_col: Optional[str] = None
    address_builder: Optional[AddressBuilder] = None
    zip_code_col: Optional[str] = None
    city_col: Optional[str] = None
    department_col: Optional[str] = None

    def __post_init__(self):
        if bool(self.address_col) == bool(self.address_builder):
            raise ValueError("Fournir exactement un de address_col ou address_builder.")

    def build_address(self, row_dict: dict) -> Address:
        if self.address_builder:
            return self.address_builder(row_dict)

        def lookup_component(col_name: Optional[str], *fallbacks: str) -> str | None:
            if col_name and col_name in row_dict:
                return row_dict.get(col_name)
            for fallback in fallbacks:
                if fallback in row_dict:
                    return row_dict.get(fallback)
            return None

        raw_address = row_dict.get(self.address_col)
        address = parse_address(raw_address)

        zip_code = _normalize_address_component(
            lookup_component(self.zip_code_col, "location_zipcode", "LOCATION_ZIPCODE")
        )
        city = _normalize_address_component(
            lookup_component(self.city_col, "location_label", "location_city", "LOCATION_LABEL", "LOCATION_CITY")
        )
        department_hint = _normalize_address_component(
            lookup_component(self.department_col, "location_departement", "location_department", "LOCATION_DEPARTEMENT", "LOCATION_DEPARTMENT")
        )

        street = address.street
        if street and not address.city and not address.zip_code and (zip_code or city or department_hint):
            if city is None:
                city = street
            street = None

        if zip_code or city or department_hint or address.street or address.city or address.zip_code:
            return Address(
                street=street,
                zip_code=address.zip_code or zip_code,
                city=address.city or city,
                department_hint=address.department_hint or department_hint,
            )

        return address


CONTRACTING_COLUMNS = EntityColumns(
    name_col="CONTRACTING_STATED_NAME", siren_col="CONTRACTING_SIREN", role="CONTRACTING",
    address_col="CONTRACTING_ADDRESS",
)
AWARDED_COLUMNS = EntityColumns(
    name_col="AWARDED_STATED_NAME", siren_col="AWARDED_SIREN", role="AWARDED",
    address_col="AWARDED_ADDRESS",
)


class SirenResolutionPipeline:
    """Orchestration bout-en-bout pour une entité donnée (contracting ou
    awarded) : charge le fichier "missing", résout via cache + providers,
    met à jour le stock, sauvegarde. Remplace les deux blocs procéduraux
    dupliqués du script d'origine par un seul chemin de code paramétré.
    """

    def __init__(
        self,
        config: ResolverConfig,
        resolver: SirenResolver,
        cache: ParquetSirenCache,
        columns: EntityColumns,
    ):
        self._config = config
        self._resolver = resolver
        self._cache = cache
        self._columns = columns

    def run(self, missing_input_path: Path, resolved_output_path: Path) -> pd.DataFrame:
        df = pd.read_parquet(missing_input_path)
        logger.info("[%s] %d lignes à résoudre.", self._columns.role, len(df))

        queries = [self._to_query(row) for row in df.itertuples(index=False)]

        # Déduplication : une même entreprise peut apparaître des centaines
        # de fois (ex: JOCAS, un attributaire récurrent). Sans ça, on
        # rappellerait les providers autant de fois qu'il y a de lignes,
        # puisque le cache n'est mis à jour qu'en fin de run (cf upsert_many
        # plus bas) et ne voit donc pas les doublons internes à ce batch.
        unique_by_key: dict[tuple[str, str], CompanyQuery] = {q.cache_key: q for q in queries}
        logger.info("[%s] %d entités distinctes à résoudre (sur %d lignes).",
                    self._columns.role, len(unique_by_key), len(queries))

        results_by_key: dict[tuple[str, str], str | None] = {}
        newly_resolved: list[tuple[CompanyQuery, str]] = []
        n_resolved = 0
        for key, query in unique_by_key.items():
            result = self._resolver.resolve(query)
            results_by_key[key] = result.siren
            if result.is_resolved:
                n_resolved += 1
                if result.confidence.value != "cache":
                    newly_resolved.append((query, result.siren))
            else:
                logger.debug("Non résolu : %s | %s", query.raw_name, query.address)

        resolved_sirens = [results_by_key[q.cache_key] or "nan" for q in queries]

        df = df.copy()
        df[self._columns.siren_col] = resolved_sirens
        df = df.astype(str)
        df.to_parquet(resolved_output_path, index=False)
        logger.info(
            "[%s] Terminé : %d/%d entités distinctes résolues (%d lignes mises à jour). Écrit dans %s",
            self._columns.role, n_resolved, len(unique_by_key), len(queries), resolved_output_path,
        )

        self._cache.upsert_many(newly_resolved)
        self._cache.prune_random()
        self._cache.save()
        return df

    def _to_query(self, row) -> CompanyQuery:
        row_dict = row._asdict()
        raw_name = row_dict[self._columns.name_col]
        address = self._columns.build_address(row_dict)
        return CompanyQuery(raw_name=str(raw_name), address=address, role=self._columns.role)
