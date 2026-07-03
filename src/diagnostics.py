"""Diagnostics de couverture SIREN sur JOCAS.

Répond à la question "quel % d'offres mon sirénisateur peut-il viser ?" en
distinguant :
  - les offres sans nom d'entreprise du tout (hors de portée quoi qu'il arrive) ;
  - les offres avec un SIREN déjà présent (rien à faire) ;
  - les offres récupérables gratuitement par nom identique déjà résolu ailleurs ;
  - les offres provenant de placeholders connus (Pôle emploi, etc. -- cf
    jocas_common.NAME_BLACKLIST), volontairement hors scope ;
  - le reste : la cible réelle du pipeline de résolution.

Fournit aussi une courbe de rendement (couverture obtenue selon un seuil
minimal d'occurrences par entité) pour arbitrer le paramètre --min-offers
de run_jocas.py.

Usage :
    python diagnostics.py
"""
from __future__ import annotations

import logging

import duckdb
import pandas as pd

from jocas_common import connect_jocas, sql_is_blacklisted_name, sql_name_norm, sql_siren_norm

logger = logging.getLogger(__name__)

pd.set_option("display.max_columns", None)
pd.set_option("display.width", 160)


def _offres_sans_siren_recuperable_cte() -> str:
    """Fragment SQL partagé : offres avec entreprise_nom mais sans SIREN
    exploitable, ni sur la ligne, ni récupérable par nom identique ailleurs
    dans la base, en excluant les placeholders (blacklist)."""
    return f"""
        offres AS (
            SELECT
                entreprise_nom,
                {sql_siren_norm('entreprise_siren')} AS siren_norm
            FROM jocas
            WHERE entreprise_nom IS NOT NULL AND trim(entreprise_nom) != ''
        ),
        nom_siren_connu AS (
            SELECT {sql_name_norm('entreprise_nom')} AS nom_key, max(siren_norm) AS siren_connu_ailleurs
            FROM offres
            WHERE siren_norm IS NOT NULL
            GROUP BY {sql_name_norm('entreprise_nom')}
        ),
        sans_siren_brut AS (
            SELECT o.entreprise_nom
            FROM offres o
            LEFT JOIN nom_siren_connu k ON {sql_name_norm('o.entreprise_nom')} = k.nom_key
            WHERE o.siren_norm IS NULL AND k.siren_connu_ailleurs IS NULL
        ),
        sans_siren AS (
            SELECT entreprise_nom
            FROM sans_siren_brut
            WHERE NOT {sql_is_blacklisted_name('entreprise_nom')}
        )
    """


def couverture_globale(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Vue d'ensemble : où partent les offres, du nom manquant jusqu'à la
    cible réelle du pipeline de résolution."""
    query = f"""
        WITH {_offres_sans_siren_recuperable_cte()},
        base AS (
            SELECT count(*) AS total_offres FROM jocas
        )
        SELECT
            b.total_offres,
            (SELECT count(*) FROM jocas WHERE entreprise_nom IS NULL OR trim(entreprise_nom) = '')
                AS offres_sans_nom_entreprise,
            (SELECT count(*) FROM jocas WHERE entreprise_nom IS NOT NULL AND trim(entreprise_nom) != ''
                AND {sql_is_blacklisted_name('entreprise_nom')})
                AS offres_nom_placeholder_blackliste,
            (SELECT count(*) FROM sans_siren_brut) AS offres_sans_siren_hors_blacklist_inclus,
            (SELECT count(*) FROM sans_siren) AS offres_cible_pipeline,
            round(100.0 * (SELECT count(*) FROM sans_siren) / b.total_offres, 2)
                AS pct_cible_pipeline_sur_total,
            round(
                100.0 * (SELECT count(*) FROM sans_siren)
                / nullif(b.total_offres - (SELECT count(*) FROM jocas
                    WHERE entreprise_nom IS NULL OR trim(entreprise_nom) = ''), 0),
                2
            ) AS pct_cible_pipeline_sur_offres_avec_nom
        FROM base b
    """
    return conn.sql(query).df()


def top_entites_sans_siren(conn: duckdb.DuckDBPyConnection, n: int = 30, include_blacklisted: bool = False) -> pd.DataFrame:
    """Top entités (par nombre d'offres) sans SIREN récupérable.

    include_blacklisted=True permet de repasser sur le brut (avant filtre
    blacklist) pour repérer de nouveaux placeholders à ajouter à
    jocas_common.NAME_BLACKLIST -- toute entité en tête avec un volume
    disproportionné par rapport à une vraie entreprise est suspecte.
    """
    source_cte = "sans_siren_brut" if include_blacklisted else "sans_siren"
    query = f"""
        WITH {_offres_sans_siren_recuperable_cte()}
        SELECT entreprise_nom, count(*) AS n_offres
        FROM {source_cte}
        GROUP BY entreprise_nom
        ORDER BY n_offres DESC
        LIMIT {int(n)}
    """
    return conn.sql(query).df()


def courbe_rendement(
    conn: duckdb.DuckDBPyConnection, seuils: tuple[int, ...] = (1, 2, 3, 5, 10, 20, 50, 100)
) -> pd.DataFrame:
    """Pour chaque seuil minimal d'occurrences par entité : combien
    d'entités distinctes ça représente (= coût API) et quel % des offres
    orphelines c'est couvre (= gain). Sert à choisir --min-offers."""
    values_clause = ", ".join(f"({s})" for s in seuils)
    query = f"""
        WITH {_offres_sans_siren_recuperable_cte()},
        freq AS (
            SELECT {sql_name_norm('entreprise_nom')} AS nom_key, count(*) AS n_offres
            FROM sans_siren
            GROUP BY {sql_name_norm('entreprise_nom')}
        ),
        total AS (SELECT sum(n_offres) AS total_offres, count(*) AS total_entites FROM freq)
        SELECT
            s.seuil,
            (SELECT count(*) FROM freq WHERE n_offres >= s.seuil) AS entites_a_traiter,
            round(100.0 * (SELECT count(*) FROM freq WHERE n_offres >= s.seuil) / t.total_entites, 2)
                AS pct_entites_traitees,
            (SELECT sum(n_offres) FROM freq WHERE n_offres >= s.seuil) AS offres_couvertes,
            round(100.0 * (SELECT sum(n_offres) FROM freq WHERE n_offres >= s.seuil) / t.total_offres, 2)
                AS pct_offres_couvertes
        FROM total t, (VALUES {values_clause}) AS s(seuil)
        ORDER BY s.seuil
    """
    return conn.sql(query).df()


def run_all() -> None:
    logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")
    conn = connect_jocas()

    print("\n=== Couverture globale ===")
    print(couverture_globale(conn).to_string(index=False))

    print("\n=== Top 30 entités sans SIREN récupérable (hors blacklist) ===")
    print(top_entites_sans_siren(conn, n=30, include_blacklisted=False).to_string(index=False))

    print("\n=== Top 15 entités sans SIREN, blacklist NON appliquée ===")
    print("(à surveiller pour repérer de nouveaux placeholders à blacklister)")
    print(top_entites_sans_siren(conn, n=15, include_blacklisted=True).to_string(index=False))

    print("\n=== Courbe de rendement (seuil --min-offers) ===")
    print(courbe_rendement(conn).to_string(index=False))


if __name__ == "__main__":
    run_all()
