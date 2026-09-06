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
    def test_null_price_providers_are_ignored(self):
        self.assertEqual(
            MODULE.transform_price_data(
                "card-1",
                {"cardmarket": None, "tcgplayer": None},
            ),
            [],
        )

    def test_null_tcgplayer_variants_are_ignored(self):
        rows = MODULE.transform_price_data(
            "card-1",
            {
                "tcgplayer": {
                    "normal": None,
                    "reverse": {"lowPrice": 1.25, "marketPrice": 2.5},
                }
            },
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price_type"], "reverse")

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
