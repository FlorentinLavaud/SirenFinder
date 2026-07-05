"""Fonctions pures de nettoyage de texte : pas d'I/O, pas d'état -> faciles
à tester unitairement, contrairement au script original où tout était
mélangé avec les appels API.
"""
from __future__ import annotations

import ast
import json
import re
from difflib import SequenceMatcher
from typing import Optional

from .models import Address

_RANG1_RE = re.compile(r"rang[\s\-]?1", re.IGNORECASE)
_PARENS_RE = re.compile(r"\([^)]*\)")
_QUOTES_RE = re.compile(r'["“”«»]')
_SPACES_RE = re.compile(r"\s+")
_GROUPEMENT_RE = re.compile(r"groupement( (solidaire|conjoint))?", re.IGNORECASE)
_SPLIT_SEP_RE = re.compile(r"\s*/\s*|\s*-\s*|\s*,\s*|\s+et\s+|\s*&\s*|\s*\\\\\s*", re.IGNORECASE)


def strip_rang1(name: str) -> str:
    return _RANG1_RE.sub("", name).strip()


def is_groupement(name: str) -> bool:
    lowered = name.lower()
    has_separator = any(sep in lowered for sep in ["/", "-", ",", "&", "\\"]) or " et " in lowered
    return "groupement" in lowered and "public" not in lowered and has_separator


def split_groupement(name: str) -> list[str]:
    """Découpe un nom de groupement d'entreprises en sous-noms individuels."""
    cleaned = _GROUPEMENT_RE.sub("", name)
    cleaned = _PARENS_RE.sub("", cleaned)
    cleaned = _QUOTES_RE.sub("", cleaned)
    cleaned = _SPACES_RE.sub(" ", cleaned).strip()
    parts = [p.strip() for p in _SPLIT_SEP_RE.split(cleaned) if p.strip()]
    return parts[:10]


def split_slash_names(name: str) -> list[str]:
    """Cas 'A / B' hors groupement explicite."""
    cleaned = _PARENS_RE.sub("", name)
    cleaned = _QUOTES_RE.sub("", cleaned)
    cleaned = _SPACES_RE.sub(" ", cleaned).strip()
    return [p.strip() for p in cleaned.split("/") if p.strip()][:10]


def normalize_company_name(name: str) -> str:
    return _SPACES_RE.sub(" ", strip_rang1(str(name))).strip().lower()


def _safe_parse_dict(raw: str) -> dict:
    """Parse une adresse encodée en JSON ou en repr Python (le script
    d'origine mélangeait les deux formats selon la source)."""
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        return ast.literal_eval(raw.replace("null", "None"))
    except (ValueError, SyntaxError):
        return {}


def _normalize_address_component(raw) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.lower() in {"nan", "none"}:
        return None
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text or None


def parse_address(raw: str | dict) -> Address:
    """Construit un Address à partir d'un champ brut (UBL-like dict/JSON).

    Si l'adresse fournie est une chaîne non-JSON, on la conserve comme
    composante brute pour éviter de perdre totalement l'information.
    """
    d = raw if isinstance(raw, dict) else _safe_parse_dict(raw)
    if isinstance(raw, str):
        raw_text = raw.strip()
        if raw_text and not d:
            return Address(street=raw_text)

    street = d.get("StreetName")
    if isinstance(street, dict):
        street = street.get("#text")
    city = d.get("CityName")
    zip_code = d.get("PostalZone")
    return Address(
        street=str(street).strip() if street else None,
        zip_code=str(zip_code).strip() if zip_code else None,
        city=str(city).strip() if city else None,
    )


def extract_siren_from_siret_or_siren(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) == 14:
        return digits[:9]
    if len(digits) == 9:
        return digits
    return None


def normalize_address_repr(raw) -> str:
    """Représentation canonique d'une adresse, quel que soit son format
    d'entrée : JSON/dict brut (fichiers externes) ou chaîne déjà
    normalisée (`Address.as_query_string()`, utilisée en interne une fois
    une entrée ajoutée au cache dans le run courant). Sans cette double
    tolérance, une même adresse produit deux clés de cache différentes
    selon son origine et le cache "rate" silencieusement.
    """
    if isinstance(raw, dict):
        return parse_address(raw).as_query_string().strip().lower()
    if isinstance(raw, str):
        parsed = parse_address(raw)
        if not parsed.is_empty:
            return parsed.as_query_string().strip().lower()
        return raw.strip().lower()
    return ""


def name_similarity(a: str, b: str) -> float:
    """Score de similarité [0, 1] entre deux raisons sociales normalisées."""
    return SequenceMatcher(None, normalize_company_name(a), normalize_company_name(b)).ratio()
