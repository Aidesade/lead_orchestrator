# -*- coding: utf-8 -*-
"""Офлайн-регрессия источника ГИР БО: ни одного реального запроса.

Проверяем то, ради чего источник и появился: отрасль определяется по ОКВЭД
ПРЕФИКСНО (иначе теряются компании с детализированными кодами), выручка
пересчитывается из тысяч в рубли, фильтр года отсекает старую отчётность, а
предел offset не даёт уйти в бесконечную пагинацию.
"""
import os
import sys
import types
import unittest
from unittest import mock

# Тесты живут в lead_orchestrator/tests, код пайплайна — в соседней app/.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import source_girbo as SG  # noqa: E402


def _record(inn, name, okved, gain_thousands, period="2025", region="ТАТАРСТАН"):
    return {
        "id": f"id-{inn}", "inn": inn, "ogrn": "1" + inn + "00", "shortName": name,
        "region": region, "city": "КАЗАНЬ", "street": "БАУМАНА", "house": "1",
        "okved2": okved,
        "bfo": {"period": period, "gainSum": gain_thousands},
    }


class GirboTests(unittest.TestCase):
    def setUp(self):
        self.industry = types.ModuleType("source_rusprofile")
        self.industry.INDUSTRY = {
            "processing": {"label": "Переработка", "okved": ["19.2", "20.1"],
                           "pain": "p", "offer": "o"},
            "manufacturing": {"label": "Обработка", "okved": ["29.1"],
                              "pain": "p", "offer": "o"},
            "trade": {"label": "Торговля", "okved": ["47.1"], "pain": "p", "offer": "o"},
        }
        self._previous = sys.modules.get("source_rusprofile")
        sys.modules["source_rusprofile"] = self.industry

    def tearDown(self):
        if self._previous is None:
            sys.modules.pop("source_rusprofile", None)
        else:
            sys.modules["source_rusprofile"] = self._previous

    def test_industry_matched_by_okved_prefix(self):
        """19.20.1 (Татнефть) и 29.10.4 (КАМАЗ) обязаны попадать в свои отрасли.

        Точный матч, как у API источников, их теряет — из-за этого и понадобился
        отдельный источник."""
        industry_map = self.industry.INDUSTRY
        self.assertEqual(SG.industry_of("19.20.1", industry_map), "processing")
        self.assertEqual(SG.industry_of("29.10.4", industry_map), "manufacturing")
        self.assertEqual(SG.industry_of("47.11.3", industry_map), "trade")
        self.assertEqual(SG.industry_of("68.32", industry_map), "")
        self.assertEqual(SG.industry_of("", industry_map), "")

    def test_revenue_converted_from_thousands(self):
        lead = SG.record_to_lead(_record("1600000001", "АО Тест", "19.20.1", 1_379_900_000))
        self.assertEqual(lead["_revenue"], 1_379_900_000_000)
        self.assertEqual(lead["_revenue_year"], "2025")
        self.assertEqual(lead["_revenue_src"], "girbo")
        self.assertIn("bo.nalog.gov.ru", lead["_revenue_source_url"])
        self.assertEqual(lead["_inn"], "1600000001")

    def test_harvest_filters_threshold_year_industry_and_dedups(self):
        rows = [
            _record("1600000001", "АО Нефтехим", "19.20.1", 2_000_000),      # 2 млрд, 2025
            _record("1600000002", "АО Мелкий", "19.20.1", 500_000),          # 0.5 млрд — ниже
            _record("1600000003", "АО Старый", "29.10.4", 5_000_000, "2023"),  # не тот год
            _record("1600000004", "АО Чужой", "68.32", 9_000_000),           # вне отраслей
            _record("1600000005", "АО Иногородний", "29.10.4", 3_000_000,
                    region="САМАРСКАЯ"),                                     # другой регион
            _record("1600000001", "АО Нефтехим", "19.20.1", 2_000_000),      # дубль по ИНН
            _record("1600000006", "АО Машзавод", "29.10.4", 4_000_000),      # 4 млрд, 2025
        ]

        with mock.patch.object(SG, "region_prefixes", return_value=[("1600", len(rows))]):
            with mock.patch.object(SG, "iter_organizations", return_value=iter(rows)):
                with mock.patch.object(SG.time, "sleep", lambda _s: None):
                    leads = SG.harvest(
                        region="16", min_revenue=1_000_000_000, year="2025",
                        industries=["processing", "manufacturing", "trade"],
                        log=lambda _m: None)

        self.assertEqual([lead["_inn"] for lead in leads], ["1600000006", "1600000001"])
        self.assertEqual(leads[0]["_revenue"], 4_000_000_000)   # отсортировано по убыванию
        self.assertEqual(leads[0]["_industry"], "manufacturing")
        self.assertEqual(leads[1]["_industry"], "processing")

    def test_harvest_limit_keeps_largest(self):
        rows = [
            _record("1600000001", "АО Первый", "19.20.1", 2_000_000),
            _record("1600000006", "АО Второй", "29.10.4", 4_000_000),
        ]
        with mock.patch.object(SG, "region_prefixes", return_value=[("1600", 2)]):
            with mock.patch.object(SG, "iter_organizations", return_value=iter(rows)):
                with mock.patch.object(SG.time, "sleep", lambda _s: None):
                    leads = SG.harvest(region="16", year="2025", limit=1,
                                       log=lambda _m: None)
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["_inn"], "1600000006")

    def test_pagination_stops_at_offset_limit(self):
        """За offset 10000 ГИР БО отдаёт HTTP 500 — цикл обязан остановиться сам."""
        calls = []

        def fake_get(params, **_kwargs):
            calls.append(params["page"])
            return {"content": [_record(f"16000000{params['page']:02d}", "АО", "19.20.1", 9)]}

        with mock.patch.object(SG, "_get", fake_get):
            with mock.patch.object(SG.time, "sleep", lambda _s: None):
                rows = list(SG.iter_organizations("1600", log=lambda _m: None))

        self.assertEqual(len(rows), SG.MAX_OFFSET // SG.PAGE_SIZE)
        self.assertEqual(max(calls) * SG.PAGE_SIZE, SG.MAX_OFFSET - SG.PAGE_SIZE)

    def test_http_500_is_not_an_error_but_end_of_data(self):
        with mock.patch.object(SG.urllib.request, "urlopen",
                               side_effect=SG.urllib.error.HTTPError(
                                   "u", 500, "err", {}, None)):
            self.assertEqual(SG._get({"query": "1600", "page": 0}), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
