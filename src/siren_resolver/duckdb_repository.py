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
            naf_ape=row[3],
            zip_code=row[4],
            city=row[5],
            department=row[6],
            is_headquarters=bool(row[7]),
        )

    def lookup_candidates(
        self,
        department: str | None,
        city: str | None = None,
        zip_code: str | None = None,
        limit: int = 64,
    ) -> list[SirenCandidate]:

        glob = self._table_glob(department)

        base = f"""
            SELECT
                siren,
                raison_sociale,
                enseigne,
                naf_ape,
                code_postal,
                commune,
                code_departement,
                is_headquarters
            FROM read_parquet(
                '{glob}',
                hive_partitioning=true
            )
        """

        scopes = []

        if zip_code and city:
            scopes.append(
                (
                    """
                    WHERE code_postal = ?
                    AND lower(commune) = lower(?)
                    """,
                    [zip_code, city],
                )
            )

        if zip_code:
            scopes.append(
                (
                    """
                    WHERE code_postal = ?
                    """,
                    [zip_code],
                )
            )

        if department:
            scopes.append(
                (
                    """
                    WHERE code_departement = ?
                    """,
                    [department],
                )
            )

        for where, params in scopes:

            sql = f"""
                {base}
                {where}
                ORDER BY is_headquarters DESC
                LIMIT ?
            """

            try:
                rows = self._conn.execute(
                    sql,
                    params + [limit],
                ).fetchall()

            except duckdb.IOException as exc:
                logger.warning(
                    "Erreur lecture SIRENE %s : %s",
                    glob,
                    exc,
                )
                continue

            if rows:
                return [
                    self._candidate_from_row(row)
                    for row in rows
                ]

        return []

    def candidate_scope(
        self,
        address: Address,
        limit: int = 64,
    ) -> list[SirenCandidate]:

        return self.lookup_candidates(
            department=address.department_hint,
            city=address.city,
            zip_code=address.zip_code,
            limit=limit,
        )