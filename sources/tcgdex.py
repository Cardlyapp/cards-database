"""TCGdex source adapter for international and Japanese Pokemon cards."""

from __future__ import annotations

import re
import threading
from typing import Any
from urllib.parse import quote, unquote

import requests


BASE_URLS = {
    "international": "https://api.tcgdex.net/v2/en",
    "japan": "https://api.tcgdex.net/v2/ja",
}
_HTTP_THREAD_LOCAL = threading.local()


def _upstream_get(url: str, **kwargs: Any) -> requests.Response:
    """Reuse connections independently in each fetch worker."""
    session = getattr(_HTTP_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = "Cardly-Database-Updater/1.0"
        _HTTP_THREAD_LOCAL.session = session
    return session.get(url, **kwargs)


def _base_url(version: str) -> str:
    try:
        return BASE_URLS[version]
    except KeyError as exc:
        raise ValueError(f"Unsupported database region: {version}") from exc


def normalize_set_id(set_id: str, version: str) -> str:
    """Map printed Japanese plus codes to TCGdex card-bearing IDs."""
    value = str(set_id)
    if version == "japan" and re.fullmatch(r"SM\d+\+", value, re.IGNORECASE):
        return f"{value[:-1]}p"
    return value


def _normalize_set_summaries(
    sets: list[dict[str, Any]],
    version: str,
) -> list[dict[str, Any]]:
    if version != "japan":
        return sets

    canonical_ids = {
        str(row["id"]).casefold()
        for row in sets
        if isinstance(row, dict)
        and row.get("id")
        and normalize_set_id(row["id"], version) == str(row["id"])
    }
    normalized = []
    for row in sets:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        normalized_id = normalize_set_id(row["id"], version)
        if normalized_id != str(row["id"]) and normalized_id.casefold() in canonical_ids:
            print(f"Skipping duplicate {version} TCGdex set alias: {row['id']}")
            continue
        clean_row = dict(row)
        clean_row["id"] = normalized_id
        normalized.append(clean_row)
    return normalized


def fetch_all_sets(version: str = "international") -> list[dict[str, Any]]:
    """Fetch sets, deriving their IDs from the card index if necessary."""
    base_url = _base_url(version)
    print(f"Fetching all {version} sets from TCGdex...")
    response = _upstream_get(f"{base_url}/sets", timeout=(10, 30))
    try:
        response.raise_for_status()
        sets = response.json()
    except requests.RequestException as exc:
        print(f"TCGdex set list is unavailable ({exc}); deriving set IDs from the card index...")
        cards_response = _upstream_get(f"{base_url}/cards", timeout=(10, 60))
        cards_response.raise_for_status()
        cards = cards_response.json()
        if not isinstance(cards, list):
            raise RuntimeError("TCGdex card index did not return a list")

        sets_by_id: dict[str, dict[str, str]] = {}
        for card in cards:
            if not isinstance(card, dict):
                continue
            card_id = str(card.get("id") or "")
            local_id = str(card.get("localId") or "")
            suffix = f"-{local_id}"
            if card_id and local_id and card_id.endswith(suffix):
                set_id = card_id[: -len(suffix)]
                if set_id:
                    sets_by_id.setdefault(set_id, {"id": set_id})
        sets = list(sets_by_id.values())
        if not sets:
            raise RuntimeError("Could not derive any TCGdex set IDs from the card index")
        print(f"Derived {len(sets)} {version} set IDs from the TCGdex card index")

    if not isinstance(sets, list):
        raise RuntimeError("TCGdex set index did not return a list")
    return _normalize_set_summaries(sets, version)


def fetch_set_details(set_id: str, version: str = "international") -> dict[str, Any]:
    canonical_id = normalize_set_id(set_id, version)
    print(f"Fetching {version} set details from TCGdex for: {canonical_id}")
    response = _upstream_get(
        f"{_base_url(version)}/sets/{quote(canonical_id, safe='')}",
        timeout=(10, 30),
    )
    response.raise_for_status()
    return response.json()


def fetch_card_details(
    card_id: str,
    version: str = "international",
    set_id: str | None = None,
    local_id: Any = None,
) -> dict[str, Any]:
    base_url = _base_url(version)
    response = _upstream_get(f"{base_url}/cards/{card_id}", timeout=(10, 30))
    try:
        response.raise_for_status()
    except requests.HTTPError:
        if response.status_code != 404 or not set_id or local_id is None:
            raise
        canonical_local_id = quote(unquote(str(local_id)), safe="")
        canonical_card_id = f"{normalize_set_id(set_id, version)}-{canonical_local_id}"
        encoded_card_id = quote(canonical_card_id, safe="")
        response = _upstream_get(f"{base_url}/cards/{encoded_card_id}", timeout=(10, 30))
        response.raise_for_status()
    return response.json()


def detect_data_source(row: dict[str, Any]) -> str:
    return "tcgdex"


def _png_url(url: str | None) -> str | None:
    if not url:
        return None
    base, separator, query = url.partition("?")
    if "." not in base.rstrip("/").split("/")[-1]:
        base += ".png"
    return base + (separator + query if separator else "")


def transform_set_data(
    set_data: dict[str, Any],
    version: str = "international",
    source: str = "tcgdex",
) -> dict[str, Any]:
    serie = set_data.get("serie")
    card_count = set_data.get("cardCount")
    return {
        "id": set_data.get("id"),
        "name": set_data.get("name"),
        "series": serie.get("name") if isinstance(serie, dict) else serie,
        "total": card_count.get("total") if isinstance(card_count, dict) else set_data.get("total"),
        "release_date": set_data.get("releaseDate"),
        "images": {
            "logo": _png_url(set_data.get("logo")),
            "symbol": _png_url(set_data.get("symbol")),
        },
        "legalities": set_data.get("legal"),
        "version": version,
    }


def transform_card_data(
    card_data: dict[str, Any],
    version: str = "international",
    source: str = "tcgdex",
) -> dict[str, Any]:
    set_info = card_data.get("set") if isinstance(card_data.get("set"), dict) else {}
    legalities = card_data.get("legal") if isinstance(card_data.get("legal"), dict) else {}
    tcgplayer = card_data.get("tcgplayer")
    base_image_url = card_data.get("image")
    image_small_url = f"{base_image_url}/low.webp" if base_image_url else None
    image_large_url = f"{base_image_url}/high.webp" if base_image_url else None
    card_number = card_data.get("localId") or card_data.get("number")
    if not base_image_url and set_info.get("id") and card_number:
        image_small_url = f"https://images.pokemontcg.io/{set_info['id']}/{card_number}.png"
        image_large_url = f"https://images.pokemontcg.io/{set_info['id']}/{card_number}_hires.png"

    variants_detailed = None
    if isinstance(card_data.get("variants_detailed"), list):
        variants_detailed = {}
        for item in card_data["variants_detailed"]:
            if not isinstance(item, dict) or not item.get("type") or not item.get("size"):
                continue
            variant_type = item["type"]
            variant_size = item["size"]
            existing = variants_detailed.get(variant_type)
            if existing is None:
                variants_detailed[variant_type] = variant_size
            elif existing != variant_size:
                if not isinstance(existing, list):
                    existing = variants_detailed[variant_type] = [existing]
                if variant_size not in existing:
                    existing.append(variant_size)
        variants_detailed = variants_detailed or None

    return {
        "id": card_data.get("id"),
        "name": card_data.get("name"),
        "supertype": card_data.get("category"),
        "subtypes": card_data.get("dexId"),
        "hp": str(card_data.get("hp")) if card_data.get("hp") else None,
        "types": card_data.get("types"),
        "rarity": card_data.get("rarity"),
        "set_id": set_info.get("id"),
        "set_name": set_info.get("name"),
        "set_series": set_info.get("serie"),
        "set_symbol_url": _png_url(set_info.get("symbol")),
        "set_logo_url": _png_url(set_info.get("logo")),
        "number": card_data.get("localId"),
        "artist": card_data.get("illustrator"),
        "image_small_url": image_small_url,
        "image_large_url": image_large_url,
        "legality_standard": legalities.get("standard"),
        "legality_expanded": legalities.get("expanded"),
        "legality_unlimited": legalities.get("unlimited"),
        "regulation_mark": card_data.get("regulationMark"),
        "stage": card_data.get("stage"),
        "suffix": card_data.get("suffix"),
        "description": card_data.get("effect") or card_data.get("description"),
        "tcgplayer_url": tcgplayer.get("url") if isinstance(tcgplayer, dict) else None,
        "variants": card_data.get("variants"),
        "variants_detailed": variants_detailed,
        "version": version,
    }


def transform_price_data(card_id: str, pricing_data: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cardmarket = pricing_data.get("cardmarket")
    if isinstance(cardmarket, dict):
        rows.append(
            {
                "card_id": card_id,
                "market_source": "cardmarket",
                "condition": "average",
                "currency": cardmarket.get("unit", "EUR"),
                "low": cardmarket.get("low"),
                "average": cardmarket.get("avg"),
                "trend": str(cardmarket.get("trend")),
                "price_type": "normal",
                "last_updated": cardmarket.get("updated"),
            }
        )
        if "avg-holo" in cardmarket or "low-holo" in cardmarket:
            rows.append(
                {
                    "card_id": card_id,
                    "market_source": "cardmarket",
                    "condition": "average",
                    "currency": cardmarket.get("unit", "EUR"),
                    "low": cardmarket.get("low-holo"),
                    "average": cardmarket.get("avg-holo"),
                    "trend": str(cardmarket.get("trend-holo")),
                    "price_type": "holo",
                    "last_updated": cardmarket.get("updated"),
                }
            )

    tcgplayer = pricing_data.get("tcgplayer")
    if isinstance(tcgplayer, dict):
        for provider_key, price_type in (
            ("normal", "normal"),
            ("reverse", "reverse"),
            ("holofoil", "holofoil"),
            ("1stEdition", "1stEdition"),
        ):
            prices = tcgplayer.get(provider_key)
            if not isinstance(prices, dict):
                continue
            rows.append(
                {
                    "card_id": card_id,
                    "market_source": "tcgplayer",
                    "condition": "normal",
                    "currency": tcgplayer.get("unit", "USD"),
                    "low": prices.get("lowPrice"),
                    "mid": prices.get("midPrice"),
                    "high": prices.get("highPrice"),
                    "market": prices.get("marketPrice"),
                    "price_type": price_type,
                    "last_updated": tcgplayer.get("updated"),
                }
            )
    return rows
