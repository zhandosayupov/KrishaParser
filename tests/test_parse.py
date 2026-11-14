"""Проверки разбора на сохранённых страницах (без обращения к сайту).

Запуск:  python -m unittest discover tests
"""
import unittest
from pathlib import Path

from krisha.config import (parse_search_url, build_list_url, polygon_from_params,
                           load_config, filter_params, search_params)
from krisha.parse import parse_list_page, parse_preview, total_count, is_blocked, BlockedError

FIX = Path(__file__).parent / "fixtures"
LIST = (FIX / "list_page.html").read_text(encoding="utf-8")
BLOCKED = (FIX / "blocked_page.html").read_text(encoding="utf-8")


class ListPage(unittest.TestCase):
    def setUp(self):
        self.items = {it["id"]: it for it in parse_list_page(LIST)}

    def test_counts(self):
        self.assertEqual(len(self.items), 21)
        self.assertEqual(total_count(LIST), 528)

    def test_regular_flat(self):
        it = self.items[1012495559]
        self.assertEqual((it["rooms"], it["area"], it["floor"], it["floors"]), (2, 63.6, 2, 13))
        self.assertEqual(it["price"], 50_000_000)
        self.assertEqual(it["district"], "Медеуский р-н")
        self.assertEqual(it["complex"], "RAMS Garden")
        self.assertEqual((it["house_type"], it["year"], it["ceiling"]), ("монолитный", 2025, 3.0))
        self.assertEqual(it["is_layout"], 0)
        self.assertEqual(it["photo_count"], 16)
        self.assertTrue(it["description"].startswith("Прямая продажа"))

    def test_condition_and_furniture(self):
        self.assertEqual(self.items[1007022630]["condition"], "черновая отделка")
        self.assertEqual(self.items[1016589576]["furniture"], "полностью")
        self.assertEqual(self.items[1016589576]["seller_type"], "хозяин")

    def test_developer_layout(self):
        it = self.items[1015085208]
        self.assertEqual(it["is_layout"], 1)
        self.assertEqual(it["price"], 54_112_000)
        self.assertEqual((it["floor"], it["floors"], it["year"]), (None, 10, 2026))
        self.assertEqual(it["seller_type"], "застройщик")
        self.assertEqual(it["complex"], "Status 2.0")


class Preview(unittest.TestCase):
    def test_unknown_part_starts_description(self):
        p = parse_preview("жил. комплекс X, 2021 г.п., Продам, срочно, торг")
        self.assertEqual((p["complex"], p["year"]), ("X", 2021))
        self.assertEqual(p["description"], "Продам, срочно, торг")

    def test_pledged(self):
        p = parse_preview("В залоге, жил. комплекс Soho-17, монолитный дом, 2023 г.п., Продается")
        self.assertTrue(p["pledged"])
        self.assertEqual((p["complex"], p["house_type"], p["year"]), ("Soho-17", "монолитный", 2023))
        self.assertEqual(p["description"], "Продается")

    def test_condition_with_comma(self):
        p = parse_preview("жил. комплекс X, состояние: не новый, но аккуратный ремонт, потолки 3м., без мебели, Продам")
        self.assertEqual(p["condition"], "не новый, но аккуратный ремонт")
        self.assertEqual((p["ceiling"], p["furniture"], p["description"]), (3.0, "нет", "Продам"))

    def test_empty(self):
        self.assertEqual(parse_preview(None)["description"], "")


class Blocked(unittest.TestCase):
    def test_detect(self):
        self.assertTrue(is_blocked(BLOCKED))
        self.assertFalse(is_blocked(LIST))
        with self.assertRaises(BlockedError):
            parse_list_page(BLOCKED)


class Url(unittest.TestCase):
    def test_only_city_and_borders_come_from_url(self):
        cfg = {"search_url": "https://krisha.kz/map/prodazha/kvartiry/almaty/?das[live.rooms][]=2&zoom=15"
                             "&lat=43.2&lon=76.9&areas=p43.1,76.1,43.2,76.2,43.3,76.3",
               "filters": {"price_to": 100000000}}
        base, params = search_params(cfg)
        self.assertEqual(base, "https://krisha.kz/prodazha/kvartiry/almaty/")
        self.assertEqual(params, [("das[price][to]", "100000000"), ("areas", "p43.1,76.1,43.2,76.2,43.3,76.3")])
        self.assertEqual(len(polygon_from_params(params)), 3)
        self.assertTrue(build_list_url(base, params, 3).endswith("&page=3"))
        self.assertNotIn("page=", build_list_url(base, params, 1))

    def test_filters(self):
        self.assertEqual(filter_params({"price_from": None, "rooms": [], "extra": {}}), [])
        got = filter_params({"year_from": 2020, "rooms": [2, 5], "not_last_floor": True,
                             "building": [3], "extra": {"das[flat.priv_dorm]": 2}})
        self.assertEqual(got, [("das[house.year][from]", "2020"), ("das[live.rooms][]", "2"),
                               ("das[live.rooms][]", "5.100"), ("das[flat.building][]", "3"),
                               ("das[floor_not_last]", "1"), ("das[flat.priv_dorm]", "2")])

    def test_shipped_configs_load(self):
        for name in (None, "configs/golden_square.yaml"):
            base, params = search_params(load_config(name))
            self.assertEqual([k for k, _ in params], ["das[price][to]", "areas"])


if __name__ == "__main__":
    unittest.main()
