from __future__ import annotations

import logging
import re
import time

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ..config import GoogleCSEConfig
from ..exceptions import ProviderQuotaExceeded, ProviderUnavailable
from ..models import CompanyQuery, MatchConfidence, ResolutionResult
from ..rate_limiter import DailyQuota, TokenBucketRateLimiter
from ..text_utils import extract_siren_from_siret_or_siren
from .base import SirenProvider

logger = logging.getLogger(__name__)

_SIRET_RE = re.compile(r"(\d{14})(?:/)?$")
_SIREN_RE = re.compile(r"(\d{9})(?:/)?$")


class GoogleCSEProvider(SirenProvider):
    """Fournisseur de secours, repris du script d'origine mais isolé et
    fiabilisé : quota explicite, rate limiting, retries. À n'utiliser que
    lorsque l'API officielle n'a rien trouvé (nom trop ambigu, entreprise
    radiée, etc.) car le quota gratuit est faible (100/j en standard).
    """

    name = "google_cse"

    def __init__(self, config: GoogleCSEConfig):
        self._config = config
        self._quota = DailyQuota(config.daily_quota)
        self._rate_limiter = TokenBucketRateLimiter(config.requests_per_second)
        self._service = build("customsearch", "v1", developerKey=config.api_key) if config.enabled else None

    @property
    def is_available(self) -> bool:
        return self._config.enabled and not self._quota.exhausted

    def resolve(self, query: CompanyQuery) -> ResolutionResult:
        if not self.is_available:
            raise ProviderQuotaExceeded("Quota Google CSE épuisé ou fournisseur désactivé.")

        candidates = self._build_queries(query)
        for q in candidates:
            siren = self._search_for_siren(q)
            if siren:
                return ResolutionResult(query=query, siren=siren, confidence=MatchConfidence.GOOGLE_CSE, match_score=1.0)
        return ResolutionResult(query=query, siren=None, confidence=MatchConfidence.NONE)

    def _build_queries(self, query: CompanyQuery) -> list[str]:
        name, addr = query.raw_name, query.address
        queries = []
        if addr.city:
            queries.append(f"site:annuaire-entreprises.data.gouv.fr/etablissement {name} {addr.city}")
            queries.append(f"site:annuaire-entreprises.data.gouv.fr {name} {addr.city}")
        if addr.zip_code:
            queries.append(f"site:annuaire-entreprises.data.gouv.fr {name} {addr.zip_code}")
        if addr.department:
            queries.append(f"site:annuaire-entreprises.data.gouv.fr {name} {addr.department}")
        return queries

    def _search_for_siren(self, query_str: str) -> str | None:
        if self._quota.exhausted:
            return None
        self._rate_limiter.acquire()
        try:
            self._quota.consume()
            result = self._service.cse().list(q=query_str, cx=self._config.cse_id, num=1).execute()
        except HttpError as exc:
            logger.warning("Erreur Google CSE pour la requête '%s' : %s", query_str, exc)
            time.sleep(1)
            return None
        for item in result.get("items", []):
            link = item.get("link", "")
            match = _SIRET_RE.search(link) or _SIREN_RE.search(link)
            if match:
                siren = extract_siren_from_siret_or_siren(match.group(1))
                if siren:
                    return siren
        return None
