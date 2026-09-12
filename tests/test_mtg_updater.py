import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests


MODULE_PATH = Path(__file__).parents[1] / "mtg-updater.py"
SPEC = importlib.util.spec_from_file_location("mtg_updater", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class MtgTransformTests(unittest.TestCase):
    def setUp(self):
        self.set_row = {
            "id": 23556,
            "name": "Foundations",
            "abbreviation": "FDN",
            "published_on": "2024-11-15",
        }
        self.product = {
            "id": 557921,
            "name": "Llanowar Elves",
            "number": "227",
            "rarity": "C",
            "scryfall_id": "printing-id",
            "image_url": "https://example.test/product.jpg",
            "ext_data": {"SubType": "Creature — Elf Druid", "P": "1", "T": "1"},
        }

    def test_card_uses_printing_identity_and_rich_scryfall_fields(self):
        scryfall = {
            "id": "printing-id",
            "oracle_id": "oracle-id",
            "name": "Llanowar Elves",
            "collector_number": "227",
            "mana_cost": "{G}",
            "cmc": 1.0,
            "type_line": "Creature — Elf Druid",
            "oracle_text": "{T}: Add {G}.",
            "power": "1",
            "toughness": "1",
            "legalities": {"standard": "legal"},
            "image_uris": {"normal": "https://example.test/normal.jpg"},
            "scryfall_uri": "https://scryfall.com/card/fdn/227",
        }

        card = MODULE.transform_card(self.product, self.set_row, scryfall)

        self.assertEqual(card["id"], "tcgplayer-557921")
        self.assertEqual(card["set_id"], "tcgtracking-23556")
        self.assertEqual(card["mana_cost"], "{G}")
        self.assertEqual(card["legalities"]["standard"], "legal")
        self.assertEqual(card["image_normal_url"], "https://example.test/normal.jpg")
        self.assertEqual(card["scryfall_url"], "https://scryfall.com/card/fdn/227")

    def test_double_faced_card_uses_first_face_image_as_fallback(self):
        scryfall = {
            "name": "Front // Back",
            "card_faces": [
                {"name": "Front", "image_uris": {"large": "https://example.test/front.jpg"}},
                {"name": "Back", "image_uris": {"large": "https://example.test/back.jpg"}},
            ],
        }

        card = MODULE.transform_card(self.product, self.set_row, scryfall)

        self.assertEqual(card["image_large_url"], "https://example.test/front.jpg")
        self.assertEqual(len(card["card_faces"]), 2)

    def test_product_metadata_is_used_when_scryfall_match_is_missing(self):
        card = MODULE.transform_card(self.product, self.set_row, None)

        self.assertEqual(card["name"], "Llanowar Elves")
        self.assertEqual(card["type_line"], "Creature — Elf Druid")
        self.assertEqual(card["power"], "1")
        self.assertEqual(card["image_normal_url"], "https://example.test/product.jpg")

    def test_japanese_card_uses_printed_name_text_and_image(self):
        japanese = {
            "id": "japanese-printing-id",
            "name": "Llanowar Elves",
            "printed_name": "ラノワールのエルフ",
            "type_line": "Creature — Elf Druid",
            "printed_type_line": "クリーチャー — エルフ・ドルイド",
            "oracle_text": "{T}: Add {G}.",
            "printed_text": "{T}：{G}を加える。",
            "image_uris": {"normal": "https://example.test/japanese.jpg"},
        }

        card = MODULE.transform_card(self.product, self.set_row, japanese, language="ja")

        self.assertEqual(card["name"], "ラノワールのエルフ")
        self.assertEqual(card["oracle_name"], "Llanowar Elves")
        self.assertEqual(card["scryfall_id"], "japanese-printing-id")
        self.assertEqual(card["tcgtracking_scryfall_id"], "printing-id")
        self.assertEqual(card["rules_text"], "{T}：{G}を加える。")
        self.assertEqual(card["language"], "ja")
        self.assertTrue(card["translation_available"])
        self.assertEqual(card["image_normal_url"], "https://example.test/japanese.jpg")

    def test_prices_include_tcgplayer_and_manapool_finishes(self):
        payload = {
            "updated": "2026-09-12T09:35:00-04:00",
            "prices": {
                "557921": {
                    "tcg": {
                        "Normal": {"low": 0.2, "market": 0.35},
                        "Foil": {"low": 0.3, "market": 1.57},
                    },
                    "manapool": {"normal": 0.15, "foil": 0.18, "etched": None},
                    "mp_qty": 5481,
                }
            },
        }

        rows = MODULE.transform_prices(payload, {"557921": "tcgplayer-557921"})

        self.assertEqual(len(rows), 4)
        self.assertEqual(
            {(row["market_source"], row["price_type"]) for row in rows},
            {
                ("tcgplayer", "normal"),
                ("tcgplayer", "foil"),
                ("manapool", "normal"),
                ("manapool", "foil"),
            },
        )
        manapool = next(row for row in rows if row["market_source"] == "manapool")
        self.assertEqual(manapool["quantity"], 5481)

    def test_japanese_sku_prices_include_condition_and_finish(self):
        payload = {
            "updated": "2026-09-12T09:35:00-04:00",
            "products": {
                "557921": {
                    "8277202": {
                        "cnd": "NM",
                        "var": "Normal",
                        "vid": 1,
                        "lng": "JP",
                        "mkt": 0.34,
                        "low": 0.2,
                        "hi": 0.75,
                        "cnt": 25,
                        "mp": 0.15,
                    },
                    "english": {"cnd": "NM", "var": "Normal", "lng": "EN", "mkt": 1},
                }
            },
        }

        rows = MODULE.transform_sku_prices(
            payload, {"557921": "tcgplayer-557921"}, "JP"
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual({row["market_source"] for row in rows}, {"tcgplayer", "manapool"})
        self.assertTrue(all(row["condition"] == "near_mint" for row in rows))
        self.assertTrue(all(row["language"] == "ja" for row in rows))

    def test_sealed_only_tracking_group_is_returned_as_empty(self):
        response = requests.Response()
        response.status_code = 404
        error = requests.HTTPError("not found", response=response)
        with patch.object(MODULE, "request_json", side_effect=error):
            payload = MODULE.fetch_set_payload(object(), {"id": 29})

        self.assertEqual(payload["products"], [])
        self.assertEqual(payload["pricing"], {"prices": {}})
        self.assertEqual(payload["skus"], {"products": {}})

    def test_catalog_validation_rejects_unknown_price_card(self):
        with self.assertRaisesRegex(RuntimeError, "prices reference unknown cards"):
            MODULE.validate_catalog(
                [{"id": "tcgtracking-1"}],
                [{"id": "tcgplayer-1", "set_id": "tcgtracking-1"}],
                [{"card_id": "tcgplayer-missing"}],
            )


class MtgManifestTests(unittest.TestCase):
    def test_price_publish_preserves_full_data_timestamp(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sets = [{"id": "tcgtracking-1", "source_id": 1}]
            cards = [{"id": "tcgplayer-1", "tcgplayer_id": 1}]
            prices = [{"card_id": "tcgplayer-1", "low": 1.0}]
            catalogs = {"english": (sets, cards, prices), "japanese": (sets, cards, prices)}
            for directory in (MODULE.DATA_DIRECTORY, MODULE.JAPANESE_DATA_DIRECTORY):
                data_dir = root / directory
                MODULE.write_json_atomic(data_dir / "sets.json", sets)
                MODULE.write_json_shards(data_dir, "cards", cards)
                MODULE.write_json_shards(data_dir, "prices", prices)

            with patch.object(MODULE, "utc_now", return_value="2026-09-01T00:00:00+00:00"):
                MODULE.publish_manifest(root, catalogs, "full")
            prices[0]["low"] = 2.0
            catalogs = {"english": (sets, cards, prices), "japanese": (sets, cards, prices)}
            for directory in (MODULE.DATA_DIRECTORY, MODULE.JAPANESE_DATA_DIRECTORY):
                MODULE.write_json_shards(root / directory, "prices", prices)
            with patch.object(MODULE, "utc_now", return_value="2026-09-02T00:00:00+00:00"):
                manifest = MODULE.publish_manifest(root, catalogs, "prices")

            saved = json.loads((root / "mtg" / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["dataUpdatedAt"], "2026-09-01T00:00:00+00:00")
        self.assertEqual(manifest["pricesUpdatedAt"], "2026-09-02T00:00:00+00:00")
        self.assertEqual(
            manifest["languages"]["japanese"]["dataUpdatedAt"],
            "2026-09-01T00:00:00+00:00",
        )
        self.assertEqual(manifest["languageCount"], 2)
        self.assertEqual(saved, manifest)


if __name__ == "__main__":
    unittest.main()
