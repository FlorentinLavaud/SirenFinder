from __future__ import annotations

import logging
from dataclasses import dataclass

import duckdb

from .config import S3Config
from .models import Address

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SirenCandidate:
    siren: str
    official_name: str
    enseigne: str | None = None
    sigle: str | None = None
    naf_ape: str | None = None
    zip_code: str | None = None
    city: str | None = None
    department: str | None = None
    is_headquarters: bool = False


class DuckDBSirenRepository:

    def __init__(
        self,
        parquet_root: str,
        s3_config: S3Config | None = None,
    ):
        self._parquet_root = parquet_root.rstrip("/").replace("\\", "/")
        self._conn = duckdb.connect(":memory:")

        if self._parquet_root.startswith("s3://"):
            self._configure_s3(s3_config or S3Config())

    def _configure_s3(self, config: S3Config) -> None:
        if not config.is_configured:
            raise ValueError(
                "Lecture S3 demandée mais credentials AWS/MinIO absents."
            )

        self._conn.execute("INSTALL httpfs; LOAD httpfs;")

        settings = {
            "s3_endpoint": config.endpoint,
            "s3_access_key_id": config.access_key_id,
            "s3_secret_access_key": config.secret_access_key,
            "s3_url_style": config.url_style,
            "s3_region": config.region,
        }

        if config.session_token:
            settings["s3_session_token"] = config.session_token

        for key, value in settings.items():
            self._conn.execute(
                f"SET {key}=?",
                [value],
            )

        logger.info(
            "Référentiel SIRENE chargé depuis %s",
            self._parquet_root,
        )

    def _table_glob(self, department: str | None) -> str:
        if department:
            return (
                f"{self._parquet_root}/"
                f"code_departement={department}/*.parquet"
            )

        return f"{self._parquet_root}/**/*.parquet"

    def _candidate_from_row(self, row) -> SirenCandidate:
        return SirenCandidate(
            siren=str(row[0]),
            official_name=str(row[1] or ""),
            enseigne=row[2],
            sigle=row[3],
            naf_ape=row[4],
            zip_code=row[5],
            city=row[6],
            department=row[7],
            is_headquarters=bool(row[8]),
        )

    def lookup_candidates(
        self,
        department: str | None,
        raw_name: str,
        city: str | None = None,
        zip_code: str | None = None,
        limit: int = 64,
    ) -> list[SirenCandidate]:

        glob = self._table_glob(department)
        glob = f"s3://flavaud/SirenFinder/db_sirene_api/code_departement={department}/*.parquet"

        conditions = []
        params: list = []

        if zip_code and city:
            conditions.append(
                "(CAST(code_postal AS VARCHAR) = ? AND lower(commune) = lower(?))"
            )
            params += [zip_code, city]
        if zip_code:
            conditions.append("(CAST(code_postal AS VARCHAR) = ?)")
            params += [zip_code]
        if department:
            conditions.append("(code_departement = ?)")
            params += [department]

        if not conditions:
            return []

        # Priorité 0 : sigle exact (quasi déterministe pour les EPCI,
        # associations, syndicats mixtes, etc. -- ex. "CCPLM" pour
        # "COMMUNAUTE DE COMMUNES PIEGE-LAURAGAIS-MALEPERE"). Testé avant la
        # géographie car un sigle exact est un signal plus fort qu'un simple
        # rapprochement de code postal.
        priority_case = "CASE "
        case_params: list = []
        priority_case += "WHEN lower(coalesce(sigle, '')) = ? AND lower(coalesce(sigle, '')) != '' THEN 0 "
        case_params += [raw_name.lower()]
        if zip_code and city:
            priority_case += (
                "WHEN CAST(code_postal AS VARCHAR) = ? AND lower(commune) = lower(?) THEN 1 "
            )
            case_params += [zip_code, city]
        if zip_code:
            priority_case += "WHEN CAST(code_postal AS VARCHAR) = ? THEN 2 "
            case_params += [zip_code]
        if department:
            priority_case += "WHEN code_departement = ? THEN 3 "
            case_params += [department]
        priority_case += "ELSE 4 END"

        where_clause = " OR ".join(conditions)
        name_lower = raw_name.lower()

        sql = f"""
            SELECT
                siren, raison_sociale, enseigne, sigle, naf_ape,
                code_postal, commune, code_departement, is_headquarters,
                {priority_case} AS priority,
                GREATEST(
                    jaro_winkler_similarity(lower(raison_sociale), ?),
                    jaro_winkler_similarity(lower(coalesce(enseigne, '')), ?),
                    jaro_winkler_similarity(lower(coalesce(sigle, '')), ?)
                ) AS name_sim
            FROM read_parquet('{glob}', hive_partitioning=true)
            WHERE {where_clause}
            ORDER BY priority ASC, name_sim DESC, is_headquarters DESC
            LIMIT ?
        """

        try:
            rows = self._conn.execute(
                sql,
                case_params + [name_lower, name_lower, name_lower] + params + [limit],
            ).fetchall()
        except duckdb.IOException as exc:
            logger.warning("Erreur lecture SIRENE %s : %s", glob, exc)
            return []

        return [self._candidate_from_row(row) for row in rows]

    def candidate_scope(
        self,
        address: Address,
        raw_name: str,
        limit: int = 64,
    ) -> list[SirenCandidate]:

        return self.lookup_candidates(
            department=address.department_hint,
            raw_name=raw_name,
            city=address.city,
            zip_code=address.zip_code,
            limit=limit,
        )