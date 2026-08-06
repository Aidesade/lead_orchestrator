# -*- coding: utf-8 -*-
"""Офлайн-регрессии RusProfile/Playwright: cookie, порог, revenue-sort и разбор
карточки (руководитель под пейволом, учредители с отдельной страницы)."""
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


# Реальный текст карточки RusProfile, снятый 2026-08-06 при ЗАКРЫТЫХ контактах:
# телефоны и почта замаскированы, а руководитель и его ИНН читаются свободно.
# Ради этого и переносился разбор ЛПР выше пейвол-гарда — фикстура держит инвариант.
CARD_LOCKED = """Уставный капитал
22 000 руб.
Все реквизиты (ФНС / ПФР / ФСС / РОССТАТ)
Юридический адрес
400005, Волгоградская область, город Волгоград, ул. Им. Глазкова, д. 15, офис 9
Руководитель
Генеральный директор
Гребнева Татьяна Николаевна
с 25 ноября 2014 г.
Среднесписочная численность
░ сотрудников в ░░░░  1
Основной вид деятельности
Торговля оптовая твердым, жидким и газообразным топливом и подобными продуктами (46.71)
Контакты
Телефон
+7 (░░░░) ░░-░░-░░
Электронная почта
░░░░░░░░░░░@░░░░.ru
Сайт не указан
Для просмотра контактов оформите профессиональный доступ

Генеральный директор ООО "Регион-Нефть" - Гребнева Татьяна Николаевна (ИНН 343900088737).
"""

# Страница /founders/<id> без профессионального доступа — состав закрыт целиком.
FOUNDERS_LOCKED = """Учредители
Уставный капитал: 22 000 руб.
Актуальные (5)
░░░░░░░░ ░░░░░░░ ░░░░░░░░░░
Период: с 25.06.2015 по настоящее время
Доля: ░░░░░░░░░░ руб. (░░░░)
ИНН: ░░░░░░░░░░░░
░░░░░░░░ ░░░░░░░░░ ░░░░░░░░░░░
Период: с 25.06.2015 по настоящее время
Доля: ░░░░░░░░░ руб. (░░░░)
ИНН: ░░░░░░░░░░░░
"""

# Та же вёрстка с открытым доступом: структура проверена на живой странице,
# подставлены значения вместо маски.
FOUNDERS_OPEN = """Учредители
Уставный капитал: 22 000 руб.
Актуальные (2)
Гребнева Татьяна Николаевна
Период: с 25.06.2015 по настоящее время
Доля: 11 833 руб. (53,79%)
Руководитель: 1 организация
Связи: 2 организации
Волгоградская область
ИНН: 343900088737
Филиппов Александр Анатольевич
Период: с 21.09.2015 по настоящее время
Доля: 4 004 руб. (18,2%)
Связи: 1 организация
ИНН: 344101234567
Исторические (1)
Орлова Виктория Павловна
Период: с 25.06.2015 по 26.10.2015
ИНН: 344109876543
"""


def _check_card_parsers() -> None:
    facts = RPW.card_facts(CARD_LOCKED)
    # главное: ЛПР достаётся из карточки, у которой контакты под замком
    assert facts["_ceo_fio"] == "Гребнева Татьяна Николаевна", facts
    assert facts["_ceo_post"] == "Генеральный директор", facts
    # 12 цифр — ИНН физлица; ИНН самой компании (10 цифр) сюда попасть не должен
    assert facts["_ceo_inn"] == "343900088737", facts
    assert facts["_capital"] == "22 000 руб.", facts

    masked_card = CARD_LOCKED.replace("Гребнева Татьяна Николаевна", "░░░░░░░ ░░░░░░")
    masked = RPW.card_facts(masked_card)
    assert "_ceo_fio" not in masked, "замаскированное ФИО нельзя выдавать за прочитанное"

    locked = RPW.parse_founders(FOUNDERS_LOCKED)
    assert locked["locked"] is True, locked
    assert locked["founders"] == [], "закрытый состав — это не пустой состав"

    opened = RPW.parse_founders(FOUNDERS_OPEN)
    assert opened["locked"] is False, opened
    assert [f["fio"] for f in opened["founders"]] == [
        "Гребнева Татьяна Николаевна", "Филиппов Александр Анатольевич"], opened
    assert opened["founders"][0]["share"] == "11 833 руб. (53,79%)", opened
    assert opened["founders"][0]["inn"] == "343900088737", opened
    # исторические владельцы отделены: писать бывшему учредителю незачем
    assert [f["fio"] for f in opened["historic"]] == ["Орлова Виктория Павловна"], opened

    assert RPW.parse_founders("") == {"founders": [], "historic": [], "locked": False}


def main() -> int:
    _check_card_parsers()

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
