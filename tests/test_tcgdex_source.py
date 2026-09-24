import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests


MODULE_PATH = Path(__file__).parents[1] / "sources" / "tcgdex.py"
SPEC = importlib.util.spec_from_file_location("tcgdex_source", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class TCGdexSourceTests(unittest.TestCase):
    def test_marketplace_ids_are_exported(self):
        card = {
            "id": "sv02-021",
            "localId": "021",
            "variants_detailed": [
                {"type": "normal", "size": "standard", "thirdParty": {"cardmarket": 111, "tcgplayer": 684397}},
                {"type": "reverse", "size": "standard", "foil": "pokeball", "stamp": ["staff"], "variantId": "variant-2", "thirdParty": {"cardmarket": 222, "tcgplayer": 684397, "cardtrader": 333}, "pricing": {
                    "cardmarket": {"idProduct": 222, "unit": "EUR", "avg": 0.16, "avg-holo": 0.22},
                    "tcgplayer": {"unit": "USD", "holofoil": {"productId": 684397, "marketPrice": 0.07}, "reverse-holofoil": {"productId": 684397, "marketPrice": 0.22}},
                }},
            ],
            "pricing": {
                "cardmarket": {"idProduct": 877413, "unit": "EUR"},
                "tcgplayer": {
                    "unit": "USD",
                    "normal": {"productId": 684397, "marketPrice": 0.07},
                    "reverse-holofoil": {"productId": 684397, "marketPrice": 0.22},
                },
            },
        }
        exported = MODULE.transform_card_data(card)
        self.assertEqual(exported["id"], "sv02-021")
        self.assertEqual(exported["identifiers"], {"tcgdex": "sv02-021"})
        self.assertEqual(exported["variants"], [
            {"id": None, "name": "Normal", "type": "normal", "size": "standard", "subtype": None, "foil": None, "stamps": [], "identifiers": {"cardmarket": 111, "tcgplayer": 684397}},
            {"id": "variant-2", "name": "Pokeball Reverse Holo Staff", "type": "reverse", "size": "standard", "subtype": None, "foil": "pokeball", "stamps": ["staff"], "identifiers": {"cardmarket": 222, "tcgplayer": 684397, "cardtrader": 333}},
        ])
        self.assertEqual(exported["language"], "english")
        prices = MODULE.transform_price_data(card["id"], card)
        self.assertEqual(len(prices), 2)
        self.assertEqual({row["variant_id"] for row in prices}, {"variant-2"})
        self.assertEqual(prices[0]["product_id"], 222)
        self.assertEqual(prices[0]["holo_average"], 0.22)
        self.assertEqual(prices[1]["price_type"], "reverse-holofoil")
        self.assertEqual(prices[1]["market"], 0.22)

    def test_pricing_ids_are_not_exported_as_identifiers(self):
        card = {
            "id": "example-1",
            "pricing": {
                "cardmarket": {"idProduct": 123},
                "tcgplayer": {"holofoil": {"productId": 456}},
            },
        }
        self.assertEqual(MODULE.transform_card_data(card)["identifiers"], {"tcgdex": "example-1"})

    def test_card_shape_and_variant_name_order(self):
        card = {
            "id": "set-021", "name": "Lokix", "category": "Pokemon", "dexId": [920],
            "hp": 120, "localId": "021", "set": {"id": "set", "name": "Set"},
            "legal": {"standard": False, "expanded": True},
            "image": "https://assets.tcgdex.net/ja/set/021",
            "variants_detailed": [
                {"variantId": "v1", "type": "holo", "size": "jumbo", "foil": "cosmos", "subtype": "unlimited", "stamp": ["1st-edition", "staff"]},
                {"variantId": "v2", "type": "holofoil", "size": "standard"},
            ],
        }
        exported = MODULE.transform_card_data(card, "japan")
        self.assertEqual(exported["pokemon_ids"], [920])
        self.assertEqual(exported["hp"], 120)
        self.assertEqual(exported["set_id"], "set")
        self.assertEqual(exported["legalities"], {"standard": False, "expanded": True, "unlimited": None})
        self.assertEqual(exported["images"]["small"], "https://assets.tcgdex.net/ja/set/021/low.webp")
        self.assertEqual(exported["language"], "japanese")
        self.assertEqual(exported["variants"][0]["name"], "Jumbo Cosmos Holofoil Unlimited 1st Edition Staff")
        self.assertEqual(exported["variants"][1]["name"], "Holofoil")
        for removed in ("subtypes", "set_name", "set_series", "version", "variants_detailed", "image_small_url"):
            self.assertNotIn(removed, exported)

    def test_null_price_providers_are_ignored(self):
        self.assertEqual(
            MODULE.transform_price_data(
                "card-1",
                {"variants_detailed": [{"variantId": "v1", "thirdParty": {"cardmarket": 1}, "pricing": {"cardmarket": None, "tcgplayer": None}}]},
            ),
            [],
        )

    def test_null_tcgplayer_variants_are_ignored(self):
        rows = MODULE.transform_price_data(
            "card-1",
            {"variants_detailed": [{
                "variantId": "v1", "type": "reverse", "thirdParty": {"tcgplayer": 123},
                "pricing": {"tcgplayer": {
                    "normal": None,
                    "reverse": {"productId": 123, "lowPrice": 1.25, "marketPrice": 2.5},
                }},
            }]},
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price_type"], "reverse")
        self.assertEqual(rows[0]["variant_id"], "v1")

    def test_shared_product_prices_keep_distinct_variant_ids(self):
        shared_pricing = {
            "cardmarket": {"idProduct": 715494, "unit": "EUR", "avg": 0.06, "avg-holo": 0.16},
            "tcgplayer": {
                "unit": "USD",
                "holofoil": {"productId": 497432, "marketPrice": 0.04},
                "reverse-holofoil": {"productId": 497432, "marketPrice": 0.22},
            },
        }
        card = {"variants_detailed": [
            {"variantId": "holo-id", "type": "holo", "thirdParty": {"cardmarket": 715494, "tcgplayer": 497432}, "pricing": shared_pricing},
            {"variantId": "reverse-id", "type": "reverse", "thirdParty": {"cardmarket": 715494, "tcgplayer": 497432}, "pricing": shared_pricing},
        ]}
        rows = MODULE.transform_price_data("sv02-021", card)
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            {(row["variant_id"], row["market"]) for row in rows if row["market_source"] == "tcgplayer"},
            {("holo-id", 0.04), ("reverse-id", 0.22)},
        )
        self.assertEqual(
            {row["variant_id"] for row in rows if row["market_source"] == "cardmarket"},
            {"holo-id", "reverse-id"},
        )

    def test_mismatched_marketplace_ids_do_not_link_prices(self):
        card = {"variants_detailed": [{
            "variantId": "v1", "type": "normal",
            "thirdParty": {"cardmarket": 1, "tcgplayer": 2},
            "pricing": {
                "cardmarket": {"idProduct": 3, "avg": 1.0},
                "tcgplayer": {"normal": {"productId": 4, "marketPrice": 2.0}},
            },
        }]}
        self.assertEqual(MODULE.transform_price_data("card-1", card), [])

    def test_set_index_503_falls_back_to_card_index(self):
        unavailable = Mock()
        unavailable.raise_for_status.side_effect = requests.HTTPError("unavailable")
        cards = Mock()
        cards.raise_for_status.return_value = None
        cards.json.return_value = [
            {"id": "base1-1", "localId": "1"},
            {"id": "tk-xy-n-12", "localId": "12"},
        ]

        with patch.object(MODULE, "_upstream_get", side_effect=[unavailable, cards]):
            result = MODULE.fetch_all_sets("international")

        self.assertEqual(result, [{"id": "base1"}, {"id": "tk-xy-n"}])

    def test_japanese_aliases_are_collapsed_case_insensitively(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = [
            {"id": "sm2+"},
            {"id": "SM2p"},
            {"id": "SM2K"},
        ]

        with patch.object(MODULE, "_upstream_get", return_value=response):
            result = MODULE.fetch_all_sets("japan")

        self.assertEqual([row["id"] for row in result], ["SM2p", "SM2K"])

    def test_japanese_card_goes_directly_to_tcgdex(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"id": "SV5K-098"}

        with patch.object(MODULE, "_upstream_get", return_value=response) as get:
            result = MODULE.fetch_card_details("SV5K-098", "japan")

        self.assertEqual(result["id"], "SV5K-098")
        self.assertEqual(
            get.call_args.args[0],
            "https://api.tcgdex.net/v2/ja/cards/SV5K-098",
        )
        self.assertEqual(get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
