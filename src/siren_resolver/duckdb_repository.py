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
    """Acces au referentiel SIRENE (local ou S3/MinIO) via DuckDB + Parquet.

    Le referentiel est partitionne par `code_departement`
    (cf. siren_fetching.fetch_stock_sirene.write_cleaned_dataset), sous la
    forme `{root}/code_departement=75/*.parquet`. Quand le departement de
    l'offre est connu, on cible directement ce sous-dossier : DuckDB ne liste
    et ne lit alors que les fichiers de ce departement (partition pruning),
    ce qui evite de parcourir l'integralite de la base a chaque resolution --
    determinant sur S3, ou lister/scanner toutes les partitions a un cout
    reseau non negligeable.
    """

    def __init__(self, parquet_root: str, s3_config: S3Config | None = None):
        self._parquet_root = str(parquet_root).replace("\\", "/").rstrip("/")
        self._is_s3 = self._parquet_root.startswith("s3://")
        self._conn = duckdb.connect(database=":memory:")
        if self._is_s3:
            self._configure_httpfs(s3_config or S3Config())

    def _configure_httpfs(self, s3_config: S3Config) -> None:
        if not s3_config.is_configured:
            raise ValueError(
                "Un referentiel SIREN sur S3 a ete demande "
                f"({self._parquet_root}) mais AWS_ACCESS_KEY_ID / "
                "AWS_SECRET_ACCESS_KEY ne sont pas renseignes dans "
                "l'environnement (variables Onyxia)."
            )
        self._conn.execute("INSTALL httpfs; LOAD httpfs;")
        self._conn.execute("SET s3_endpoint=?;", [s3_config.endpoint])
        self._conn.execute("SET s3_access_key_id=?;", [s3_config.access_key_id])
        self._conn.execute("SET s3_secret_access_key=?;", [s3_config.secret_access_key])
        if s3_config.session_token:
            self._conn.execute("SET s3_session_token=?;", [s3_config.session_token])
        self._conn.execute("SET s3_url_style=?;", [s3_config.url_style])
        self._conn.execute("SET s3_region=?;", [s3_config.region])
        logger.info(
            "DuckDB configure pour lire le referentiel SIREN sur S3 (%s, endpoint=%s).",
            self._parquet_root,
            s3_config.endpoint,
        )

    def _table_glob(self, department: str | None) -> str:
        """Chemin (local ou s3://) passe a read_parquet().

        Si le departement est connu, on cible directement la partition Hive
        correspondante ; sinon on retombe sur un scan de toute l'arborescence
        (hive_partitioning=true reconstitue alors `code_departement` a la
        volee, utile pour un filtrage par ville/code postal seul).
        """
        if department:
            return f"{self._parquet_root}/code_departement={department}/*.parquet"
        return f"{self._parquet_root}/**/*.parquet"

    def lookup_candidates(
        self,
        department: str | None = None,
        city: str | None = None,
        zip_code: str | None = None,
        limit: int = 20,
    ) -> list[SirenCandidate]:
        """
        Retourne un scope de candidats SIREN.

        Stratégie :
        - privilégie la proximité géographique ;
        - élargit progressivement si trop peu de candidats ;
        - le scoring nom est fait ensuite.
        """

        table_glob = self._table_glob(department)
        read_parquet_args = f"'{table_glob}', hive_partitioning=true"

        base_select = """
            SELECT
                siren,
                raison_sociale,
                enseigne,
                naf_ape,
                code_postal,
                commune,
                code_departement,
                is_headquarters
            FROM read_parquet({table})
        """.format(table=read_parquet_args)

        scopes = []

        # 1) Recherche locale forte
        if zip_code and city:
            scopes.append((
                """
                WHERE code_postal = ?
                AND lower(commune) = lower(?)
                """,
                [zip_code, city],
            ))

        # 2) Recherche CP seulement
        if zip_code:
            scopes.append((
                """
                WHERE code_postal = ?
                """,
                [zip_code],
            ))

        # 3) Département seulement
        if department:
            scopes.append((
                """
                WHERE code_departement = ?
                """,
                [department],
            ))

        rows = []

        for where_clause, params in scopes:
            sql = f"""
                {base_select}
                {where_clause}
                LIMIT ?
            """

            try:
                rows = self._conn.execute(
                    sql,
                    params + [limit],
                ).fetchall()

            except duckdb.IOException as exc:
                logger.warning(
                    "Lecture Parquet impossible %s : %s",
                    table_glob,
                    exc,
                )
                continue

            if len(rows) >= limit:
                break

        return [
            SirenCandidate(
                siren=str(row[0]),
                official_name=str(row[1] or ""),
                enseigne=str(row[2]) if row[2] else None,
                naf_ape=str(row[3]) if row[3] else None,
                zip_code=str(row[4]) if row[4] else None,
                city=str(row[5]) if row[5] else None,
                department=str(row[6]) if row[6] else None,
                is_headquarters=bool(row[7]) if row[7] is not None else False,
            )
            for row in rows
        ]

    def candidate_scope(self, address: Address, limit: int = 64) -> list[SirenCandidate]:
        return self.lookup_candidates(
            department=address.department,
            city=address.city,
            zip_code=address.zip_code,
            limit=limit,
        )
