"""Utilitaires partagés entre `run_jocas.py` (résolution) et `diagnostics.py`
(mesure de couverture), pour ne pas dupliquer la connexion S3, la
normalisation SQL des colonnes numériques-mais-cassées, et la blacklist des
noms d'entreprise placeholder.
"""
from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pandas as pd
import s3fs

from siren_resolver import Address, EntityColumns

DATA_DIR = Path("./jocas_siren_work")
DATA_DIR.mkdir(exist_ok=True)

JOCAS_S3_GLOB = "s3://projet-jocas-prod/diffusion/JOCAS/annee=*/mois=*/*.parquet"

# Colonnes techniques injectées par le pipeline d'écriture Dask lors de
# l'union de fichiers hétérogènes (ré-indexation à l'écriture) : visibles
# dans un échantillon `SELECT * FROM jocas LIMIT 1`. Elles n'ont aucune
# valeur métier et polluent tout export final si on ne les exclut pas.
DASK_ARTIFACT_COLUMNS = ["__index_level_0__", "__null_dask_index__"]

# Valeurs d'`entreprise_nom` qui ne désignent pas une entreprise réelle mais
# un intermédiaire/placeholder (offre anonymisée diffusée sous le nom du
# site ou de l'organisme, employeur non communiqué, etc.). Les envoyer au
# pipeline de résolution est non seulement inutile (aucun SIREN "correct"
# à trouver) mais dangereux : un faux positif sur un de ces noms très
# fréquents contamine potentiellement des centaines de milliers de lignes
# d'un coup (cf "Pôle emploi", observé en tête des entités sans SIREN).
# Comparaison faite en minuscules et sans accents (cf `sql_name_norm`).
NAME_BLACKLIST = {
    "pole emploi",
    "confidentiel",
    "entreprise confidentielle",
    "non communique",
    "non renseigne",
    "non precise",
    "employeur non precise",
    "employeur non communique",
    "entreprise non precisee",
    "anonyme",
    "sans nom",
    "inconnu",
    "n/a",
    "na",
    "nc",
    "-",
    "--",
    "*",
    ".",
    "communes",
    "place-de-l-emploi-public",
}


def sql_clean_numeric(col: str) -> str:
    """Strippe le suffixe '.0' produit par la promotion en DOUBLE d'une
    colonne numérique contenant des NaN (ex: location_zipcode lu comme
    34130.0 au lieu de 34130), quel que soit le type réel de la colonne
    (DOUBLE, BIGINT ou déjà VARCHAR)."""
    return rf"regexp_replace(CAST({col} AS VARCHAR), '\.0+$', '')"


def sql_siren_norm(col: str) -> str:
    """SIREN nettoyé et zero-paddé sur 9 chiffres (récupère les SIREN
    commençant par '0' perdus lors d'une promotion en DOUBLE), NULL si vide."""
    cleaned = sql_clean_numeric(col)
    return f"NULLIF(lpad({cleaned}, 9, '0'), lpad('', 9, '0'))"


def sql_name_norm(col: str) -> str:
    """Nom normalisé pour comparaison : minuscules, sans accents, trim."""
    return f"lower(strip_accents(trim({col})))"


def sql_is_blacklisted_name(col: str) -> str:
    """Prédicat SQL : True si le nom est un placeholder connu (blacklist
    statique) ou trop court pour être une raison sociale exploitable."""
    normalized = sql_name_norm(col)
    values = ", ".join("'" + v.replace("'", "''") + "'" for v in sorted(NAME_BLACKLIST))
    return f"({normalized} IN ({values}) OR length({normalized}) <= 1)"


def connect_jocas() -> duckdb.DuckDBPyConnection:
    """Ouvre la connexion DuckDB et enregistre la vue `jocas` sur le bucket
    S3 de prod, selon le pattern SSP Cloud/OVH habituel (AWS_S3_ENDPOINT +
    s3fs). L'instanciation de `s3fs.S3FileSystem` n'est pas utilisée
    directement par DuckDB (qui lit le S3 via sa propre extension httpfs),
    mais elle est conservée pour rester cohérente avec le reste du pipeline
    Observatoire Compétences Radar où `fs` sert à d'autres opérations
    (listing, lecture directe pandas, etc.).
    """
    s3_endpoint_url = "https://" + os.environ["AWS_S3_ENDPOINT"]
    fs = s3fs.S3FileSystem(endpoint_url=s3_endpoint_url)  # noqa: F841 (cf docstring)

    conn = duckdb.connect()
    conn.sql(f"""
        CREATE VIEW jocas AS
        SELECT *
        FROM read_parquet(
            '{JOCAS_S3_GLOB}',
            hive_partitioning=True,
            union_by_name=True
        )
    """)
    return conn


def clean_numeric_str(value) -> str | None:
    """Équivalent Python de `sql_clean_numeric`, pour le code appelé
    ligne-par-ligne côté pipeline (build_address)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    if text.endswith(".0"):
        text = text[:-2]
    return text or None


def build_entreprise_address(row: dict) -> Address:
    """Construit une Address à partir des colonnes JOCAS.

    Pas de rue disponible. `location_label` est utilisé comme ville (à
    ajuster si c'est en réalité un libellé plus générique type "Paris
    (75)" -- vérifier un échantillon avant un run massif).
    """
    zip_code = clean_numeric_str(row.get("location_zipcode"))
    departement = clean_numeric_str(row.get("location_departement"))
    city = str(row.get("location_label")) if row.get("location_label") else None
    return Address(street=None, zip_code=zip_code, city=city, department_hint=departement)


ENTREPRISE_COLUMNS = EntityColumns(
    name_col="entreprise_nom",
    siren_col="entreprise_siren",
    role="ENTREPRISE",
    address_builder=build_entreprise_address,
)
