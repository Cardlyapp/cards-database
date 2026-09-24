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


def _variant_label(value: str) -> str:
    special = {"mcdonalds": "McDonald's", "1st-edition": "1st Edition"}
    return special.get(value, value.replace("-", " ").replace("_", " ").title())


def _variant_name(variant: dict[str, Any]) -> str:
    variant_type = str(variant["type"])
    type_name = {
        "reverse": "Reverse Holo",
        "holo": "Holofoil",
        "holofoil": "Holofoil",
        "normal": "Normal",
    }.get(variant_type, _variant_label(variant_type))
    parts = []
    if variant.get("size") == "jumbo":
        parts.append("Jumbo")
    if variant.get("foil"):
        parts.append(_variant_label(str(variant["foil"])))
    parts.append(type_name)
    if variant.get("subtype"):
        parts.append(_variant_label(str(variant["subtype"])))
    stamps = variant.get("stamps") or []
    parts.extend(_variant_label(str(stamp)) for stamp in stamps)
    return " ".join(parts)


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
    base_image_url = card_data.get("image")
    image_small_url = f"{base_image_url}/low.webp" if base_image_url else None
    image_large_url = f"{base_image_url}/high.webp" if base_image_url else None
    card_number = card_data.get("localId") or card_data.get("number")
    if not base_image_url and set_info.get("id") and card_number:
        image_small_url = f"https://images.pokemontcg.io/{set_info['id']}/{card_number}.png"
        image_large_url = f"https://images.pokemontcg.io/{set_info['id']}/{card_number}_hires.png"

    variants = []
    if isinstance(card_data.get("variants_detailed"), list):
        for item in card_data["variants_detailed"]:
            if not isinstance(item, dict) or not item.get("type") or not item.get("size"):
                continue
            third_party = item.get("thirdParty")
            marketplace_ids = (
                {
                    market: third_party[market]
                    for market in ("cardmarket", "tcgplayer", "cardtrader")
                    if third_party.get(market) is not None
                }
                if isinstance(third_party, dict)
                else {}
            )
            stamps = item.get("stamp") or []
            if not isinstance(stamps, list):
                stamps = [stamps]
            variant = {
                "id": item.get("variantId"),
                "type": item["type"],
                "size": item["size"],
                "subtype": item.get("subtype"),
                "foil": item.get("foil"),
                "stamps": stamps,
                "identifiers": marketplace_ids,
            }
            variant["name"] = _variant_name(variant)
            variants.append(variant)

    hp = card_data.get("hp")
    if hp is not None:
        try:
            hp = int(hp)
        except (TypeError, ValueError):
            hp = None

    return {
        "id": card_data.get("id"),
        "name": card_data.get("name"),
        "supertype": card_data.get("category"),
        "pokemon_ids": card_data.get("dexId") or [],
        "hp": hp,
        "types": card_data.get("types"),
        "rarity": card_data.get("rarity"),
        "set_id": set_info.get("id"),
        "number": card_number,
        "artist": card_data.get("illustrator"),
        "images": {"small": image_small_url, "large": image_large_url},
        "legalities": {
            "standard": legalities.get("standard"),
            "expanded": legalities.get("expanded"),
            "unlimited": legalities.get("unlimited"),
        },
        "regulation_mark": card_data.get("regulationMark"),
        "stage": card_data.get("stage"),
        "suffix": card_data.get("suffix"),
        "description": card_data.get("effect") or card_data.get("description"),
        "identifiers": {"tcgdex": card_data.get("id")},
        "variants": variants,
        "language": "english" if version == "international" else "japanese",
    }


def _tcgplayer_price_key(variant: dict[str, Any], pricing: dict[str, Any]) -> str | None:
    variant_type = variant.get("type")
    base = {
        "normal": "normal",
        "holo": "holofoil",
        "holofoil": "holofoil",
        "reverse": "reverse-holofoil",
    }.get(variant_type)
    if base is None:
        return None
    stamps = variant.get("stamp") or []
    if not isinstance(stamps, list):
        stamps = [stamps]
    if "1st-edition" in stamps:
        candidates = ("1st-edition-holofoil", "1stEdition") if base == "holofoil" else ("1st-edition", "1stEdition")
    elif variant.get("subtype") == "unlimited":
        candidates = (("unlimited-holofoil", base) if base == "holofoil" else ("unlimited", base))
    else:
        candidates = (base, "reverse" if base == "reverse-holofoil" else base)
    return next((key for key in candidates if isinstance(pricing.get(key), dict)), None)


def transform_price_data(card_id: str, card_data: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    variants = card_data.get("variants_detailed")
    if not isinstance(variants, list):
        return rows
    for variant in variants:
        if not isinstance(variant, dict) or not variant.get("variantId"):
            continue
        pricing = variant.get("pricing")
        third_party = variant.get("thirdParty")
        if not isinstance(pricing, dict) or not isinstance(third_party, dict):
            continue
        variant_id = variant["variantId"]
        cardmarket = pricing.get("cardmarket")
        cardmarket_id = third_party.get("cardmarket")
        if (
            cardmarket_id is not None
            and isinstance(cardmarket, dict)
            and cardmarket.get("idProduct") == cardmarket_id
        ):
            rows.append({
                "card_id": card_id,
                "variant_id": variant_id,
                "market_source": "cardmarket",
                "product_id": cardmarket_id,
                "currency": cardmarket.get("unit", "EUR"),
                "low": cardmarket.get("low"),
                "average": cardmarket.get("avg"),
                "trend": cardmarket.get("trend"),
                "holo_low": cardmarket.get("low-holo"),
                "holo_average": cardmarket.get("avg-holo"),
                "holo_trend": cardmarket.get("trend-holo"),
                "last_updated": cardmarket.get("updated"),
            })

        tcgplayer = pricing.get("tcgplayer")
        tcgplayer_id = third_party.get("tcgplayer")
        if tcgplayer_id is None or not isinstance(tcgplayer, dict):
            continue
        price_key = _tcgplayer_price_key(variant, tcgplayer)
        prices = tcgplayer.get(price_key) if price_key else None
        if not isinstance(prices, dict) or prices.get("productId") != tcgplayer_id:
            continue
        rows.append({
            "card_id": card_id,
            "variant_id": variant_id,
            "market_source": "tcgplayer",
            "product_id": tcgplayer_id,
            "currency": tcgplayer.get("unit", "USD"),
            "price_type": price_key,
            "low": prices.get("lowPrice"),
            "mid": prices.get("midPrice"),
            "high": prices.get("highPrice"),
            "market": prices.get("marketPrice"),
            "last_updated": tcgplayer.get("updated"),
        })
    return rows
