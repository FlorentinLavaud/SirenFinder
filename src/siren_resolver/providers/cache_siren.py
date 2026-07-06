from __future__ import annotations

import logging

from ..cache import ParquetSirenCache
from ..models import CompanyQuery
from .base import SirenProvider

logger = logging.getLogger(__name__)


class CacheSirenProvider(SirenProvider):
    name = "cache"

    def __init__(self, cache: ParquetSirenCache):
        self._cache = cache

    @property
    def is_available(self) -> bool:
        return True

    async def resolve(self, query: CompanyQuery) -> str | None:
        return self._cache.lookup(query)
