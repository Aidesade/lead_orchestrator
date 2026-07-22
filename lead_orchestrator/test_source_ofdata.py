# -*- coding: utf-8 -*-
"""Офлайн-регрессия источника OfData: ни одного реального API-запроса."""
import json
import os
import sys
import tempfile
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from project_env import load_project_env
import source_ofdata as OD


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class _FakeClient:
    def __init__(self):
        self.requests_used = 0
        self.balance = 100.0
        self.records = [
            {
                "ИНН": "1000000001", "ОГРН": "1000000000001", "НаимСокр": "АО Ровно",
                "Статус": "Действует", "РегионКод": "16", "ЮрАдрес": "Казань",
                "ОКВЭД": "Строительство дорог",
            },
            {
                "ИНН": "1000000002", "ОГРН": "1000000000002", "НаимСокр": "АО Ниже",
                "Статус": "Действует", "РегионКод": "16", "ЮрАдрес": "Казань",
                "ОКВЭД": "Строительство дорог",
            },
            {
                "ИНН": "1000000003", "ОГРН": "1000000000003", "НаимСокр": "АО Выше",
                "Статус": "Действует", "РегионКод": "16", "ЮрАдрес": "Казань",
                "ОКВЭД": "Строительство дорог",
            },
            {
                "ИНН": "1000000004", "ОГРН": "1000000000004", "НаимСокр": "АО Без отчета",
                "Статус": "Действует", "РегионКод": "16", "ЮрАдрес": "Казань",
                "ОКВЭД": "Строительство дорог",
            },
        ]
        self.finance_data = {
            "1000000001": {"2025": {"2110": {"СумОтч": 1_000_000_000}}},
            "1000000002": {"2025": {"2110": 999_999_999}},
            "1000000003": {
                "2025": {"2100": 1},
                "2024": {"2110": "2 500 000 000"},
            },
            "1000000004": {"2025": {"2100": 10}},
        }
        self.company_data = {
            "1000000001": {
                "НаимСокр": "АО Ровно",
                "Статус": {"Наим": "Действующая"},
                "Регион": {"Код": "16", "Наим": "Республика Татарстан"},
                "ЮрАдрес": {"АдресРФ": "420000, г. Казань"},
                "ОКВЭД": {"Код": "42.11", "Наим": "Строительство дорог"},
                "Руковод": [{"ФИО": "Иванов Иван Иванович", "НаимДолжн": "Директор"}],
                "Контакты": {
                    "Тел": ["+7 843 000-00-01"],
                    "Емэйл": ["info@rovno.example", "sales@rovno.example"],
                    "ВебСайт": "https://rovno.example",
                },
            },
            "1000000003": {
                "НаимСокр": "АО Выше",
                "Руковод": [{"ФИО": "Петров Петр Петрович"}],
                "Контакты": {
                    "Тел": ["+7 843 000-00-03"],
                    "Емэйл": ["partner@vyshe.example"],
                    "ВебСайт": "https://vyshe.example",
                },
            },
        }

    def by_okved(self, okved, region_code=None, max_pages=50, active=True):
        self.requests_used += 1
        if okved == "42.11" and (region_code is None or region_code == "16") and active:
            yield from self.records

    def finances(self, inn):
        self.requests_used += 1
        return self.finance_data[inn]

    def company(self, inn):
        self.requests_used += 1
        return self.company_data.get(inn, {})

    def usage_line(self):
        return f"mock {self.requests_used}"


class OfDataTests(unittest.TestCase):
    def test_project_env_loads_silently_without_overriding_process(self):
        name = "OFDATA_TEST_ONLY"
        previous = os.environ.get(name)
        try:
            os.environ.pop(name, None)
            with tempfile.TemporaryDirectory() as temp:
                env_file = Path(temp) / ".env"
                env_file.write_text(
                    "# test\nOFDATA_TEST_ONLY=from-file\nINVALID-NAME=no\n",
                    encoding="utf-8",
                )
                loaded = load_project_env(env_file)
                self.assertEqual(loaded, (name,))
                self.assertEqual(os.environ[name], "from-file")
                os.environ[name] = "from-process"
                self.assertEqual(load_project_env(env_file), ())
                self.assertEqual(os.environ[name], "from-process")
        finally:
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

    def test_latest_revenue_supports_normal_and_extended_shapes(self):
        self.assertEqual(
            OD.extract_latest_revenue({"2024": {"2110": 1_200_000_000}}),
            (1_200_000_000, 2024),
        )
        self.assertEqual(
            OD.extract_latest_revenue({
                "2025": {"2110": {"СумОтч": 2_300_000_000}},
                "2024": {"2110": {"СумОтч": 1}},
            }),
            (2_300_000_000, 2025),
        )
        self.assertEqual(
            OD.extract_latest_revenue({"2025": {"2100": 7}, "2024": {"2110": "1 500 000 000"}}),
            (1_500_000_000, 2024),
        )
        self.assertTrue(OD.finances_are_paywalled({
            "2024": "Недоступно для бесплатного тарифа",
            "2025": "Недоступно для бесплатного тарифа",
        }))
        self.assertFalse(OD.finances_are_paywalled({"2025": {"2110": 1}}))

    def test_key_is_posted_not_put_in_url(self):
        seen = {}

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            seen["timeout"] = timeout
            seen["body"] = urllib.parse.parse_qs(request.data.decode("utf-8"))
            return _Response({
                "data": {"СтрВсего": 1, "СтрТекущ": 1, "Записи": []},
                "meta": {"status": "ok", "today_request_count": 4, "balance": 99.5},
            })

        client = OD.OfDataClient(api_key="SECRET-OFDATA", pause=0, retries=1, timeout=7)
        with mock.patch.object(OD.urllib.request, "urlopen", fake_urlopen):
            list(client.by_okved("42.11", region_code="16", max_pages=1))
        self.assertEqual(seen["method"], "POST")
        self.assertNotIn("SECRET-OFDATA", seen["url"])
        self.assertEqual(seen["body"]["key"], ["SECRET-OFDATA"])
        self.assertEqual(seen["body"]["query"], ["42.11"])
        self.assertEqual(seen["body"]["active"], ["true"])
        self.assertEqual(seen["timeout"], 7)

    def test_api_error_never_echoes_key(self):
        def fake_urlopen(_request, timeout):
            del timeout
            return _Response({
                "data": {},
                "meta": {"status": "error", "message": "bad key SECRET-OFDATA"},
            })

        client = OD.OfDataClient(api_key="SECRET-OFDATA", pause=0, retries=1)
        with mock.patch.object(OD.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(OD.OfDataSourceError) as raised:
                client.finances("7700000000")
        self.assertNotIn("SECRET-OFDATA", str(raised.exception))
        self.assertIn("<скрыто>", str(raised.exception))

    def test_harvest_clamps_floor_and_enriches_only_kept(self):
        fake_module = types.ModuleType("source_rusprofile")
        fake_module.INDUSTRY = {
            "_test": {
                "label": "Тестовая отрасль", "okved": ["42.11"],
                "pain": "pain", "offer": "offer",
            },
        }
        previous = sys.modules.get("source_rusprofile")
        sys.modules["source_rusprofile"] = fake_module
        try:
            client = _FakeClient()
            # Переданный порог 1 ₽ обязан быть поднят до 1 млрд; ровно 1 млрд проходит.
            leads = OD.harvest(
                ["_test"], min_revenue=1, per_industry=10, region="16",
                max_candidates=10, client=client,
            )
        finally:
            if previous is None:
                sys.modules.pop("source_rusprofile", None)
            else:
                sys.modules["source_rusprofile"] = previous

        self.assertEqual([lead["_inn"] for lead in leads], ["1000000003", "1000000001"])
        self.assertEqual(leads[0]["_revenue"], 2_500_000_000)
        self.assertEqual(leads[0]["_revenue_year"], 2024)
        self.assertEqual(leads[1]["_revenue"], OD.MIN_REVENUE_FLOOR)
        self.assertTrue(all(lead["_revenue"] >= OD.MIN_REVENUE_FLOOR for lead in leads))

        stats = OD.ofdata_contacts_pass(client, leads, cap=0, log=lambda _message: None)
        self.assertEqual(stats["used"], 2)
        self.assertEqual(leads[0]["contact_person"], "Петров Петр Петрович")
        self.assertEqual(leads[1]["website"], "https://rovno.example")
        self.assertEqual(leads[1]["email"], "sales@rovno.example")
        self.assertEqual(leads[1]["_status"], "Действующая")
        self.assertIn("/company", leads[1]["source"])

    def test_free_tariff_falls_back_to_girbo(self):
        fake_module = types.ModuleType("source_rusprofile")
        fake_module.INDUSTRY = {
            "_test": {
                "label": "Тестовая отрасль", "okved": ["42.11"],
                "pain": "pain", "offer": "offer",
            },
        }
        previous = sys.modules.get("source_rusprofile")
        sys.modules["source_rusprofile"] = fake_module
        client = _FakeClient()
        client.balance = 0.0
        client.records = client.records[:2]
        client.finance_data = {
            lead["ИНН"]: {"2025": "Недоступно для бесплатного тарифа"}
            for lead in client.records
        }

        def fake_add_revenue(leads, log):
            del log
            leads[0]["_revenue"] = 1_500_000_000
            leads[0]["_revenue_src"] = "girbo"
            leads[0]["_revenue_source_url"] = "https://bo.nalog.gov.ru/"
            leads[1]["_revenue"] = 500_000_000

        try:
            with mock.patch.dict(os.environ, {"OFDATA_REVENUE_SOURCE": "auto"}):
                with mock.patch("revenue_enrich.add_revenue", fake_add_revenue):
                    leads = OD.harvest(
                        ["_test"], min_revenue=OD.MIN_REVENUE_FLOOR,
                        per_industry=10, client=client,
                    )
        finally:
            if previous is None:
                sys.modules.pop("source_rusprofile", None)
            else:
                sys.modules["source_rusprofile"] = previous

        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["_revenue_src"], "girbo")
        self.assertIn("ГИР БО", leads[0]["source"])

    def test_short_okved_reuses_checko_expansion(self):
        codes, rejected = OD.usable_okved(["42.1"])
        self.assertFalse(rejected)
        self.assertIn("42.11", codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
