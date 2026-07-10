"""Adaptateur JOCAS pour siren_resolver.

JOCAS expose directement les colonnes entreprise_nom / entreprise_siren
(déjà partiellement rempli) et une localisation en colonnes séparées
(location_label / location_zipcode / location_departement), sans le
format JSON du script d'origine ni la distinction contracting/awarded.

Ce script :
  1. interroge la vue DuckDB `jocas` pour extraire les couples
     (entreprise_nom, localisation) DISTINCTS dont le SIREN est manquant,
     en excluant les placeholders connus (cf `jocas_common.NAME_BLACKLIST`) ;
  2. les priorise par fréquence d'apparition (une entité récurrente coûte
     le même appel API qu'une entité vue une seule fois, mais rapporte
     bien plus une fois résolue) et applique un seuil optionnel `--min-offers` ;
  3. les résout via le pipeline (cache -> API officielle -> Google CSE) ;
  4. réintègre le SIREN résolu dans une vue jointe, prête à réexporter.

Usage :
    python run_jocas.py                       # run complet
    python run_jocas.py --dry-run             # affiche juste le volume à traiter
    python run_jocas.py --min-offers 3        # ne traite que les entités >= 3 offres
    python run_jocas.py --limit 500 --dry-run # test rapide sur un sous-ensemble
"""
from __future__ import annotations

import argparse
import logging
import time
import os 

import duckdb
import pandas as pd

from jocas_common import (
    DASK_ARTIFACT_COLUMNS,
    DATA_DIR,
    ENTREPRISE_COLUMNS,
    connect_jocas,
    sql_clean_numeric,
    sql_is_blacklisted_name,
    sql_siren_norm,
)
from siren_resolver import (
    CacheSirenProvider,
    LocalMlSirenConfig,
    LocalMlSirenProvider,
    ParquetSirenCache,
    ResolverConfig,
    SirenResolutionPipeline,
    SirenResolver,
)

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--min-offers", type=int, default=1,
        help="Ne traiter que les entités apparaissant au moins N fois dans les offres sans SIREN "
             "(priorisation ROI : une entité récurrente coûte le même appel API qu'une entité "
             "unique mais rapporte bien plus une fois résolue). Défaut : 1 (aucun filtre).",
    )
    parser.add_argument(
        "--limit", type=int, default=100,
        help="Limite le nombre d'entités traitées (utile pour un test rapide).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="N'appelle aucune API et n'écrit aucun fichier : affiche seulement le volume "
             "qui serait traité avec ces paramètres.",
    )
    return parser.parse_args()

def get_s3_duckdb_connection():
    # Création d'une connexion DuckDB en mémoire
    con = duckdb.connect(database=':memory:')
    
    # Chargement des extensions nécessaires pour S3
    con.execute("INSTALL httpfs;")
    con.execute("LOAD httpfs;")
    
    # Configuration des accès S3 via les variables d'environnement Onyxia
    con.execute(f"SET s3_access_key_id='{os.environ.get('AWS_ACCESS_KEY_ID')}';")
    con.execute(f"SET s3_secret_access_key='{os.environ.get('AWS_SECRET_ACCESS_KEY')}';")
    
    # Très important sur Onyxia : l'endpoint MinIO et le token de session
    if os.environ.get('AWS_SESSION_TOKEN'):
        con.execute(f"SET s3_session_token='{os.environ.get('AWS_SESSION_TOKEN')}';")
        
    # Endpoint MinIO du SSP Cloud (ex: minio.lab.sspcloud.fr)
    s3_endpoint = os.environ.get('AWS_S3_ENDPOINT', 'minio.lab.sspcloud.fr')
    con.execute(f"SET s3_endpoint='{s3_endpoint}';")
    con.execute("SET s3_url_style='path';")
    
    return con

def extract_missing_entities(
    conn: duckdb.DuckDBPyConnection, min_offers: int = 1, limit: int | None = None
) -> pd.DataFrame:
    """Extrait les entités distinctes (nom canonique + localisation) sans SIREN,
    hors placeholders connus, triées par score de priorité décroissant.

    Le score de priorité favorise :
    - les entités à fort volume d'offres (n_offres)
    - les entités présentes sur plusieurs localisations (probable chaîne/franchise,
      donc plus facilement résolvable via matching national)
    - les entités avec un code postal propre (5 chiffres)
    """
    query = f"""
        WITH base AS (
            SELECT
                {sql_canonical_name('entreprise_nom')} AS entreprise_nom_canon,
                entreprise_nom,
                location_label,
                {sql_clean_numeric('location_zipcode')} AS location_zipcode,
                {sql_clean_numeric('location_departement')} AS location_departement,
                entreprise_siren
            FROM jocas
            WHERE entreprise_nom IS NOT NULL
            AND trim(entreprise_nom) != ''
            AND length(trim(entreprise_nom)) >= 3
            AND NOT {sql_is_blacklisted_name('entreprise_nom')}
            AND {sql_clean_numeric('location_zipcode')} IS NOT NULL
        ),
        entity_stats AS (
            SELECT
                entreprise_nom_canon,
                count(DISTINCT location_zipcode) AS n_locations
            FROM base
            GROUP BY 1
        ),
        entities AS (
            SELECT
                b.entreprise_nom_canon,
                any_value(b.entreprise_nom) AS entreprise_nom_exemple,
                b.location_label,
                b.location_zipcode,
                b.location_departement,
                count(*) AS n_offres
            FROM base b
            GROUP BY 1, 3, 4, 5
            HAVING count(*) FILTER (
                WHERE {sql_siren_norm('b.entreprise_siren')} IS NOT NULL
            ) = 0
            AND count(*) >= {int(min_offers)}
        )
        SELECT
            e.entreprise_nom_canon,
            e.entreprise_nom_exemple,
            e.location_label,
            e.location_zipcode,
            e.location_departement,
            e.n_offres,
            s.n_locations,
            (
                e.n_offres
                * ln(1 + s.n_locations)
                * CASE WHEN length(e.location_zipcode) = 5 THEN 1.0 ELSE 0.5 END
            ) AS priority_score
        FROM entities e
        JOIN entity_stats s USING (entreprise_nom_canon)
        ORDER BY
            priority_score DESC,
            n_offres DESC,
            entreprise_nom_canon,
            location_zipcode
    """

    if limit is not None:
        query += f"\n        LIMIT {int(limit)}"

    df = conn.sql(query).df()
    logger.info(
        "%d entités distinctes à résoudre (min_offers=%d%s), couvrant %d offres.",
        len(df), min_offers, f", limit={limit}" if limit else "", int(df["n_offres"].sum()) if len(df) else 0,
    )
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
        #CacheSirenProvider(cache),
        LocalMlSirenProvider(
            LocalMlSirenConfig(
                parquet_root=config.pipeline.siren_reference_root,
                model_path=config.pipeline.local_ml_model_path,
                bi_encoder_model_name=config.pipeline.local_ml_bi_encoder_model_name,
                candidate_limit=config.pipeline.local_ml_candidate_limit,
                strict_threshold=config.pipeline.local_ml_strict_threshold,
                s3=config.pipeline.s3,
            )
        ),
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

    Les entités blacklistées (cf jocas_common.NAME_BLACKLIST) et celles
    sous le seuil --min-offers n'apparaissent pas dans `resolved_entities`
    et restent donc naturellement sans SIREN, sans traitement spécial ici.
    """
    conn.register("resolved_entities", resolved_df)
    conn.sql(f"""
        CREATE OR REPLACE VIEW jocas_enriched AS
        WITH entity_known_siren AS (
            SELECT
                entreprise_nom,
                location_label,
                {sql_clean_numeric('location_zipcode')} AS location_zipcode,
                {sql_clean_numeric('location_departement')} AS location_departement,
                max({sql_siren_norm('entreprise_siren')}) AS known_siren
            FROM jocas
            GROUP BY entreprise_nom, location_label, location_zipcode, location_departement
        ),
        jocas_norm AS (
            SELECT
                j.* EXCLUDE (entreprise_siren),
                {sql_clean_numeric('j.location_zipcode')} AS location_zipcode_norm,
                {sql_siren_norm('j.entreprise_siren')} AS entreprise_siren_norm
            FROM jocas j
        )
        SELECT
            j.* EXCLUDE (location_zipcode_norm, entreprise_siren_norm, {", ".join(DASK_ARTIFACT_COLUMNS)}),
            COALESCE(
                j.entreprise_siren_norm,
                k.known_siren,
                {sql_siren_norm('r.entreprise_siren')}
            ) AS entreprise_siren
        FROM jocas_norm j
        LEFT JOIN entity_known_siren k
          ON lower(trim(j.entreprise_nom)) = lower(trim(k.entreprise_nom))
         AND coalesce(j.location_zipcode_norm, '') = coalesce(k.location_zipcode, '')
         AND coalesce(j.location_label, '') = coalesce(k.location_label, '')
        LEFT JOIN resolved_entities r
          ON lower(trim(j.entreprise_nom)) = lower(trim(r.entreprise_nom))
         AND coalesce(j.location_zipcode_norm, '') = coalesce(r.location_zipcode, '')
         AND coalesce(j.location_label, '') = coalesce(r.location_label, '')
    """)
    logger.info("Vue 'jocas_enriched' créée avec les SIREN résolus.")


def main() -> None:
    logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")
    args = parse_args()

    start_time = time.time()
    conn = connect_jocas()

    missing_df = extract_missing_entities(conn, min_offers=args.min_offers, limit=args.limit)
    if missing_df.empty:
        logger.info("Rien à résoudre avec ces paramètres.")
        return

    if args.dry_run:
        logger.info(
            "[dry-run] %d entités seraient envoyées au pipeline (%d offres couvertes). "
            "Aucun appel API, aucun fichier écrit.",
            len(missing_df), int(missing_df["n_offres"].sum()),
        )
        return

    resolved_df = run_resolution(missing_df)
    join_back(conn, resolved_df)

    # --- CONFIGURATION S3 POUR DUCKDB ---
    print("Configuration de la connexion S3...")
    conn.execute("LOAD httpfs;")
    
    # Récupération automatique des variables d'environnement Onyxia
    conn.execute(f"SET s3_access_key_id='{os.environ.get('AWS_ACCESS_KEY_ID')}';")
    conn.execute(f"SET s3_secret_access_key='{os.environ.get('AWS_SECRET_ACCESS_KEY')}';")
    
    if os.environ.get('AWS_SESSION_TOKEN'):
        conn.execute(f"SET s3_session_token='{os.environ.get('AWS_SESSION_TOKEN')}';")
        
    s3_endpoint = os.environ.get('AWS_S3_ENDPOINT', 'minio.lab.sspcloud.fr')
    conn.execute(f"SET s3_endpoint='{s3_endpoint}';")
    conn.execute("SET s3_url_style='path';")
    
    # --- EXPORTATION DIRECTE VERS MINIO ---
    bucket_name = "flavaud" 
    s3_destination = f"s3://{bucket_name}/SirenFinder/jocas_enriched.parquet"
    
    print(f"Écriture du fichier enrichi directement sur S3: {s3_destination}")
    conn.execute(f"COPY jocas_enriched TO '{s3_destination}' (FORMAT PARQUET);")
    print("Export S3 terminé avec succès !")

if __name__ == "__main__":
    main()
