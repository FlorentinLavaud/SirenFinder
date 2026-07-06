from __future__ import annotations

import asyncio
import logging

from .cache import ParquetSirenCache
from .exceptions import ProviderQuotaExceeded, ProviderUnavailable
from .models import CompanyQuery, MatchConfidence, ResolutionResult
from .providers.base import SirenProvider
from .text_utils import is_groupement, split_groupement, split_slash_names

logger = logging.getLogger(__name__)


class SirenResolver:
    """Point d'entrée unique pour résoudre une CompanyQuery en SIREN.

    Ordre de résolution :
      1. Cache local (stock déjà connu) -> gratuit et instantané.
      2. Découpage des groupements ("A / B", "A et B") en sous-requêtes.
      3. Chaîne de fournisseurs, dans l'ordre fourni (Strategy + Chain of
         Responsibility), le premier résultat suffisamment fiable gagne.

    Le resolver ne fait aucune I/O de fichier : c'est au Pipeline de lire/
    écrire les parquet. Ça le rend testable avec de vrais mocks de provider.
    """

    def __init__(self, cache: ParquetSirenCache, providers: list[SirenProvider]):
        if not providers:
            raise ValueError("Au moins un fournisseur est requis.")
        self._cache = cache
        self._providers = providers

    def resolve(self, query: CompanyQuery) -> ResolutionResult:
        return asyncio.run(self.resolve_async(query))

    async def resolve_async(self, query: CompanyQuery) -> ResolutionResult:
        if is_groupement(query.raw_name):
            return await self._resolve_composite(query, split_groupement(query.raw_name))
        if "/" in query.raw_name and "groupement" not in query.raw_name.lower():
            return await self._resolve_composite(query, split_slash_names(query.raw_name))

        if query.address.is_empty:
            logger.debug("Adresse vide pour '%s', résolution impossible.", query.raw_name)
            return ResolutionResult(query=query, siren=None, confidence=MatchConfidence.NONE)

        return await self._resolve_single(query)

    async def _resolve_composite(self, query: CompanyQuery, sub_names: list[str]) -> ResolutionResult:
        """Un groupement d'entreprises n'a pas de SIREN propre : on résout
        chaque membre et on retourne le premier trouvé (comportement du
        script d'origine), en le traçant clairement dans matched_name.
        """
        for sub_name in sub_names:
            sub_query = CompanyQuery(raw_name=sub_name, address=query.address, role=query.role)
            result = await self._resolve_single(sub_query)
            if result.is_resolved:
                return ResolutionResult(
                    query=query,
                    siren=result.siren,
                    confidence=result.confidence,
                    match_score=result.match_score,
                    matched_name=f"{sub_name} (membre du groupement '{query.raw_name}')",
                )
        return ResolutionResult(query=query, siren=None, confidence=MatchConfidence.NONE)

    async def _resolve_single(self, query: CompanyQuery) -> ResolutionResult:
        for provider in self._providers:
            if not provider.is_available:
                logger.debug("Fournisseur %s indisponible, passage au suivant.", provider.name)
                continue
            try:
                siren = await provider.resolve(query)
                if siren:
                    if provider.name != "cache":
                        self._cache.upsert_many([(query, siren)])
                    confidence = MatchConfidence.CACHE if provider.name == "cache" else MatchConfidence.OFFICIAL_API
                    return ResolutionResult(query=query, siren=siren, confidence=confidence, match_score=1.0)
            except ProviderQuotaExceeded:
                logger.info("Quota épuisé pour %s, passage au fournisseur suivant.", provider.name)
                continue
            except ProviderUnavailable as exc:
                logger.warning("Fournisseur %s indisponible : %s. Passage au suivant.", provider.name, exc)
                continue
        return ResolutionResult(query=query, siren=None, confidence=MatchConfidence.NONE)
