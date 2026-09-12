#!/usr/bin/env python3
"""Build the English Magic: The Gathering catalog and daily price export."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests


SCHEMA_VERSION = 2
TCG_TRACKING_BASE_URL = "https://openapi.tcgtracking.com/v1"
SCRYFALL_BULK_URL = "https://api.scryfall.com/bulk-data"
MTG_CATEGORY_ID = 1
DATA_DIRECTORY = "mtg/data-english"
JAPANESE_DATA_DIRECTORY = "mtg/data-japanese"
MANIFEST_PATH = "mtg/manifest.json"
SHARD_COUNT = 32
DEFAULT_WORKERS = 12
REQUEST_ATTEMPTS = 5
REQUEST_TIMEOUT = 90
USER_AGENT = "Cardly-Database-Updater/1.0"

CONDITIONS = {
    "NM": "near_mint",
    "LP": "lightly_played",
    "MP": "moderately_played",
    "HP": "heavily_played",
    "DMG": "damaged",
    "UNO": "unopened",
}

SCRYFALL_FIELDS = (
    "id",
    "oracle_id",
    "name",
    "printed_name",
    "lang",
    "released_at",
    "scryfall_uri",
    "layout",
    "highres_image",
    "image_status",
    "image_uris",
    "mana_cost",
    "cmc",
    "type_line",
    "printed_type_line",
    "oracle_text",
    "printed_text",
    "flavor_text",
    "printed_flavor_text",
    "power",
    "toughness",
    "loyalty",
    "defense",
    "colors",
    "color_identity",
    "color_indicator",
    "keywords",
    "legalities",
    "games",
    "reserved",
    "foil",
    "nonfoil",
    "finishes",
    "oversized",
    "promo",
    "reprint",
    "variation",
    "set_id",
    "set",
    "set_name",
    "set_type",
    "collector_number",
    "digital",
    "rarity",
    "card_back_id",
    "artist",
    "artist_ids",
    "illustration_id",
    "border_color",
    "frame",
    "frame_effects",
    "security_stamp",
    "full_art",
    "textless",
    "booster",
    "story_spotlight",
    "edhrec_rank",
    "penny_rank",
    "card_faces",
    "related_uris",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(canonical_json_bytes(value))
    temporary.replace(path)


def read_json_list(path: Path, label: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Could not read {label} from {path}: {exc}") from exc
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise RuntimeError(f"The existing {label} is invalid: {path}")
    return value


def shard_name(index: int) -> str:
    return f"{index:02x}.json"


def row_shard(row: dict[str, Any]) -> int:
    row_id = str(row.get("id") or row.get("card_id") or "")
    if not row_id:
        raise RuntimeError("Cannot shard a row without id or card_id")
    return int(hashlib.sha256(row_id.encode("utf-8")).hexdigest()[:8], 16) % SHARD_COUNT


def write_json_shards(data_dir: Path, kind: str, rows: list[dict[str, Any]]) -> None:
    shards: list[list[dict[str, Any]]] = [[] for _ in range(SHARD_COUNT)]
    for row in rows:
        shards[row_shard(row)].append(row)
    for index, shard in enumerate(shards):
        write_json_atomic(data_dir / kind / shard_name(index), shard)


def read_json_shards(data_dir: Path, kind: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(SHARD_COUNT):
        rows.extend(read_json_list(data_dir / kind / shard_name(index), f"MTG {kind} shard"))
    return rows


def file_metadata(root: Path, path: Path) -> dict[str, Any]:
    content = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def request_json(session: requests.Session, url: str) -> dict[str, Any]:
    for attempt in range(1, REQUEST_ATTEMPTS + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise RuntimeError(f"Expected a JSON object from {url}")
            return value
        except (requests.RequestException, ValueError, RuntimeError):
            if attempt == REQUEST_ATTEMPTS:
                raise
            time.sleep(min(2 ** (attempt - 1), 15))
    raise AssertionError("unreachable")


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})
    return session


def source_set_id(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid TCG Tracking set ID: {value!r}") from exc


def catalog_set_id(value: Any) -> str:
    return f"tcgtracking-{source_set_id(value)}"


def catalog_card_id(value: Any) -> str:
    try:
        return f"tcgplayer-{int(value)}"
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid TCGPlayer product ID: {value!r}") from exc


def slug(value: Any) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower())
    return normalized.strip("_") or "unknown"


def compact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: compact(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [compact(item) for item in value if item is not None]
    return value


def transform_set(
    row: dict[str, Any], card_count: int, language: str = "en"
) -> dict[str, Any]:
    return compact(
        {
            "id": catalog_set_id(row.get("id")),
            "source_id": source_set_id(row.get("id")),
            "name": row.get("name"),
            "code": row.get("abbreviation"),
            "set_type": row.get("type"),
            "is_supplemental": row.get("is_supplemental"),
            "release_date": row.get("published_on"),
            "card_count": card_count,
            "set_symbol_url": row.get("set_symbol_url"),
            "source_modified_at": row.get("products_modified") or row.get("modified_on"),
            "language": language,
        }
    )


def selected_scryfall_data(row: dict[str, Any]) -> dict[str, Any]:
    return compact({key: row.get(key) for key in SCRYFALL_FIELDS})


def transform_card(
    product: dict[str, Any],
    set_row: dict[str, Any],
    scryfall: dict[str, Any] | None,
    language: str = "en",
) -> dict[str, Any]:
    scryfall = scryfall or {}
    extra = product.get("ext_data") if isinstance(product.get("ext_data"), dict) else {}
    card_faces = scryfall.get("card_faces")
    image_uris = scryfall.get("image_uris")
    if not isinstance(image_uris, dict) and isinstance(card_faces, list) and card_faces:
        first_face = card_faces[0] if isinstance(card_faces[0], dict) else {}
        image_uris = first_face.get("image_uris")
    image_uris = image_uris if isinstance(image_uris, dict) else {}

    japanese = language == "ja"
    oracle_name = scryfall.get("name") or product.get("name")
    oracle_type_line = scryfall.get("type_line") or extra.get("SubType")
    oracle_text = scryfall.get("oracle_text") or extra.get("OracleText")
    localized_name = scryfall.get("printed_name") if japanese else oracle_name
    localized_type_line = scryfall.get("printed_type_line") if japanese else oracle_type_line
    localized_text = scryfall.get("printed_text") if japanese else oracle_text
    localized_flavor = (
        scryfall.get("printed_flavor_text") if japanese else scryfall.get("flavor_text")
    )

    return compact(
        {
            "id": catalog_card_id(product.get("id")),
            "tcgplayer_id": int(product["id"]),
            "scryfall_id": scryfall.get("id") or product.get("scryfall_id"),
            "tcgtracking_scryfall_id": product.get("scryfall_id"),
            "mtgjson_uuid": product.get("mtgjson_uuid"),
            "cardmarket_id": product.get("cardmarket_id"),
            "cardtrader_id": product.get("cardtrader_id"),
            "oracle_id": scryfall.get("oracle_id"),
            "name": localized_name or oracle_name,
            "oracle_name": oracle_name,
            "language": language,
            "translation_available": bool(scryfall.get("printed_name")) if japanese else True,
            "set_id": catalog_set_id(set_row.get("id")),
            "set_name": set_row.get("name"),
            "set_code": set_row.get("abbreviation"),
            "number": scryfall.get("collector_number") or product.get("number"),
            "release_date": scryfall.get("released_at") or set_row.get("published_on"),
            "rarity": scryfall.get("rarity") or product.get("rarity"),
            "layout": scryfall.get("layout"),
            "mana_cost": scryfall.get("mana_cost"),
            "mana_value": scryfall.get("cmc") or product.get("mana_value"),
            "type_line": localized_type_line or oracle_type_line,
            "oracle_type_line": oracle_type_line,
            "rules_text": localized_text or oracle_text,
            "oracle_text": oracle_text,
            "flavor_text": localized_flavor
            or scryfall.get("flavor_text")
            or extra.get("FlavorText"),
            "power": scryfall.get("power") or extra.get("P"),
            "toughness": scryfall.get("toughness") or extra.get("T"),
            "loyalty": scryfall.get("loyalty"),
            "defense": scryfall.get("defense"),
            "colors": scryfall.get("colors") or product.get("colors"),
            "color_identity": scryfall.get("color_identity") or product.get("color_identity"),
            "color_indicator": scryfall.get("color_indicator"),
            "keywords": scryfall.get("keywords"),
            "legalities": scryfall.get("legalities"),
            "games": scryfall.get("games"),
            "finishes": scryfall.get("finishes") or product.get("finishes"),
            "card_faces": card_faces,
            "artist": scryfall.get("artist"),
            "artist_ids": scryfall.get("artist_ids"),
            "illustration_id": scryfall.get("illustration_id"),
            "reserved": scryfall.get("reserved"),
            "foil": scryfall.get("foil"),
            "nonfoil": scryfall.get("nonfoil"),
            "promo": scryfall.get("promo"),
            "reprint": scryfall.get("reprint"),
            "variation": scryfall.get("variation"),
            "digital": scryfall.get("digital"),
            "oversized": scryfall.get("oversized"),
            "full_art": scryfall.get("full_art"),
            "textless": scryfall.get("textless"),
            "booster": scryfall.get("booster"),
            "story_spotlight": scryfall.get("story_spotlight"),
            "border_color": scryfall.get("border_color") or product.get("border_color"),
            "frame": scryfall.get("frame"),
            "frame_effects": scryfall.get("frame_effects"),
            "security_stamp": scryfall.get("security_stamp"),
            "highres_image": scryfall.get("highres_image"),
            "image_status": scryfall.get("image_status"),
            "image_small_url": image_uris.get("small") or product.get("image_url"),
            "image_normal_url": image_uris.get("normal") or product.get("image_url"),
            "image_large_url": image_uris.get("large") or product.get("image_url"),
            "image_png_url": image_uris.get("png"),
            "tcgplayer_url": product.get("tcgplayer_url"),
            "manapool_url": product.get("manapool_url"),
            "scryfall_url": scryfall.get("scryfall_uri"),
            "gatherer_url": scryfall.get("related_uris", {}).get("gatherer")
            if isinstance(scryfall.get("related_uris"), dict)
            else None,
            "edhrec_rank": scryfall.get("edhrec_rank"),
            "penny_rank": scryfall.get("penny_rank"),
        }
    )


def valid_price(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def transform_prices(
    payload: dict[str, Any], product_to_card: dict[str, str]
) -> list[dict[str, Any]]:
    updated = payload.get("updated")
    prices = payload.get("prices")
    if not isinstance(prices, dict):
        return []
    rows: list[dict[str, Any]] = []
    for product_id, price_block in prices.items():
        card_id = product_to_card.get(str(product_id))
        if card_id is None or not isinstance(price_block, dict):
            continue
        tcg = price_block.get("tcg")
        if isinstance(tcg, dict):
            for finish, values in tcg.items():
                if not isinstance(values, dict):
                    continue
                if not any(valid_price(values.get(key)) for key in ("low", "market")):
                    continue
                rows.append(
                    compact(
                        {
                            "card_id": card_id,
                            "market_source": "tcgplayer",
                            "condition": "ungraded",
                            "language": "en",
                            "price_type": slug(finish),
                            "currency": "USD",
                            "low": values.get("low"),
                            "market": values.get("market"),
                            "last_updated": updated,
                        }
                    )
                )
        manapool = price_block.get("manapool")
        if isinstance(manapool, dict):
            for finish, low in manapool.items():
                if not valid_price(low):
                    continue
                rows.append(
                    compact(
                        {
                            "card_id": card_id,
                            "market_source": "manapool",
                            "condition": "ungraded",
                            "language": "en",
                            "price_type": slug(finish),
                            "currency": "USD",
                            "low": low,
                            "quantity": price_block.get("mp_qty"),
                            "last_updated": updated,
                        }
                    )
                )
    return rows


def transform_sku_prices(
    payload: dict[str, Any],
    product_to_card: dict[str, str],
    language_code: str,
) -> list[dict[str, Any]]:
    updated = payload.get("updated")
    products = payload.get("products")
    if not isinstance(products, dict):
        return []
    rows: list[dict[str, Any]] = []
    for product_id, sku_map in products.items():
        card_id = product_to_card.get(str(product_id))
        if card_id is None or not isinstance(sku_map, dict):
            continue
        for sku_id, sku in sku_map.items():
            if not isinstance(sku, dict) or str(sku.get("lng") or "").upper() != language_code:
                continue
            shared = {
                "card_id": card_id,
                "sku_id": str(sku_id),
                "condition": CONDITIONS.get(str(sku.get("cnd") or "").upper(), slug(sku.get("cnd"))),
                "language": "ja" if language_code == "JP" else language_code.lower(),
                "price_type": slug(sku.get("var")),
                "variant_id": sku.get("vid"),
                "currency": "USD",
                "last_updated": updated,
            }
            if any(valid_price(sku.get(key)) for key in ("low", "mkt", "hi")):
                rows.append(
                    compact(
                        {
                            **shared,
                            "market_source": "tcgplayer",
                            "low": sku.get("low"),
                            "market": sku.get("mkt"),
                            "high": sku.get("hi"),
                            "quantity": sku.get("cnt"),
                        }
                    )
                )
            if valid_price(sku.get("mp")):
                rows.append(
                    compact(
                        {
                            **shared,
                            "market_source": "manapool",
                            "low": sku.get("mp"),
                        }
                    )
                )
    return rows


def products_with_language(payload: dict[str, Any], language_code: str) -> set[str]:
    products = payload.get("products")
    if not isinstance(products, dict):
        return set()
    return {
        str(product_id)
        for product_id, sku_map in products.items()
        if isinstance(sku_map, dict)
        and any(
            isinstance(sku, dict)
            and str(sku.get("lng") or "").upper() == language_code
            for sku in sku_map.values()
        )
    }


def fetch_tracking_sets(session: requests.Session) -> list[dict[str, Any]]:
    payload = request_json(session, f"{TCG_TRACKING_BASE_URL}/{MTG_CATEGORY_ID}/sets")
    sets = payload.get("sets")
    if not isinstance(sets, list) or not all(isinstance(row, dict) for row in sets):
        raise RuntimeError("TCG Tracking returned an invalid MTG set list")
    return sets


def fetch_set_payload(session: requests.Session, set_row: dict[str, Any]) -> dict[str, Any]:
    set_id = source_set_id(set_row.get("id"))
    try:
        cards = request_json(
            session, f"{TCG_TRACKING_BASE_URL}/{MTG_CATEGORY_ID}/sets/{set_id}/cards"
        )
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code != 404:
            raise
        return {
            "set": set_row,
            "products": [],
            "pricing": {"prices": {}},
            "skus": {"products": {}},
        }
    pricing = request_json(
        session, f"{TCG_TRACKING_BASE_URL}/{MTG_CATEGORY_ID}/sets/{set_id}/pricing"
    )
    try:
        skus = request_json(
            session, f"{TCG_TRACKING_BASE_URL}/{MTG_CATEGORY_ID}/sets/{set_id}/skus"
        )
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code != 404:
            raise
        skus = {"products": {}}
    products = cards.get("products")
    if not isinstance(products, list) or not all(isinstance(row, dict) for row in products):
        raise RuntimeError(f"TCG Tracking returned invalid cards for MTG set {set_id}")
    return {"set": set_row, "products": products, "pricing": pricing, "skus": skus}


def fetch_full_payloads(
    sets: list[dict[str, Any]], workers: int
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    def fetch(row: dict[str, Any]) -> dict[str, Any]:
        with new_session() as session:
            return fetch_set_payload(session, row)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mtg-set") as executor:
        futures = {executor.submit(fetch, row): row for row in sets}
        for completed, future in enumerate(as_completed(futures), 1):
            result = future.result()
            results.append(result)
            if completed % 25 == 0 or completed == len(futures):
                print(f"Fetched {completed}/{len(futures)} MTG sets")
    return results


def fetch_scryfall_cards(
    session: requests.Session, required_ids: set[str]
) -> tuple[
    dict[str, dict[str, Any]],
    dict[tuple[str, str], list[dict[str, Any]]],
    str,
]:
    bulk = request_json(session, SCRYFALL_BULK_URL)
    entries = bulk.get("data")
    if not isinstance(entries, list):
        raise RuntimeError("Scryfall returned an invalid bulk data index")
    definition = next(
        (row for row in entries if isinstance(row, dict) and row.get("type") == "all_cards"),
        None,
    )
    if definition is None or not isinstance(definition.get("jsonl_download_uri"), str):
        raise RuntimeError("Scryfall did not provide the all-cards JSONL download")

    url = definition["jsonl_download_uri"]
    print(
        f"Downloading multilingual Scryfall metadata for "
        f"{len(required_ids)} MTG printings..."
    )
    response = session.get(url, timeout=REQUEST_TIMEOUT, stream=True)
    response.raise_for_status()
    response.raw.decode_content = False
    matched: dict[str, dict[str, Any]] = {}
    japanese_by_print: dict[tuple[str, str], list[dict[str, Any]]] = {}
    with gzip.GzipFile(fileobj=response.raw) as compressed:
        for line in compressed:
            card = json.loads(line)
            card_id = card.get("id") if isinstance(card, dict) else None
            if card_id in required_ids:
                matched[card_id] = selected_scryfall_data(card)
            if isinstance(card, dict) and card.get("lang") == "ja":
                selected = selected_scryfall_data(card)
                key = (
                    str(card.get("set") or "").casefold(),
                    str(card.get("collector_number") or "").casefold(),
                )
                japanese_by_print.setdefault(key, []).append(selected)
    print(
        f"Matched {len(matched)}/{len(required_ids)} products and indexed "
        f"{sum(len(rows) for rows in japanese_by_print.values())} Japanese printings"
    )
    return matched, japanese_by_print, str(definition.get("updated_at") or "")


def choose_japanese_printing(
    product: dict[str, Any],
    set_row: dict[str, Any],
    direct: dict[str, Any] | None,
    japanese_by_print: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any] | None:
    if direct and direct.get("lang") == "ja":
        return direct
    set_code = (direct or {}).get("set") or set_row.get("abbreviation")
    collector_number = (direct or {}).get("collector_number") or product.get("number")
    candidates = japanese_by_print.get(
        (str(set_code or "").casefold(), str(collector_number or "").casefold()), []
    )
    if not candidates:
        return None
    oracle_id = (direct or {}).get("oracle_id")
    same_oracle = [row for row in candidates if row.get("oracle_id") == oracle_id]
    if same_oracle:
        candidates = same_oracle
    illustration_id = (direct or {}).get("illustration_id")
    same_art = [row for row in candidates if row.get("illustration_id") == illustration_id]
    if same_art:
        candidates = same_art
    return sorted(candidates, key=lambda row: str(row.get("id") or ""))[0]


def sort_prices(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("card_id") or ""),
            str(row.get("market_source") or ""),
            str(row.get("condition") or ""),
            str(row.get("price_type") or ""),
        ),
    )


def validate_catalog(
    sets: list[dict[str, Any]],
    cards: list[dict[str, Any]],
    prices: list[dict[str, Any]],
) -> None:
    if not sets or not cards:
        raise RuntimeError("The MTG catalog cannot be published without sets and cards")
    set_ids = [str(row.get("id") or "") for row in sets]
    card_ids = [str(row.get("id") or "") for row in cards]
    if "" in set_ids or len(set_ids) != len(set(set_ids)):
        raise RuntimeError("The MTG catalog contains missing or duplicate set IDs")
    if "" in card_ids or len(card_ids) != len(set(card_ids)):
        raise RuntimeError("The MTG catalog contains missing or duplicate card IDs")
    known_sets = set(set_ids)
    unknown_card_sets = sorted(
        {str(row.get("set_id") or "") for row in cards} - known_sets
    )
    if unknown_card_sets:
        raise RuntimeError(f"MTG cards reference unknown sets: {unknown_card_sets[:10]}")
    known_cards = set(card_ids)
    unknown_price_cards = sorted(
        {str(row.get("card_id") or "") for row in prices} - known_cards
    )
    if unknown_price_cards:
        raise RuntimeError(f"MTG prices reference unknown cards: {unknown_price_cards[:10]}")


def build_full_catalog(
    output_root: Path,
    workers: int = DEFAULT_WORKERS,
    limit_sets: int | None = None,
    only_set_id: int | None = None,
) -> dict[str, Any]:
    with new_session() as session:
        sets = fetch_tracking_sets(session)
        if only_set_id is not None:
            sets = [row for row in sets if source_set_id(row.get("id")) == only_set_id]
        if limit_sets is not None:
            sets = sets[:limit_sets]
        if not sets:
            raise RuntimeError("No MTG sets were returned")
        payloads = fetch_full_payloads(sets, workers)
        products = [product for payload in payloads for product in payload["products"]]
        scryfall_ids = {
            str(product["scryfall_id"])
            for product in products
            if product.get("scryfall_id")
        }
        scryfall_cards, japanese_by_print, scryfall_updated_at = fetch_scryfall_cards(
            session, scryfall_ids
        )

    english_cards: list[dict[str, Any]] = []
    english_sets: list[dict[str, Any]] = []
    english_prices: list[dict[str, Any]] = []
    english_product_to_card: dict[str, str] = {}
    japanese_cards: list[dict[str, Any]] = []
    japanese_sets: list[dict[str, Any]] = []
    japanese_prices: list[dict[str, Any]] = []
    japanese_product_to_card: dict[str, str] = {}
    seen_products: set[str] = set()
    for payload in payloads:
        set_row = payload["set"]
        set_products = payload["products"]
        if not set_products:
            continue
        english_sets.append(transform_set(set_row, len(set_products)))
        japanese_product_ids = products_with_language(payload["skus"], "JP")
        japanese_set_card_count = 0
        for product in set_products:
            product_id = str(product.get("id"))
            if product_id in seen_products:
                raise RuntimeError(f"Duplicate TCGPlayer product ID: {product_id}")
            seen_products.add(product_id)
            card_id = catalog_card_id(product_id)
            english_product_to_card[product_id] = card_id
            direct = scryfall_cards.get(str(product.get("scryfall_id") or ""))
            english_cards.append(transform_card(product, set_row, direct))
            if product_id in japanese_product_ids:
                japanese = choose_japanese_printing(
                    product, set_row, direct, japanese_by_print
                )
                japanese_cards.append(
                    transform_card(product, set_row, japanese or direct, language="ja")
                )
                japanese_product_to_card[product_id] = card_id
                japanese_set_card_count += 1
        if japanese_set_card_count:
            japanese_sets.append(
                transform_set(set_row, japanese_set_card_count, language="ja")
            )

    for payload in payloads:
        english_prices.extend(
            transform_prices(payload["pricing"], english_product_to_card)
        )
        japanese_prices.extend(
            transform_sku_prices(payload["skus"], japanese_product_to_card, "JP")
        )

    english_sets.sort(key=lambda row: str(row["id"]))
    english_cards.sort(key=lambda row: str(row["id"]))
    english_prices = sort_prices(english_prices)
    japanese_sets.sort(key=lambda row: str(row["id"]))
    japanese_cards.sort(key=lambda row: str(row["id"]))
    japanese_prices = sort_prices(japanese_prices)
    validate_catalog(english_sets, english_cards, english_prices)
    if japanese_cards:
        validate_catalog(japanese_sets, japanese_cards, japanese_prices)

    for directory, sets_rows, card_rows, price_rows in (
        (DATA_DIRECTORY, english_sets, english_cards, english_prices),
        (JAPANESE_DATA_DIRECTORY, japanese_sets, japanese_cards, japanese_prices),
    ):
        data_dir = output_root / directory
        write_json_atomic(data_dir / "sets.json", sets_rows)
        write_json_shards(data_dir, "cards", card_rows)
        write_json_shards(data_dir, "prices", price_rows)
    return publish_manifest(
        output_root,
        {
            "english": (english_sets, english_cards, english_prices),
            "japanese": (japanese_sets, japanese_cards, japanese_prices),
        },
        update_type="full",
        scryfall_updated_at=scryfall_updated_at,
    )


def fetch_pricing_payloads(
    source_set_ids: list[int], workers: int
) -> tuple[dict[int, dict[str, Any]], list[int]]:
    payloads: dict[int, dict[str, Any]] = {}
    failures: list[int] = []

    def fetch(set_id: int) -> dict[str, Any]:
        with new_session() as session:
            return {
                "pricing": request_json(
                    session,
                    f"{TCG_TRACKING_BASE_URL}/{MTG_CATEGORY_ID}/sets/{set_id}/pricing",
                ),
                "skus": request_json(
                    session,
                    f"{TCG_TRACKING_BASE_URL}/{MTG_CATEGORY_ID}/sets/{set_id}/skus",
                ),
            }

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mtg-price") as executor:
        futures = {executor.submit(fetch, set_id): set_id for set_id in source_set_ids}
        for completed, future in enumerate(as_completed(futures), 1):
            set_id = futures[future]
            try:
                payloads[set_id] = future.result()
            except Exception as exc:
                failures.append(set_id)
                print(f"Preserving prior prices for failed MTG set {set_id}: {exc}", file=sys.stderr)
            if completed % 50 == 0 or completed == len(futures):
                print(f"Refreshed {completed}/{len(futures)} MTG set prices")
    return payloads, sorted(failures)


def refresh_prices(output_root: Path, workers: int = DEFAULT_WORKERS) -> dict[str, Any]:
    english_dir = output_root / DATA_DIRECTORY
    japanese_dir = output_root / JAPANESE_DATA_DIRECTORY
    english_sets = read_json_list(english_dir / "sets.json", "English MTG sets")
    english_cards = read_json_shards(english_dir, "cards")
    previous_english_prices = read_json_shards(english_dir, "prices")
    japanese_sets = read_json_list(japanese_dir / "sets.json", "Japanese MTG sets")
    japanese_cards = read_json_shards(japanese_dir, "cards")
    previous_japanese_prices = read_json_shards(japanese_dir, "prices")
    english_product_to_card = {
        str(card["tcgplayer_id"]): str(card["id"])
        for card in english_cards
        if card.get("tcgplayer_id") is not None and card.get("id")
    }
    japanese_product_to_card = {
        str(card["tcgplayer_id"]): str(card["id"])
        for card in japanese_cards
        if card.get("tcgplayer_id") is not None and card.get("id")
    }
    source_set_ids = [source_set_id(row.get("source_id")) for row in english_sets]
    payloads, failures = fetch_pricing_payloads(source_set_ids, workers)

    english_prices: list[dict[str, Any]] = []
    japanese_prices: list[dict[str, Any]] = []
    for payload in payloads.values():
        english_prices.extend(
            transform_prices(payload["pricing"], english_product_to_card)
        )
        japanese_prices.extend(
            transform_sku_prices(payload["skus"], japanese_product_to_card, "JP")
        )
    if failures:
        failed_catalog_sets = {catalog_set_id(set_id) for set_id in failures}
        failed_english_card_ids = {
            str(card["id"])
            for card in english_cards
            if card.get("set_id") in failed_catalog_sets and card.get("id")
        }
        failed_japanese_card_ids = {
            str(card["id"])
            for card in japanese_cards
            if card.get("set_id") in failed_catalog_sets and card.get("id")
        }
        english_prices.extend(
            row
            for row in previous_english_prices
            if str(row.get("card_id")) in failed_english_card_ids
        )
        japanese_prices.extend(
            row
            for row in previous_japanese_prices
            if str(row.get("card_id")) in failed_japanese_card_ids
        )

    english_prices = sort_prices(english_prices)
    japanese_prices = sort_prices(japanese_prices)
    validate_catalog(english_sets, english_cards, english_prices)
    if japanese_cards:
        validate_catalog(japanese_sets, japanese_cards, japanese_prices)
    write_json_shards(english_dir, "prices", english_prices)
    write_json_shards(japanese_dir, "prices", japanese_prices)
    return publish_manifest(
        output_root,
        {
            "english": (english_sets, english_cards, english_prices),
            "japanese": (japanese_sets, japanese_cards, japanese_prices),
        },
        update_type="prices",
        failed_set_ids=failures,
    )


def existing_manifest(output_root: Path) -> dict[str, Any]:
    try:
        value = json.loads((output_root / MANIFEST_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def content_hash(metadata: Iterable[dict[str, Any]]) -> str:
    return hashlib.sha256(
        "".join(item["sha256"] for item in metadata).encode("ascii")
    ).hexdigest()


def shard_metadata(root: Path, data_dir: Path, kind: str) -> list[dict[str, Any]]:
    return [
        file_metadata(root, data_dir / kind / shard_name(index))
        for index in range(SHARD_COUNT)
    ]


def publish_manifest(
    output_root: Path,
    catalogs: dict[
        str,
        tuple[
            list[dict[str, Any]],
            list[dict[str, Any]],
            list[dict[str, Any]],
        ],
    ],
    update_type: str,
    scryfall_updated_at: str | None = None,
    failed_set_ids: list[int] | None = None,
) -> dict[str, Any]:
    if update_type not in {"full", "prices"}:
        raise ValueError(f"Unsupported update type: {update_type}")
    prior = existing_manifest(output_root)
    prior_languages = prior.get("languages")
    if not isinstance(prior_languages, dict):
        prior_languages = {}
    now = utc_now()
    language_config = {
        "english": ("en", DATA_DIRECTORY),
        "japanese": ("ja", JAPANESE_DATA_DIRECTORY),
    }
    languages: dict[str, dict[str, Any]] = {}
    for name, (sets, cards, prices) in catalogs.items():
        language_code, directory = language_config[name]
        data_dir = output_root / directory
        files = {
            "sets": file_metadata(output_root, data_dir / "sets.json"),
            "cards": shard_metadata(output_root, data_dir, "cards"),
            "prices": shard_metadata(output_root, data_dir, "prices"),
        }
        prior_region = prior_languages.get(name)
        if not isinstance(prior_region, dict):
            prior_region = {}
        data_version = content_hash((files["sets"], *files["cards"]))
        price_version = content_hash(files["prices"])
        languages[name] = {
            "language": language_code,
            "directory": directory,
            "version": content_hash(
                (files["sets"], *files["cards"], *files["prices"])
            ),
            "dataVersion": data_version,
            "priceVersion": price_version,
            "dataUpdatedAt": now
            if update_type == "full"
            else prior_region.get("dataUpdatedAt", now),
            "pricesUpdatedAt": now,
            "setCount": len(sets),
            "cardCount": len(cards),
            "priceCount": len(prices),
            "failedSetCount": len(failed_set_ids or []),
            "failedSetIds": failed_set_ids or [],
            "shardCount": SHARD_COUNT,
            "files": files,
        }
    version = hashlib.sha256(
        "".join(languages[name]["version"] for name in sorted(languages)).encode("ascii")
    ).hexdigest()
    data_version = hashlib.sha256(
        "".join(languages[name]["dataVersion"] for name in sorted(languages)).encode("ascii")
    ).hexdigest()
    price_version = hashlib.sha256(
        "".join(languages[name]["priceVersion"] for name in sorted(languages)).encode("ascii")
    ).hexdigest()
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "game": "mtg",
        "version": version,
        "dataVersion": data_version,
        "priceVersion": price_version,
        "publishedAt": now,
        "dataUpdatedAt": now if update_type == "full" else prior.get("dataUpdatedAt", now),
        "pricesUpdatedAt": now,
        "updateType": update_type,
        "languageCount": len(languages),
        "setCount": sum(region["setCount"] for region in languages.values()),
        "cardCount": sum(region["cardCount"] for region in languages.values()),
        "priceCount": sum(region["priceCount"] for region in languages.values()),
        "failedSetCount": len(failed_set_ids or []),
        "failedSetIds": failed_set_ids or [],
        "languages": languages,
        "sources": {
            "cards": "scryfall-all-cards",
            "products": "tcgtracking",
            "prices": ["tcgplayer", "manapool"],
        },
    }
    source_updated = scryfall_updated_at or prior.get("scryfallUpdatedAt")
    if source_updated:
        manifest["scryfallUpdatedAt"] = source_updated
    write_json_atomic(output_root / MANIFEST_PATH, manifest)
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export English and Japanese Magic: The Gathering data"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prices-only", action="store_true")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--limit-sets", type=int, help="Testing only: export the first N sets")
    parser.add_argument("--set-id", type=int, help="Testing only: export one TCG Tracking set ID")
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.limit_sets is not None and args.limit_sets < 1:
        parser.error("--limit-sets must be at least 1")
    if args.set_id is not None and args.set_id < 1:
        parser.error("--set-id must be at least 1")
    if args.limit_sets is not None and args.set_id is not None:
        parser.error("--limit-sets and --set-id cannot be combined")
    if args.prices_only and (args.limit_sets is not None or args.set_id is not None):
        parser.error("testing set filters cannot be used with --prices-only")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_root = args.output.resolve()
    if args.prices_only:
        manifest = refresh_prices(output_root, args.workers)
    else:
        manifest = build_full_catalog(output_root, args.workers, args.limit_sets, args.set_id)
    print(
        f"MTG catalog {manifest['version'][:12]} ready: "
        f"{manifest['cardCount']} cards, {manifest['priceCount']} prices"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
