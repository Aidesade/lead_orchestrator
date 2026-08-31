# -*- coding: utf-8 -*-
"""Офлайн-регрессии RusProfile/Playwright: cookie, порог, revenue-sort и разбор
карточки (руководитель под пейволом, учредители с отдельной страницы)."""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
from types import SimpleNamespace


HERE = pathlib.Path(__file__).resolve().parent
APP = HERE.parent / "app"          # код пайплайна лежит рядом, в app/
sys.path.insert(0, str(APP))

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

# Живой блок карточки RusProfile 2026-08-16. В выдаче точная выручка приходит
# числом, а годы выручки и численности нужно подтверждать на карточке.
CARD_METRICS = """Среднесписочная численность
250 сотрудников в 2025 году  6
Среднемесячная зарплата
85 734 руб в 2025 году
Финансы
Основные показатели за 2025 год:
Выручка
2,5 млрд руб.
↑+11 %
Прибыль
7,9 млн руб.
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

    metrics = RPW.card_facts(CARD_METRICS)
    assert metrics["_staff_count"] == 250, metrics
    assert metrics["_staff_year"] == 2025, metrics
    assert metrics["_revenue_year"] == 2025, metrics
    assert metrics["_revenue_display"] == "2,5 млрд руб.", metrics

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


def _check_staff_filter_body() -> None:
    """Оба browser-backend должны отправлять серверу один и тот же диапазон."""
    for cls in (RPW.RusProfilePlaywrightSession, RP.RusProfileSession):
        session = object.__new__(cls)
        captured = []
        session._post_retry = lambda body, log=None, **_kwargs: (
            captured.append(body) or {
                "success": True,
                "data": {"items": [], "total_count": 0,
                         "pagination": {"page_count": 1}},
            })
        if cls is RPW.RusProfilePlaywrightSession:
            session.page = SimpleNamespace(url=RPW.ADV_URL)
            session._goto = lambda *_args, **_kwargs: None
            session.log = lambda *_args: None
        session.search(
            ["10.11"], 2_000_000_000, max_pages=1, pause=0,
            staff_from=240, staff_to=260, log=lambda *_args: None,
        )
        assert captured, cls
        assert captured[0]["finance_revenue_from"] == "2000000000", captured[0]
        assert captured[0]["sshr_from"] == "240", captured[0]
        assert captured[0]["sshr_to"] == "260", captured[0]

    session = object.__new__(RPW.RusProfilePlaywrightSession)
    captured = []
    session.page = SimpleNamespace(url=RPW.ADV_URL)
    session._goto = lambda *_args, **_kwargs: None
    session.log = lambda *_args: None
    session._post_retry = lambda body, log=None, **_kwargs: captured.append(body)
    try:
        session.search([], 2_000_000_000, max_pages=20, deadline=0,
                       log=lambda *_args: None)
    except RPW.RusProfileDeadlineReached:
        pass
    else:
        raise AssertionError("истёкший deadline был выдан за пустую выдачу")
    assert captured == [], "истёкший deadline не должен читать выдачу"


def _check_card_url_and_search_failure() -> None:
    assert RPW.canonical_card_url("/id/123") == "https://www.rusprofile.ru/id/123"
    assert RPW.canonical_card_url("https://rusprofile.ru/id/123").endswith("/id/123")
    for unsafe in ("http://127.0.0.1/id/1", "https://evil.test/id/1",
                   "https://www.rusprofile.ru/id/1?next=x", "/company/1"):
        try:
            RPW.canonical_card_url(unsafe)
        except RPW.RusProfilePlaywrightError:
            pass
        else:
            raise AssertionError(f"SSRF/неканоническая ссылка принята: {unsafe}")

    session = object.__new__(RPW.RusProfilePlaywrightSession)
    session.log = lambda *_args: None
    session.timeout_ms = 1_000
    session._post = lambda _body, **_kwargs: (_ for _ in ()).throw(OSError("offline"))
    session._goto = lambda *_args, **_kwargs: None
    try:
        session._post_retry({"page": 3}, log_fn=lambda *_args: None)
    except RPW.RusProfilePlaywrightError as exc:
        assert "стр.3" in str(exc)
    else:
        raise AssertionError("сбой страницы поиска был выдан за исчерпание источника")

    session._post = lambda _body, **_kwargs: (_ for _ in ()).throw(
        RPW.RusProfileDeadlineReached("deadline в XHR"))
    try:
        session._post_retry({"page": 4}, log_fn=lambda *_args: None)
    except RPW.RusProfileDeadlineReached:
        pass
    except RPW.RusProfilePlaywrightError as exc:
        raise AssertionError("deadline XHR потерял тип timeout") from exc
    else:
        raise AssertionError("deadline XHR был проигнорирован")

    opened = []

    class Body:
        @staticmethod
        def inner_text():
            return "Актуальные (0)"

    founders = object.__new__(RPW.RusProfilePlaywrightSession)
    founders._goto = lambda url, **_kwargs: opened.append(url)
    founders.page = SimpleNamespace(
        content=lambda: "<html></html>", locator=lambda _selector: Body())
    result = founders.founders_by_url("/id/123")
    assert result["founders"] == []
    assert opened == ["https://www.rusprofile.ru/founders/123"]

    malformed = object.__new__(RPW.RusProfilePlaywrightSession)
    malformed.page = SimpleNamespace(url=RPW.ADV_URL)
    malformed.log = lambda *_args: None
    malformed._goto = lambda *_args, **_kwargs: None
    malformed._post_retry = lambda *_args, **_kwargs: {"success": True}
    try:
        malformed.search(["10.11"], 2_000_000_000, max_pages=1, pause=0)
    except RPW.RusProfilePlaywrightError as exc:
        assert "схем" in str(exc)
    else:
        raise AssertionError("malformed-success был выдан за пустую последнюю страницу")

    partial = object.__new__(RPW.RusProfilePlaywrightSession)
    partial.page = SimpleNamespace(url=RPW.ADV_URL)
    partial.log = lambda *_args: None
    partial._goto = lambda *_args, **_kwargs: None
    responses = iter((
        {"success": True, "data": {
            "items": [{"inn": "1650000049"}], "total_count": 100,
            "pagination": {"page_count": 2}}},
        {"success": True, "data": {
            "items": [], "total_count": 100,
            "pagination": {"page_count": 2}}},
    ))
    partial._post_retry = lambda *_args, **_kwargs: next(responses)
    try:
        partial.search(["10.11"], 2_000_000_000, max_pages=2, pause=0,
                       log=lambda *_args: None)
    except RPW.RusProfilePlaywrightError as exc:
        assert "пуст" in str(exc).lower() or "частич" in str(exc).lower()
    else:
        raise AssertionError("преждевременно пустая страница вернула partial results")

    changed = object.__new__(RPW.RusProfilePlaywrightSession)
    changed.page = SimpleNamespace(url=RPW.ADV_URL)
    changed.log = lambda *_args: None
    changed._goto = lambda *_args, **_kwargs: None
    changed_responses = iter((
        {"success": True, "data": {
            "items": [{"inn": "1650000049"}], "total_count": 2,
            "pagination": {"page_count": 2}}},
        {"success": True, "data": {
            "items": [{"inn": "1650000056"}], "total_count": 3,
            "pagination": {"page_count": 3}}},
    ))
    changed._post_retry = lambda *_args, **_kwargs: next(changed_responses)
    try:
        changed.search(["10.11"], 2_000_000_000, max_pages=2, pause=0,
                       log=lambda *_args: None)
    except RPW.RusProfilePlaywrightError as exc:
        assert "пагинац" in str(exc).lower() or "схем" in str(exc).lower()
    else:
        raise AssertionError("изменившаяся pagination metadata вернула partial results")


def _check_goto_survives_site_redirect() -> None:
    """Собственный редирект RusProfile — не сбой источника.

    Устаревший id карточки уводит на актуальный, и Playwright сообщает об этом
    ИСКЛЮЧЕНИЕМ, а не переходом. Раньше три такие подряд останавливали строгий
    добор fail-closed «карточки не разобраны» — источник при этом был жив.
    Настоящий сбой (таймаут, бан) обязан лететь наружу с первой попытки."""
    calls = []

    class Page:
        def __init__(self, fail_times):
            self.left = fail_times

        def goto(self, url, **_kwargs):
            calls.append(url)
            if self.left > 0:
                self.left -= 1
                raise RuntimeError(
                    f'Page.goto: Navigation to "{url}" is interrupted '
                    'by another navigation to "/id/999"')

        def wait_for_timeout(self, _ms):
            return None

        @staticmethod
        def title():
            return "Компания"

    session = object.__new__(RPW.RusProfilePlaywrightSession)
    session.timeout_ms = 1_000
    session.page = Page(fail_times=1)
    session._goto("https://www.rusprofile.ru/id/1")
    assert len(calls) == 2, calls

    calls.clear()
    session.page = Page(fail_times=9)
    try:
        session._goto("https://www.rusprofile.ru/id/2")
    except RuntimeError:
        assert len(calls) == 3, "повторов должно быть ровно три, а не бесконечность"
    else:
        raise AssertionError("бесконечный редирект обязан оставаться ошибкой")

    calls.clear()
    session.page = Page(fail_times=0)
    session.page.goto = lambda url, **_k: (calls.append(url), (_ for _ in ()).throw(
        RuntimeError("Timeout 30000ms exceeded")))[0]
    try:
        session._goto("https://www.rusprofile.ru/id/3")
    except RuntimeError as exc:
        assert "Timeout" in str(exc) and len(calls) == 1,             "настоящий сбой нельзя повторять и прятать"
    else:
        raise AssertionError("таймаут навигации проглочен")
    print("  OK: чужой редирект переживается, таймаут/бан летит наружу сразу")


def _check_card_by_inn() -> None:
    """Поиск карточки по ИНН: сверка по цифрам, статус-фильтр, отсутствие != ошибка."""
    sent = []

    session = object.__new__(RPW.RusProfilePlaywrightSession)
    session.page = SimpleNamespace(url="https://www.rusprofile.ru/search-advanced")
    session._goto = lambda *_a, **_k: None
    session._post = lambda body, **_k: (sent.append(body), {"data": {"items": [
        {"inn": "!~~1654038766~~!", "link": "/id/1359085", "name": 'ГУП "Таттехмедфарм"'},
        {"inn": "7736050003", "link": "/id/2", "name": "Чужая компания"},
    ]}})[1]

    item = session.card_by_inn("1654038766")
    assert item and item["link"] == "/id/1359085", item
    assert sent[0]["query"] == "1654038766"
    assert sent[0]["state_1"] is True and sent[0]["state_2"] is False,         "по умолчанию ищем только действующие юрлица"

    session.card_by_inn("1654038766", include_inactive=True)
    assert all(sent[1][f"state_{n}"] is True for n in range(1, 6)), sent[1]

    # Чужой ИНН в выдаче не должен подменить компанию: сверка по цифрам, а не «первый».
    assert session.card_by_inn("5028006494") is None

    for bad in ("", "12345", "abc"):
        try:
            session.card_by_inn(bad)
        except RPW.RusProfilePlaywrightError:
            pass
        else:
            raise AssertionError(f"мусорный ИНН принят: {bad!r}")
    print("  OK: карточка по ИНН — сверка по цифрам, статус-фильтр, мусор отвергнут")


def _check_card_schema_fail_closed() -> None:
    session = object.__new__(RPW.RusProfilePlaywrightSession)
    session._snapshot = lambda *_args, **_kwargs: (
        "https://www.rusprofile.ru/id/1", "", "Just a moment... CAPTCHA", "")
    try:
        session.card_facts_by_url("/id/1", expected_inn="1650000049")
    except RPW.RusProfilePlaywrightError as exc:
        assert "карточ" in str(exc).lower() or "captcha" in str(exc).lower()
    else:
        raise AssertionError("CAPTCHA карточки была принята как отсутствие метрик")

    session._snapshot = lambda *_args, **_kwargs: (
        "https://www.rusprofile.ru/id/1", "", "ИНН 1650000049\nКарточка компании", "")
    try:
        session.card_facts_by_url("/id/1", expected_inn="1650000049")
    except RPW.RusProfileCardIncomplete as exc:
        # Неполные данные — НЕ дрейф схемы: класс различает их намеренно
        # (одиночный пробел отсеивает компанию, а не валит прогон).
        assert not isinstance(exc, RPW.RusProfileCardSourceError)
        assert "выруч" in str(exc).lower()
    else:
        raise AssertionError("карточка без блока выручки была принята как полная")

    # Карточка с выручкой, но БЕЗ блока численности — валидна: ССЧ у части
    # компаний не опубликована, а штат больше не критерий отбора (2026-08-19).
    session._snapshot = lambda *_args, **_kwargs: (
        "https://www.rusprofile.ru/id/1", "",
        "ИНН 1650000049\nОсновные показатели за 2025 год:\n"
        "Выручка\n2,5 млрд руб.", "")
    no_staff = session.card_facts_by_url("/id/1", expected_inn="1650000049")
    assert no_staff["_revenue_year"] == 2025, no_staff
    assert no_staff.get("_staff_count") is None, no_staff

    ambiguous = (
        "ИНН 1650000049\n" + CARD_METRICS
        + "\nСреднесписочная численность\n999 сотрудников в 2024 году"
        + "\nОсновные показатели за 2024 год:\nВыручка\n3,1 млрд руб.")
    session._snapshot = lambda *_args, **_kwargs: (
        "https://www.rusprofile.ru/id/1", "", ambiguous, "")
    try:
        session.card_facts_by_url("/id/1", expected_inn="1650000049")
    except RPW.RusProfileCardIncomplete:
        # Неоднозначные метрики схлопываются в None и дают ту же «неполноту»:
        # одиночная кривая карточка — пропуск компании, серия — стоп по лимиту.
        pass
    else:
        raise AssertionError("неоднозначные метрики карточки были приняты по первому regex-match")


def main() -> int:
    _check_card_parsers()
    _check_staff_filter_body()
    _check_card_url_and_search_failure()
    _check_goto_survives_site_redirect()
    _check_card_by_inn()
    _check_card_schema_fail_closed()

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
    marker_item.update({"finance_year": 2025, "sshr": "250", "sshr_year": "2025"})
    marker_lead = RP.item_to_lead(
        marker_item,
        {"label": "Тест", "pain": "", "offer": ""},
        industry,
    )
    assert marker_lead["niche"] == "Тест"
    assert marker_lead["_revenue_year"] == 2025
    assert marker_lead["_staff_count"] == 250
    assert marker_lead["_staff_year"] == 2025

    with tempfile.TemporaryDirectory() as tmp:
        nested = pathlib.Path(tmp) / "new" / "phase1.json"
        RP._save(leads, nested)
        assert nested.is_file()

    print("test_rusprofile_playwright: OK — cookie safe, floor 1B, descending selection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
