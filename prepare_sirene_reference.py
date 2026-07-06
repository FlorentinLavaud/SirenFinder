from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path
from typing import Iterable

import duckdb

logger = logging.getLogger(__name__)


def configure_httpfs(
    conn: duckdb.DuckDBPyConnection,
    endpoint: str,
    access_key: str,
    secret_key: str,
    session_token: str | None = None,
    url_style: str = "path",
) -> None:
    conn.execute("INSTALL httpfs; LOAD httpfs;")
    conn.execute(f"SET s3_endpoint='{endpoint}';")
    conn.execute(f"SET s3_access_key_id='{access_key}';")
    conn.execute(f"SET s3_secret_access_key='{secret_key}';")
    if session_token:
        conn.execute(f"SET s3_session_token='{session_token}';")
    conn.execute(f"SET s3_url_style='{url_style}';")
    conn.execute("SET s3_region='us-east-1';")


def discover_columns(conn: duckdb.DuckDBPyConnection, input_path: str) -> list[str]:
    result = conn.execute(f"SELECT * FROM read_parquet('{input_path}') LIMIT 0").df()
    return [str(c) for c in result.columns]


def choose_column(columns: Iterable[str], candidates: list[str]) -> str | None:
    lower_to_original = {c.lower(): c for c in columns}
    for candidate in candidates:
        if candidate.lower() in lower_to_original:
            return lower_to_original[candidate.lower()]
    return None


def build_clean_sirene_sql(input_path: str, columns: list[str]) -> str:
    siren = choose_column(columns, ["siren"])
    siret = choose_column(columns, ["siret"])
    active = choose_column(
        columns,
        [
            "etat_administratif_etablissement",
            "etat_administratif_siret",
            "etat_administratif",
            "etat_administratif_unite_legale",
        ],
    )
    code_postal = choose_column(columns, ["code_postal", "postal_code"])
    code_departement = choose_column(columns, ["code_departement", "department_code"])
    commune = choose_column(columns, ["libelle_commune", "nom_commune", "commune"])
    raison_sociale = choose_column(
        columns,
        [
            "denomination_unite_legale",
            "raison_sociale",
            "nom_raison_sociale",
            "denomination_raison_sociale",
            "denomination",
        ],
    )
    enseigne = choose_column(columns, ["enseigne", "enseigne_1", "enseigne_2", "nom_commercial"])
    naf_ape = choose_column(columns, ["activite_principale", "code_naf", "naf_ape"])
    naf_label = choose_column(columns, ["libelle_activite_principale", "libelle_naf"])
    est_siege = choose_column(columns, ["est_siege", "siege", "siege_etablissement"])

    if not siren or not siret or not raison_sociale:
        raise ValueError(
            "Impossible de trouver les colonnes indispensables dans le StockEtablissement. "
            f"Colonnes détectées : {columns}"
        )

    active_predicate = "1=1"
    if active:
        active_predicate = (
            f"upper(coalesce({active}, '')) = 'A' OR upper(coalesce({active}, '')) = 'ACTIF'"
        )

    department_expr = (
        f"coalesce(trim({code_departement}), "
        f"CASE WHEN length(trim({code_postal})) >= 3 AND substr(trim({code_postal}), 1, 2) IN ('97', '98') "
        f"THEN substr(trim({code_postal}), 1, 3) ELSE substr(trim({code_postal}), 1, 2) END)"
        if code_departement and code_postal
        else f"trim({code_departement})" if code_departement
        else f"trim({code_postal})"
    )

    select_expressions = [
        f"trim({siren}) AS siren",
        f"trim({siret}) AS siret",
        f"trim({raison_sociale}) AS raison_sociale",
    ]
    if enseigne:
        select_expressions.append(f"nullif(trim({enseigne}), '') AS enseigne")
    else:
        select_expressions.append("NULL AS enseigne")
    select_expressions.append(f"nullif(trim({naf_ape}), '') AS naf_ape" if naf_ape else "NULL AS naf_ape")
    select_expressions.append(f"nullif(trim({naf_label}), '') AS naf_label" if naf_label else "NULL AS naf_label")
    select_expressions.append(f"nullif(trim({code_postal}), '') AS code_postal" if code_postal else "NULL AS code_postal")
    select_expressions.append(f"nullif(trim({commune}), '') AS commune" if commune else "NULL AS commune")
    select_expressions.append(f"NULLIF({department_expr}, '') AS code_departement")
    if est_siege:
        select_expressions.append(f"({est_siege} = TRUE OR upper(coalesce({est_siege}, '')) IN ('OUI', '1', 'TRUE')) AS is_headquarters")
    else:
        select_expressions.append("FALSE AS is_headquarters")

    select_clause = ",\n        ".join(select_expressions)
    sql = f"""
    SELECT
        {select_clause}
    FROM read_parquet('{input_path}')
    WHERE {active_predicate}
      AND {siren} IS NOT NULL
      AND {siret} IS NOT NULL
    """
    return sql


def write_cleaned_dataset(
    input_path: str,
    output_path: str,
    s3_endpoint: str | None,
    access_key: str | None,
    secret_key: str | None,
    session_token: str | None,
    url_style: str = "path",
) -> None:
    conn = duckdb.connect(database=":memory:")
    if input_path.startswith("s3://") or output_path.startswith("s3://"):
        if not s3_endpoint or not access_key or not secret_key:
            raise ValueError("S3 endpoint et identifiants AWS sont requis pour un chemin S3.")
        configure_httpfs(conn, s3_endpoint, access_key, secret_key, session_token, url_style)

    columns = discover_columns(conn, input_path)
    logger.info("Colonnes détectées dans le StockEtablissement : %s", columns)

    sql = build_clean_sirene_sql(input_path, columns)
    logger.info("Requête de nettoyage construite.")
    copy_sql = (
        f"COPY ({sql}) TO '{output_path}' (FORMAT PARQUET, PARTITION_BY (code_departement))"
        if output_path.startswith("s3://")
        else f"COPY ({sql}) TO '{output_path}' (FORMAT PARQUET, PARTITION_BY (code_departement))"
    )
    logger.info("Export partitionné vers %s", output_path)
    conn.execute(copy_sql)
    logger.info("Export terminé.")


def test_duckdb_query(output_path: str, department: str, company_name: str) -> None:
    conn = duckdb.connect(database=":memory:")
    if output_path.startswith("s3://"):
        endpoint = os.environ.get("AWS_S3_ENDPOINT", "minio.lab.sspcloud.fr")
        access_key = os.environ.get("AWS_ACCESS_KEY_ID")
        secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
        session_token = os.environ.get("AWS_SESSION_TOKEN")
        configure_httpfs(conn, endpoint, access_key or "", secret_key or "", session_token)

    table_path = f"{output_path}/*.parquet"
    sql = (
        f"SELECT siren, siret, raison_sociale, enseigne, code_postal, commune "
        f"FROM read_parquet('{table_path}') "
        f"WHERE code_departement = '{department}' "
        f"AND lower(raison_sociale) LIKE lower('%{company_name}%') "
        f"LIMIT 20"
    )
    start = time.monotonic()
    result = conn.execute(sql).fetchdf()
    elapsed_ms = (time.monotonic() - start) * 1000
    logger.info("Requête test exécutée en %.1f ms, %d résultats.", elapsed_ms, len(result))
    print(result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Nettoie un StockEtablissement SIRENE et exporte un Parquet partitionné par département.")
    parser.add_argument("--input", required=True, help="Chemin local ou S3 du StockEtablissement (Parquet).")
    parser.add_argument("--output", required=True, help="Chemin local ou S3 de sortie pour le dataset nettoyé Partitionné par code_departement.")
    parser.add_argument("--s3-endpoint", default=os.environ.get("AWS_S3_ENDPOINT"), help="Endpoint MinIO/SSP Cloud.")
    parser.add_argument("--s3-access-key-id", default=os.environ.get("AWS_ACCESS_KEY_ID"), help="Identifiant AWS/MinIO.")
    parser.add_argument("--s3-secret-access-key", default=os.environ.get("AWS_SECRET_ACCESS_KEY"), help="Secret AWS/MinIO.")
    parser.add_argument("--s3-session-token", default=os.environ.get("AWS_SESSION_TOKEN"), help="Session token AWS (optionnel).")
    parser.add_argument("--s3-url-style", default=os.environ.get("AWS_S3_URL_STYLE", "path"), choices=["path", "virtual"], help="Style d'URL S3 pour MinIO.")
    parser.add_argument("--test-department", default="75", help="Département pour la requête de validation.")
    parser.add_argument("--test-name", default="Paris", help="Nom d'entreprise partiel pour la requête de validation.")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = parse_args()

    write_cleaned_dataset(
        input_path=args.input,
        output_path=args.output,
        s3_endpoint=args.s3_endpoint,
        access_key=args.s3_access_key_id,
        secret_key=args.s3_secret_access_key,
        session_token=args.s3_session_token,
        url_style=args.s3_url_style,
    )

    logger.info("Validation sur le dataset nettoyé...")
    test_duckdb_query(args.output, args.test_department, args.test_name)


if __name__ == "__main__":
    main()
