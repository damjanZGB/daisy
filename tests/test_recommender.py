import io
import unittest
from datetime import date
from unittest import mock
import urllib.error

import aws.lambda_function as lf


class TestRecommenderHelpers(unittest.TestCase):
    def test_month_from_phrase_next_year(self):
        y, m = lf._month_from_phrase("March next year", reference=date(2025, 10, 19))
        self.assertEqual((y, m), (2026, 3))

    def test_month_from_phrase_rollover(self):
        y, m = lf._month_from_phrase("January", reference=date(2025, 11, 1))
        self.assertEqual((y, m), (2026, 1))

    def test_parse_month_range(self):
        (y1, m1), (y2, m2) = lf._parse_month_range("2026-03..2026-04")
        self.assertEqual((y1, m1), (2026, 3))
        self.assertEqual((y2, m2), (2026, 4))

    def test_theme_aliases_skiing(self):
        tags = lf._canonicalize_theme_tags(["skiing"], "Skiing in December")
        self.assertIn("winter_sports", tags)
        self.assertIn("cold", tags)


class TestScoring(unittest.TestCase):
    def setUp(self):
        # Patch coords to avoid external file dependency
        lf._IATA_COORDS_CACHE = {
            "MUC": (48.3538, 11.7861),
            "TFS": (28.0445, -16.5725),
            "GVA": (46.2381, 6.10895),
        }

    def test_beach_preference_march(self):
        tfs = {
            "code": "TFS",
            "tags": ["beach", "warm"],
            "avgHighCByMonth": {"3": 23},
            "lhGroupCarriers": ["EW", "4Y", "LH"],
        }
        gva = {
            "code": "GVA",
            "tags": ["winter_sports", "cold"],
            "avgHighCByMonth": {"3": 11},
            "snowReliability": {"1": 0.8, "2": 0.75, "12": 0.8},
            "lhGroupCarriers": ["LX", "LH"],
        }
        s_tfs, _ = lf._score_destination(tfs, {"beach", "warm"}, 3, "MUC")
        s_gva, _ = lf._score_destination(gva, {"beach", "warm"}, 3, "MUC")
        self.assertGreater(s_tfs, s_gva)

    def test_winter_preference_january(self):
        tfs = {
            "code": "TFS",
            "tags": ["beach", "warm"],
            "avgHighCByMonth": {"1": 21},
            "lhGroupCarriers": ["EW", "4Y", "LH"],
        }
        gva = {
            "code": "GVA",
            "tags": ["winter_sports", "cold"],
            "snowReliability": {"1": 0.8, "2": 0.75, "12": 0.8},
            "avgHighCByMonth": {"1": 4},
            "lhGroupCarriers": ["LX", "LH"],
        }
        s_tfs, _ = lf._score_destination(tfs, {"winter_sports"}, 1, "MUC")
        s_gva, _ = lf._score_destination(gva, {"winter_sports"}, 1, "MUC")
        self.assertGreater(s_gva, s_tfs)

class TestExploreProxy(unittest.TestCase):
    def test_summer_2026_query_normalizes_and_keeps_filters(self):
        fake_response = mock.MagicMock()
        fake_response.read.return_value = b'{"destinations": []}'
        fake_response.getcode.return_value = 200
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value = fake_response
            result = lf._call_proxy(
                "/google/explore/search",
                "GET",
                {
                    "engine": "google_travel_explore",
                    "departure_id": "ZAG",
                    "time_period": "Summer 2026",
                    "interests": "beach",
                    "travel_class": "business",
                    "max_price": "1200",
                },
                None,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        request_obj = mock_urlopen.call_args[0][0]
        self.assertTrue(request_obj.full_url.startswith(lf.GOOGLE_BASE_URL))
        self.assertIn(
            "time_period=2026-06-01..2026-08-31", request_obj.full_url
        )
        self.assertIn("interests=beaches", request_obj.full_url)
        self.assertIn("travel_class=business", request_obj.full_url)
        self.assertIn("max_price=1200", request_obj.full_url)

    def test_primary_error_surface_when_no_tool_fallback(self):
        error_fp = io.BytesIO(b"Cannot GET /google/explore/search")
        primary_error = urllib.error.HTTPError(
            f"{lf.GOOGLE_BASE_URL}/google/explore/search",
            404,
            "Not Found",
            None,
            error_fp,
        )
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [primary_error]
            result = lf._call_proxy(
                "/google/explore/search",
                "GET",
                {
                    "departure_id": "ZAG",
                    "time_period": "Summer 2026",
                    "interests": "beach",
                },
                None,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 404)
        self.assertEqual(mock_urlopen.call_count, 1)
        first_request = mock_urlopen.call_args_list[0][0][0]
        self.assertTrue(first_request.full_url.startswith(lf.GOOGLE_BASE_URL))


if __name__ == "__main__":
    unittest.main()

