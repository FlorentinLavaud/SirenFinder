"""Récupère la base SIRENE complète (établissements) via l'API officielle
INSEE Sirene (api.insee.fr, endpoint /siret) et écrit le résultat nettoyé,
partitionné par département, directement sur MinIO/S3 — sans passer par le
fichier stock téléchargé à la main.

Pourquoi l'API plutôt que le fichier stock (cf. `prepare_sirene_reference.py`) :
volontairement lent (rate-limit 30 req/min sur le plan Public), mais permet
de tourner sans télécharger/uploader un fichier de plusieurs Go, et de
filtrer nativement côté serveur (actifs seulement, NAF précis, etc.) si
besoin. Pour 13M+ établissements, prévoir plusieurs heures (voir README).

Pagination : l'API v3.11 utilise un curseur opaque (`curseur=*` sur la
première requête, puis `curseurSuivant` renvoyé par l'API). On boucle
jusqu'à ce que `curseurSuivant == curseur` (fin de résultats). Un
checkpoint local (JSON) sauvegarde le dernier curseur pour permettre de
reprendre un run interrompu avec `--resume` sans repartir de zéro.

Usage :
    export INSEE_API_KEY=...
    export AWS_ACCESS_KEY_ID=...
    export AWS_SECRET_ACCESS_KEY=...
    export AWS_SESSION_TOKEN=...          # optionnel
    export AWS_S3_ENDPOINT=minio.lab.sspcloud.fr

    python fetch_sirene_api.py \
        --output s3://flavaud/SirenFinder/sirene_reference_api \
        --checkpoint ./sirene_fetch_checkpoint.json \
        --resume
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import duckdb
import pandas as pd
import requests

logger = logging.getLogger(__name__)


class TokenBucketRateLimiter:
    """Rate limiter minimaliste, autonome (volontairement pas importé depuis
    `siren_resolver.rate_limiter` : ce script n'a pas besoin des dépendances
    lourdes du package — sentence-transformers, lightgbm — chargées par
    `siren_resolver/__init__.py`)."""

    def __init__(self, requests_per_second: float, burst: int | None = None):
        if requests_per_second <= 0:
            raise ValueError("requests_per_second doit être > 0")
        self._rate = requests_per_second
        self._capacity = burst if burst is not None else max(1, int(requests_per_second))
        self._tokens = float(self._capacity)
        self._last_refill = time.monotonic()

    def acquire(self) -> None:
        while True:
            self._refill()
            if self._tokens >= 1:
                self._tokens -= 1
                return
            wait_time = (1 - self._tokens) / self._rate
            time.sleep(max(wait_time, 0.01))

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now

API_BASE_URL = "https://api.insee.fr/api-sirene/3.11/siret"

# Colonnes de sortie alignées sur `prepare_sirene_reference.build_clean_sirene_sql`
# pour que les deux chemins (fichier stock vs API) produisent un référentiel
# interchangeable pour `LocalMlSirenProvider` / `DuckDBSirenRepository`.
OUTPUT_COLUMNS = [
    "siren",
    "siret",
    "raison_sociale",
    "enseigne",
    "naf_ape",
    "naf_label",
    "code_postal",
    "commune",
    "code_departement",
    "is_headquarters",
]


@dataclass
class FetchState:
    curseur: str = "*"
    total_fetched: int = 0
    total_expected: int | None = None
    batch_index: int = 0

    @classmethod
    def load(cls, path: Path) -> "FetchState":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        return cls(**data)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=2))


def department_from_postal_code(code_postal: str | None) -> str | None:
    if not code_postal or len(code_postal) < 2:
        return None
    if code_postal[:2] in ("97", "98") and len(code_postal) >= 3:
        return code_postal[:3]
    return code_postal[:2]


def extract_row(etablissement: dict[str, Any]) -> dict[str, Any] | None:
    """Aplati un enregistrement `etablissement` de l'API Sirene V3.11 vers
    le schéma de sortie commun. Renvoie None si les champs indispensables
    manquent (établissement non diffusible, en général)."""
    siren = etablissement.get("siren")
    siret = etablissement.get("siret")
    unite_legale = etablissement.get("uniteLegale", {}) or {}
    raison_sociale = (
        unite_legale.get("denominationUniteLegale")
        or unite_legale.get("nomUniteLegale")
        or unite_legale.get("sigleUniteLegale")
    )
    if not siren or not siret or not raison_sociale:
        return None  # établissement non diffusible ou données incomplètes

    periodes = etablissement.get("periodesEtablissement", []) or []
    periode_courante = periodes[0] if periodes else {}

    adresse = etablissement.get("adresseEtablissement", {}) or {}
    code_postal = adresse.get("codePostalEtablissement")
    commune = adresse.get("libelleCommuneEtablissement")

    return {
        "siren": str(siren).strip(),
        "siret": str(siret).strip(),
        "raison_sociale": str(raison_sociale).strip(),
        "enseigne": etablissement.get("periodesEtablissement", [{}])[0].get("enseigne1Etablissement")
        if periodes
        else None,
        "naf_ape": periode_courante.get("activitePrincipaleEtablissement"),
        "naf_label": None,  # non fourni tel quel par l'API, cf. nomenclature NAF si besoin
        "code_postal": code_postal,
        "commune": commune,
        "code_departement": department_from_postal_code(code_postal),
        "is_headquarters": bool(etablissement.get("etablissementSiege")),
    }


class InseeSireneClient:
    def __init__(self, api_key: str, requests_per_minute: float = 28.0, timeout_seconds: int = 30, max_retries: int = 6):
        # 28/min plutôt que 30/min pile : marge de sécurité contre les
        # dérives d'horloge / retries qui feraient dépasser le quota réel.
        self._rate_limiter = TokenBucketRateLimiter(requests_per_minute / 60.0, burst=1)
        self._session = requests.Session()
        self._session.headers.update({"X-INSEE-Api-Key-Integration": api_key, "Accept": "application/json"})
        self._timeout = timeout_seconds
        self._max_retries = max_retries

    def fetch_page(self, query: str, curseur: str, nombre: int) -> dict[str, Any]:
        params = {"q": query, "nombre": nombre, "curseur": curseur}
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            self._rate_limiter.acquire()
            try:
                resp = self._session.get(API_BASE_URL, params=params, timeout=self._timeout)
                if resp.status_code == 429:
                    wait = 5.0 * (2 ** attempt)
                    logger.warning("429 reçu, pause %.0fs (tentative %d/%d)", wait, attempt + 1, self._max_retries)
                    time.sleep(wait)
                    continue
                if resp.status_code >= 500:
                    wait = 3.0 * (2 ** attempt)
                    logger.warning("Erreur serveur %d, pause %.0fs", resp.status_code, wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_error = exc
                wait = 3.0 * (2 ** attempt)
                logger.warning("Échec requête API Sirene (tentative %d/%d) : %s", attempt + 1, self._max_retries, exc)
                time.sleep(wait)
        raise RuntimeError(f"API Sirene injoignable après {self._max_retries} tentatives : {last_error}")


def iter_pages(client: InseeSireneClient, query: str, nombre: int, state: FetchState) -> Iterator[list[dict[str, Any]]]:
    while True:
        payload = client.fetch_page(query, state.curseur, nombre)
        header = payload.get("header", {})
        if state.total_expected is None:
            state.total_expected = header.get("total")
            logger.info("Volume total annoncé par l'API : %s établissements", state.total_expected)

        etablissements = payload.get("etablissements", [])
        curseur_suivant = header.get("curseurSuivant")

        yield etablissements

        state.total_fetched += len(etablissements)
        if not curseur_suivant or curseur_suivant == state.curseur:
            logger.info("Fin de pagination (curseur stable). Total récupéré : %d", state.total_fetched)
            return
        state.curseur = curseur_suivant


def configure_s3(conn: duckdb.DuckDBPyConnection, endpoint: str, access_key: str, secret_key: str, session_token: str | None, url_style: str = "path") -> None:
    conn.execute("INSTALL httpfs; LOAD httpfs;")
    conn.execute(f"SET s3_endpoint='{endpoint}';")
    conn.execute(f"SET s3_access_key_id='{access_key}';")
    conn.execute(f"SET s3_secret_access_key='{secret_key}';")
    if session_token:
        conn.execute(f"SET s3_session_token='{session_token}';")
    conn.execute(f"SET s3_url_style='{url_style}';")
    conn.execute("SET s3_region='us-east-1';")


def flush_batch(conn: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]], output_path: str, batch_index: int) -> None:
    if not rows:
        return
    df = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    # Un SIREN sans département connu (adresse non diffusible) part dans
    # une partition "inconnu" plutôt que de faire planter le partitioning.
    df["code_departement"] = df["code_departement"].fillna("inconnu")
    conn.register("batch_df", df)
    # Nom de fichier unique par batch : plusieurs fichiers par partition
    # `code_departement=XX/` s'accumulent, ce que `read_parquet('glob/*')`
    # gère nativement (pas besoin d'un vrai "append" Parquet).
    conn.execute(
        f"""
        COPY (SELECT * FROM batch_df)
        TO '{output_path}'
        (FORMAT PARQUET, PARTITION_BY (code_departement), OVERWRITE_OR_IGNORE TRUE,
         FILENAME_PATTERN 'batch_{batch_index}_{{i}}')
        """
    )
    conn.unregister("batch_df")
    logger.info("Batch %d écrit sur %s (%d lignes)", batch_index, output_path, len(rows))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch de la base SIRENE complète via l'API INSEE, écriture directe sur MinIO/S3.")
    parser.add_argument("--output", required=True, help="Chemin S3 de sortie (parquet partitionné par code_departement).")
    parser.add_argument("--query", default="periode(etatAdministratifEtablissement:A)", help="Filtre Sirene (q=...). Par défaut : établissements actifs uniquement.")
    parser.add_argument("--nombre", type=int, default=1000, help="Nombre d'établissements par page (max autorisé par l'API : 1000).")
    parser.add_argument("--requests-per-minute", type=float, default=28.0, help="Cadence d'appel (quota API Sirene plan Public : 30/min).")
    parser.add_argument("--batch-size", type=int, default=50_000, help="Nombre de lignes accumulées avant écriture sur S3.")
    parser.add_argument("--checkpoint", default="./sirene_fetch_checkpoint.json", help="Fichier local de reprise (curseur, compteurs).")
    parser.add_argument("--resume", action="store_true", help="Reprendre depuis le checkpoint existant au lieu de repartir de zéro.")
    parser.add_argument("--insee-api-key", default=os.environ.get("INSEE_API_KEY"), help="Clé API Sirene (header X-INSEE-Api-Key-Integration).")
    parser.add_argument("--s3-endpoint", default=os.environ.get("AWS_S3_ENDPOINT"))
    parser.add_argument("--s3-access-key-id", default=os.environ.get("AWS_ACCESS_KEY_ID"))
    parser.add_argument("--s3-secret-access-key", default=os.environ.get("AWS_SECRET_ACCESS_KEY"))
    parser.add_argument("--s3-session-token", default=os.environ.get("AWS_SESSION_TOKEN"))
    parser.add_argument("--s3-url-style", default=os.environ.get("AWS_S3_URL_STYLE", "path"), choices=["path", "virtual"])
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = parse_args()

    if not args.insee_api_key:
        raise SystemExit("INSEE_API_KEY manquant (variable d'env ou --insee-api-key). "
                          "À récupérer sur https://portail-api.insee.fr après souscription à l'API Sirene.")
    if not (args.s3_endpoint and args.s3_access_key_id and args.s3_secret_access_key):
        raise SystemExit("Config S3 incomplète (AWS_S3_ENDPOINT / AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY).")

    checkpoint_path = Path(args.checkpoint)
    state = FetchState.load(checkpoint_path) if args.resume else FetchState()
    if args.resume and checkpoint_path.exists():
        logger.info("Reprise depuis le checkpoint : curseur=%s, déjà récupéré=%d", state.curseur, state.total_fetched)

    client = InseeSireneClient(args.insee_api_key, requests_per_minute=args.requests_per_minute)

    conn = duckdb.connect(database=":memory:")
    configure_s3(conn, args.s3_endpoint, args.s3_access_key_id, args.s3_secret_access_key, args.s3_session_token, args.s3_url_style)

    buffer: list[dict[str, Any]] = []
    start_time = time.monotonic()

    try:
        for page in iter_pages(client, args.query, args.nombre, state):
            for etablissement in page:
                row = extract_row(etablissement)
                if row is not None:
                    buffer.append(row)

            if len(buffer) >= args.batch_size:
                flush_batch(conn, buffer, args.output, state.batch_index)
                state.batch_index += 1
                buffer.clear()
                state.save(checkpoint_path)

                elapsed_min = (time.monotonic() - start_time) / 60
                if state.total_expected:
                    pct = 100 * state.total_fetched / state.total_expected
                    logger.info("Progression : %d/%s (%.1f%%) — %.1f min écoulées",
                                state.total_fetched, state.total_expected, pct, elapsed_min)

        # Dernier batch partiel
        if buffer:
            flush_batch(conn, buffer, args.output, state.batch_index)
            state.batch_index += 1
            state.save(checkpoint_path)

        logger.info("Terminé. Total écrit : %d établissements dans %s", state.total_fetched, args.output)

    except KeyboardInterrupt:
        logger.warning("Interruption manuelle. Checkpoint sauvegardé (curseur=%s, %d lignes déjà écrites). "
                        "Relancer avec --resume pour reprendre.", state.curseur, state.total_fetched)
        state.save(checkpoint_path)
        if buffer:
            flush_batch(conn, buffer, args.output, state.batch_index)
        raise
    except Exception:
        logger.exception("Erreur pendant le fetch. Checkpoint sauvegardé pour reprise avec --resume.")
        state.save(checkpoint_path)
        raise


if __name__ == "__main__":
    main()