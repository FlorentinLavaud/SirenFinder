class SirenResolverError(Exception):
    """Base exception du package."""


class ProviderQuotaExceeded(SirenResolverError):
    """Le quota journalier/horaire d'un fournisseur est épuisé."""


class ProviderUnavailable(SirenResolverError):
    """Le fournisseur est injoignable après retries (réseau, 5xx, WAF...)."""


class NoMatchFound(SirenResolverError):
    """Aucun résultat suffisamment fiable n'a été trouvé."""
