"""Adaptateur JOCAS pour siren_resolver.

JOCAS expose directement les colonnes entreprise_nom / entreprise_siren
(déjà partiellement rempli) et une localisation en colonnes séparées
(location_label / location_zipcode / location_departement), sans le
format JSON du script d'origine ni la distinction contracting/awarded.

Ce script :
  1. interroge la vue DuckDB `jocas` pour extraire les couples
     (entreprise_nom, localisation) DISTINCTS dont le SIREN est manquant ;
  2. les résout via le pipeline (cache -> API officielle -> Google CSE) ;
  3. réintègre le SIREN résolu dans une vue jointe, prête à réexporter.

Usage :
    python run_jocas.py
"""
from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import pandas as pd

from siren_resolver import (
    Address,
    EntityColumns,
    GoogleCSEProvider,
    ParquetSirenCache,
    RechercheEntreprisesProvider,
    ResolverConfig,
    SirenResolutionPipeline,
    SirenResolver,
)

logger = logging.getLogger(__name__)

DATA_DIR = Path("./jocas_siren_work")
DATA_DIR.mkdir(exist_ok=True)


def build_entreprise_address(row: dict) -> Address:
    """Construit une Address à partir des colonnes JOCAS.

    Pas de rue disponible. `location_label` est utilisé comme ville (à
    ajuster si c'est en réalité un libellé plus générique type "Paris
    (75)" -- vérifier un échantillon avant un run massif).
    """
    zip_code = str(row.get("location_zipcode")) if row.get("location_zipcode") else None
    departement = str(row.get("location_departement")) if row.get("location_departement") else None
    city = str(row.get("location_label")) if row.get("location_label") else None
    return Address(street=None, zip_code=zip_code, city=city, department_hint=departement)


ENTREPRISE_COLUMNS = EntityColumns(
    name_col="entreprise_nom",
    siren_col="entreprise_siren",
    role="ENTREPRISE",
    address_builder=build_entreprise_address,
)


def extract_missing_entities(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Extrait les entités distinctes (nom + localisation) sans SIREN.

    DISTINCT ici est essentiel : sans lui on repasserait potentiellement
    des millions de lignes de marchés dans le pipeline, alors qu'il n'y a
    probablement que quelques dizaines de milliers d'entreprises uniques.
    """
    query = """
        SELECT
            entreprise_nom,
            location_label,
            location_zipcode,
            location_departement
        FROM jocas
        WHERE entreprise_nom IS NOT NULL AND trim(entreprise_nom) != ''
        GROUP BY entreprise_nom, location_label, location_zipcode, location_departement
        -- on n'exclut une entité que si elle a un SIREN connu QUELQUE PART
        -- dans la table, pas seulement sur la ligne courante : ça évite de
        -- re-résoudre une entreprise déjà identifiée sur un autre marché.
        HAVING count(*) FILTER (
            WHERE entreprise_siren IS NOT NULL AND trim(entreprise_siren) != ''
        ) = 0
    """
    df = conn.sql(query).df()
    logger.info("%d entités distinctes sans SIREN à résoudre.", len(df))
    return df


def run_resolution(missing_df: pd.DataFrame) -> pd.DataFrame:
    missing_path = DATA_DIR / "jocas_missing_siren.parquet"
    resolved_path = DATA_DIR / "jocas_resolved_siren.parquet"
    missing_df.to_parquet(missing_path, index=False)

    config = ResolverConfig()
    cache = ParquetSirenCache(
        path=DATA_DIR / "stock_entreprise_sirens.parquet",
        name_col=ENTREPRISE_COLUMNS.name_col,
        address_col="ENTREPRISE_ADDRESS",  # label interne au cache, indépendant des colonnes source
        siren_col=ENTREPRISE_COLUMNS.siren_col,
        prune_probability=config.pipeline.cache_prune_probability,
        prune_seed=config.pipeline.cache_prune_seed,
    )
    providers = [
        RechercheEntreprisesProvider(config.recherche_entreprises, config.pipeline.min_match_score),
        GoogleCSEProvider(config.google_cse),
    ]
    resolver = SirenResolver(cache=cache, providers=providers)
    pipeline = SirenResolutionPipeline(config=config, resolver=resolver, cache=cache, columns=ENTREPRISE_COLUMNS)

    return pipeline.run(missing_input_path=missing_path, resolved_output_path=resolved_path)


def join_back(conn: duckdb.DuckDBPyConnection, resolved_df: pd.DataFrame) -> None:
    """Réintègre les SIREN dans la base JOCAS complète (toutes les lignes),
    en trois niveaux de priorité par ligne :
      1. le SIREN déjà présent sur la ligne elle-même ;
      2. un SIREN déjà connu ailleurs pour la même entité (autre marché) ;
      3. le SIREN nouvellement résolu par le pipeline.
    """
    conn.register("resolved_entities", resolved_df)
    conn.sql("""
        CREATE OR REPLACE VIEW jocas_enriched AS
        WITH entity_known_siren AS (
            SELECT
                entreprise_nom, location_label, location_zipcode, location_departement,
                max(entreprise_siren) FILTER (
                    WHERE entreprise_siren IS NOT NULL AND trim(entreprise_siren) != ''
                ) AS known_siren
            FROM jocas
            GROUP BY entreprise_nom, location_label, location_zipcode, location_departement
        )
        SELECT
            j.* EXCLUDE (entreprise_siren),
            COALESCE(
                NULLIF(j.entreprise_siren, ''),
                k.known_siren,
                r.entreprise_siren
            ) AS entreprise_siren
        FROM jocas j
        LEFT JOIN entity_known_siren k
          ON lower(trim(j.entreprise_nom)) = lower(trim(k.entreprise_nom))
         AND coalesce(j.location_zipcode, '') = coalesce(k.location_zipcode, '')
         AND coalesce(j.location_label, '') = coalesce(k.location_label, '')
        LEFT JOIN resolved_entities r
          ON lower(trim(j.entreprise_nom)) = lower(trim(r.entreprise_nom))
         AND coalesce(j.location_zipcode, '') = coalesce(r.location_zipcode, '')
         AND coalesce(j.location_label, '') = coalesce(r.location_label, '')
    """)
    logger.info("Vue 'jocas_enriched' créée avec les SIREN résolus.")


def main() -> None:
    logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")

    conn = duckdb.connect()
    conn.sql("""
        CREATE VIEW jocas AS
        SELECT *
        FROM read_parquet(
            's3://projet-jocas-prod/diffusion/JOCAS/annee=*/mois=*/*.parquet',
            hive_partitioning=True,
            union_by_name=True
        )
    """)

    missing_df = extract_missing_entities(conn)
    if missing_df.empty:
        logger.info("Rien à résoudre.")
        return

    resolved_df = run_resolution(missing_df)
    join_back(conn, resolved_df)

    # Exemple d'export -- à adapter (parquet local, réécriture S3, etc.)
    conn.sql("SELECT * FROM jocas_enriched").write_parquet(str(DATA_DIR / "jocas_enriched.parquet"))
    logger.info("Terminé. Résultat : %s", DATA_DIR / "jocas_enriched.parquet")


if __name__ == "__main__":
    main()
