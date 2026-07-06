from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import duckdb

from .models import Address


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
    """Accès local à un référentiel SIRENE via DuckDB + Parquet.

    L'objectif est de faire du smart blocking en ne chargeant que les
    candidats pertinents géographiquement (département, code postal,
    ville) avant de calculer des scores de similarité.
    """

    def __init__(self, parquet_root: Path | str):
        self._parquet_root = Path(parquet_root)
        self._conn = duckdb.connect(database=":memory:")
        self._register_parquet_source()

    def _register_parquet_source(self) -> None:
        parquet_path = str(self._parquet_root).replace("\\", "/")
        self._conn.execute("INSTALL httpfs; LOAD httpfs;")
        self._conn.execute(
            f"CREATE VIEW IF NOT EXISTS siren_reference AS "
            f"SELECT * FROM read_parquet('{parquet_path}/*.parquet')"
        )

    def lookup_candidates(
        self,
        department: str | None = None,
        city: str | None = None,
        zip_code: str | None = None,
        limit: int = 64,
    ) -> list[SirenCandidate]:
        filters: list[str] = []
        params: list[str] = []

        if department:
            filters.append("departement = ?")
            params.append(department)
        if zip_code:
            filters.append("code_postal = ?")
            params.append(zip_code)
        if city:
            filters.append("lower(commune) = lower(?)")
            params.append(city)

        sql = "SELECT siren, raison_sociale, enseigne, naf_ape, code_postal, commune, departement, est_siege "
        sql += "FROM siren_reference "
        if filters:
            sql += "WHERE " + " AND ".join(filters) + " "
        sql += "LIMIT ?"
        params.append(str(limit))

        rows = self._conn.execute(sql, params).fetchall()
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
