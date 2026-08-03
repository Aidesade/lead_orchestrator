# -*- coding: utf-8 -*-
"""Офлайн-регрессии RusProfile/Playwright: cookie, порог и revenue-sort."""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile


HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import rusprofile_playwright as RPW  # noqa: E402
import source_rusprofile as RP  # noqa: E402


class FakeSession:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def search(self, okved, revenue_from, max_pages=20, **_kwargs):
        self.calls.append({
            "okved": list(okved),
            "revenue_from": revenue_from,
            "max_pages": max_pages,
        })
        return list(self.items)


def _item(inn, revenue, *, region="Республика Татарстан", inactive=False):
    return {
        "inn": inn,
        "name": f"Компания {inn}",
        "finance_revenue": revenue,
        "region": region,
        "inactive": inactive,
        "link": f"/id/{inn}",
        "main_okved_id": "10.11",
    }


def main() -> int:
    rows = RPW.normalize_cookie_rows([
        {
            "name": "active",
            "value": "secret-must-not-be-logged",
            "domain": ".rusprofile.ru",
            "path": "/",
            "expiry": 2_000,
            "sameSite": "no_restriction",
        },
        {
            "name": "expired",
            "value": "old-secret",
            "expiry": 999,
        },
    ], now=1_000)
    assert len(rows) == 1
    assert rows[0]["name"] == "active"
    assert rows[0]["sameSite"] == "None"
    assert rows[0]["expires"] == 2_000

    industry = "_rusprofile_test"
    RP.INDUSTRY[industry] = {
        "label": "Тест",
        "okved": ["10.11"],
        "pain": "",
        "offer": "",
    }
    old_pages = os.environ.get("RUSPROFILE_MAX_PAGES")
    os.environ["RUSPROFILE_MAX_PAGES"] = "20"
    try:
        fake = FakeSession([
            _item("1000000001", 1_100_000_000),
            _item("1000000002", 9_000_000_000),
            _item("1000000003", 900_000_000),
            _item("1000000004", "5 000 000 000"),
            _item("1000000005", 8_000_000_000, inactive=True),
        ])
        leads = RP.harvest(
            [industry],
            min_revenue=1,
            per_industry=2,
            session=fake,
        )
    finally:
        RP.INDUSTRY.pop(industry, None)
        if old_pages is None:
            os.environ.pop("RUSPROFILE_MAX_PAGES", None)
        else:
            os.environ["RUSPROFILE_MAX_PAGES"] = old_pages

    assert len(fake.calls) == 1
    assert fake.calls[0]["revenue_from"] == RP.MIN_REVENUE_FLOOR
    assert fake.calls[0]["max_pages"] == RP.MAX_SEARCH_PAGES
    assert [lead["_inn"] for lead in leads] == ["1000000002", "1000000004"]
    assert [lead["_revenue"] for lead in leads] == [9_000_000_000, 5_000_000_000]
    assert all(lead["_revenue"] >= RP.MIN_REVENUE_FLOOR for lead in leads)
    marker_item = _item("1000000006", 2_000_000_000)
    marker_item["main_okved_id"] = "!~.~1.01"
    marker_lead = RP.item_to_lead(
        marker_item,
        {"label": "Тест", "pain": "", "offer": ""},
        industry,
    )
    assert marker_lead["niche"] == "Тест"

    with tempfile.TemporaryDirectory() as tmp:
        nested = pathlib.Path(tmp) / "new" / "phase1.json"
        RP._save(leads, nested)
        assert nested.is_file()

    print("test_rusprofile_playwright: OK — cookie safe, floor 1B, descending selection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
