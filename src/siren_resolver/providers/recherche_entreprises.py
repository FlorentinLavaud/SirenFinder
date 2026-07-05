from __future__ import annotations

import logging
import time

import requests

from ..config import RechercheEntreprisesConfig
from ..exceptions import ProviderUnavailable
from ..models import Address, CompanyQuery, MatchConfidence, ResolutionResult
from ..rate_limiter import TokenBucketRateLimiter
from ..text_utils import name_similarity
from .base import SirenProvider

logger = logging.getLogger(__name__)


class RechercheEntreprisesProvider(SirenProvider):
    """Fournisseur principal, basé sur l'API publique officielle de l'INSEE
    (via recherche-entreprises.api.gouv.fr). Contrairement à un scraping de
    résultats Google, cette API :
      - retourne des données structurées (pas de parsing d'URL au regex) ;
      - fournit le SIREN, la raison sociale exacte et l'adresse du siège ;
      - permet de scorer la correspondance nom/adresse pour rejeter les
        faux positifs, au lieu de prendre "le premier lien qui matche".
    """

    name = "recherche_entreprises"

    def __init__(self, config: RechercheEntreprisesConfig, min_match_score: float = 0.55):
        self._config = config
        self._min_match_score = min_match_score
        self._rate_limiter = TokenBucketRateLimiter(config.requests_per_second)
        self._session = requests.Session()

    @property
    def is_available(self) -> bool:
        return True  # API publique, pas de quota connu à faire respecter côté client

    def resolve(self, query: CompanyQuery) -> ResolutionResult:
        params = {
            "q": query.raw_name,
            "per_page": 5,
        }
        if query.address.zip_code:
            params["code_postal"] = query.address.zip_code
        if query.address.department:
            params["departement"] = query.address.department

        payload = self._get_with_retry(params)
        best = self._best_match(payload.get("results", []), query)
        if best is None:
            return ResolutionResult(query=query, siren=None, confidence=MatchConfidence.NONE)
        siren, score, matched_name = best
        return ResolutionResult(
            query=query,
            siren=siren,
            confidence=MatchConfidence.OFFICIAL_API,
            match_score=score,
            matched_name=matched_name,
        )

    def _best_match(self, results: list[dict], query: CompanyQuery) -> tuple[str, float, str] | None:
        best: tuple[str, float, str] | None = None
        for r in results:
            candidate_name = r.get("nom_complet") or r.get("nom_raison_sociale") or ""
            score = name_similarity(query.raw_name, candidate_name)
            # Bonus léger si le code postal du siège correspond -> réduit
            # les faux positifs sur les raisons sociales génériques.
            siege = r.get("siege", {}) or {}
            if query.address.zip_code and siege.get("code_postal") == query.address.zip_code:
                score = min(1.0, score + 0.1)
            if score >= self._min_match_score and (best is None or score > best[1]):
                siren = r.get("siren")
                if siren:
                    best = (str(siren), score, candidate_name)
        return best

    def _get_with_retry(self, params: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(self._config.max_retries):
            self._rate_limiter.acquire()
            try:
                resp = self._session.get(
                    self._config.base_url, params=params, timeout=self._config.timeout_seconds
                )
                if resp.status_code == 429:
                    self._sleep_backoff(attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("Échec appel API officielle (tentative %d/%d) : %s",
                                attempt + 1, self._config.max_retries, exc)
                self._sleep_backoff(attempt)
        raise ProviderUnavailable(f"recherche-entreprises.api.gouv.fr injoignable : {last_error}")

    def _sleep_backoff(self, attempt: int) -> None:
        time.sleep(self._config.backoff_base_seconds * (2 ** attempt))
