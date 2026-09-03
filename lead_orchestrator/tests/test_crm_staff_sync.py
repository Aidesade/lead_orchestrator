# -*- coding: utf-8 -*-
"""Офлайн-регрессии свода ССЧ для CRM (`crm_staff_sync`). Сеть, CRM и браузер НЕ нужны.

Что охраняется:
  * приоритет источников: численность из партии > кэш «не нашлось», свежий год > старый;
  * в RusProfile идём только без показателя за 2025 и без свежего кэша;
  * вердикты по правилу лидгена; в purge по умолчанию — только подтверждённое «below»;
  * сбой RusProfile не кэшируется, пустой XHR добирается карточкой.

Запуск:  py test_crm_staff_sync.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
APP = HERE.parent / "app"
sys.path.insert(0, str(APP))

import crm_staff_sync as CSS          # noqa: E402
import source_rusprofile as SR        # noqa: E402

INN_A, INN_B, INN_C, INN_D = "6234065445", "7707083893", "7736050003", "1650000000"


def check_sources_priority():
    with tempfile.TemporaryDirectory() as tmp:
        old = json.dumps([
            {"name": "АО Старое", "_inn": INN_A, "_staff_count": 30, "_staff_year": 2024},
            {"name": "ООО Без штата", "_inn": INN_B, "_staff_count": None},
            {"name": "мусор", "_inn": "123"},
        ], ensure_ascii=False)
        new = json.dumps([
            {"name": "АО Старое", "_inn": INN_A, "_staff_count": "1 250", "_staff_year": "2025"},
            {"name": "ПАО Второе", "_inn": INN_C, "_staff_count": 80, "_staff_year": 2025},
        ], ensure_ascii=False)
        pathlib.Path(tmp, "leads_a.json").write_text(old, encoding="utf-8")
        pathlib.Path(tmp, "leads_b.json").write_text(new, encoding="utf-8")
        pathlib.Path(tmp, "broken.json").write_text("{не json", encoding="utf-8")
        local = CSS.staff_from_leads_dir(tmp)
    assert set(local) == {INN_A, INN_B, INN_C}, local
    assert (local[INN_A]["count"], local[INN_A]["year"]) == (1250, 2025), local[INN_A]
    assert local[INN_B]["count"] is None and local[INN_B]["name"] == "ООО Без штата"

    fresh = CSS._now()
    cache = {
        INN_B: {"count": 20, "year": 2025, "name": "", "found": True,
                "source": "rusprofile:xhr", "checked_at": fresh},
        INN_C: {"count": None, "year": None, "name": "", "found": False,
                "source": "rusprofile:none", "checked_at": fresh},
        INN_D: {"count": 60, "year": 2024, "found": True, "checked_at": "2020-01-01T00:00:00Z"},
    }
    # Партия побеждает кэш «не нашлось»: показатель с карточки не отменяется сегодняшним 404.
    assert CSS.best_known(INN_C, local, cache)["count"] == 80
    assert CSS.best_known(INN_B, local, cache)["count"] == 20
    assert CSS.best_known("0000000000", local, cache) is None
    assert not CSS.needs_lookup(INN_A, local, cache, 30), "есть 2025 — в RusProfile не идём"
    assert not CSS.needs_lookup(INN_B, local, cache, 30), "свежий кэш — не переспрашиваем"
    assert CSS.needs_lookup(INN_D, local, cache, 30), "старый год и протухший кэш — идём"
    assert CSS.needs_lookup("0000000000", local, cache, 30)
    print("  ✓ источники: партия > кэш «не нашлось», свежий год > старый, кэш бережёт RusProfile")


def check_verdicts_and_targets():
    local = {INN_A: {"count": 1250, "year": 2025, "name": "АО Большое", "source": "x"},
             INN_B: {"count": 49, "year": 2025, "name": "ООО Малое", "source": "x"},
             INN_C: {"count": 300, "year": 2023, "name": "ПАО Давно", "source": "x"}}
    cache = {INN_D: {"count": None, "year": None, "found": False, "source": "rusprofile:none",
                     "checked_at": CSS._now()}}
    rows = CSS.build_rows([INN_A, INN_B, INN_C, INN_D, "5000000000"], local, cache, 50)
    assert {inn: row["verdict"] for inn, row in rows.items()} == {
        INN_A: "ok", INN_B: "below", INN_C: "unknown", INN_D: "not_found",
        "5000000000": "unknown"}, rows
    assert rows[INN_B]["name"] == "ООО Малое"
    assert CSS.purge_targets(rows) == [INN_B], "по умолчанию удаляется только подтверждённое below"
    # С --purge-unknown уходят и «нет показателя за 2025», и «не найдено», но компания
    # с ССЧ ≥ порога по последнему известному году (ПАО Давно: 300 в 2023) остаётся.
    assert set(CSS.purge_targets(rows, include_unknown=True, min_staff=50)) == {
        INN_B, INN_D, "5000000000"}
    rows[INN_C]["count"] = 49
    assert INN_C in CSS.purge_targets(rows, include_unknown=True, min_staff=50), \
        "старый год ниже порога — удаляется"
    assert CSS.verdict_for({"count": 50, "year": 2025, "found": True}, 50) == "ok", "ровно 50 проходит"
    print("  ✓ вердикты по правилу лидгена; purge без флага — только below")


class FakeSession:
    def __init__(self, items, facts=None, fail=()):
        self.items, self.facts, self.fail = items, facts or {}, set(fail)
        self.card_calls = []

    def card_by_inn(self, inn):
        if inn in self.fail:
            raise RuntimeError("timeout")
        return self.items.get(inn)

    def card_facts_by_url(self, link, *, expected_inn=""):
        self.card_calls.append(link)
        facts = self.facts.get(expected_inn)
        if facts is None:
            raise RuntimeError("RusProfileCardIncomplete")
        return facts


def check_rusprofile_lookup():
    session = FakeSession(
        items={
            INN_A: {"name": "АО Быстрое", "sshr": "203", "sshr_year": "2025", "link": "/id/1"},
            INN_B: {"name": "ООО Тихое", "sshr": None, "sshr_year": None, "link": "/id/2"},
            INN_C: {"name": "ПАО Старое", "sshr": "70", "sshr_year": "2024", "link": "/id/3"},
        },
        facts={INN_B: {"_staff_count": 45, "_staff_year": 2025}},
        fail={INN_D},
    )
    log = []
    got = CSS.lookup_rusprofile(session, INN_A, log=log.append)
    assert (got["count"], got["year"], got["source"], got["found"]) == (203, 2025, "rusprofile:xhr", True), got
    assert session.card_calls == [], "есть sshr за 2025 — карточка не открывается"
    got = CSS.lookup_rusprofile(session, INN_B, log=log.append)
    assert (got["count"], got["year"], got["source"]) == (45, 2025, "rusprofile:card"), got
    got = CSS.lookup_rusprofile(session, INN_C, log=log.append)
    assert (got["count"], got["year"]) == (70, 2024) and got["found"], "карточка не разобрана — остаётся XHR"
    assert CSS.lookup_rusprofile(session, INN_D, log=log.append) is None, "сбой — None, не «не найдено»"
    assert CSS.lookup_rusprofile(session, "5000000000", log=log.append)["found"] is False
    assert CSS.verdict_for(CSS.lookup_rusprofile(session, "5000000000"), 50) == "not_found"
    assert any("timeout" in line for line in log), log
    print("  ✓ RusProfile: XHR без карточки, пустой XHR добирается карточкой, сбой не кэшируется")


def check_cache_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "orq_cache", "staff_index.json")
        assert CSS.load_cache(path) == {}
        CSS.save_cache({INN_A: {"count": 1, "year": 2025, "checked_at": CSS._now()}}, path)
        assert CSS.load_cache(path)[INN_A]["count"] == 1
        assert not os.path.exists(path + ".tmp"), "запись атомарная"
    print("  ✓ кэш: атомарная запись, отсутствие файла — пустой словарь")


def main():
    print("свод ССЧ для CRM — офлайн-регрессии:")
    assert SR.STAFF_YEAR == 2025
    check_sources_priority()
    check_verdicts_and_targets()
    check_rusprofile_lookup()
    check_cache_roundtrip()
    print("test_crm_staff_sync: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
