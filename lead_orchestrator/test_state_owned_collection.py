# -*- coding: utf-8 -*-
"""Офлайн-регрессии добора ровно N новых госкомпаний."""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time
from decimal import Decimal

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import source_rusprofile as SR  # noqa: E402
import rusprofile_playwright as RPW  # noqa: E402
import state_lead_collection as SLC  # noqa: E402
import state_ownership as SO  # noqa: E402


def valid_test_inn(number):
    body = f"{770000000 + int(number):09d}"
    weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    control = sum(int(body[i]) * weights[i] for i in range(9)) % 11 % 10
    return body + str(control)


def item(inn, name, revenue=2_500_000_000):
    return {
        "inn": inn,
        "name": name,
        "url": f"/id/{inn}",
        "status": {"code": "ACT"},
        "finance_revenue": revenue,
        "main_okved_id": "35.11",
        "main_okved": "Производство электроэнергии",
        "region_name": "Республика Татарстан",
    }


def facts(year=2025, staff=250, revenue="2,5 млрд руб."):
    return {
        "_revenue_year": year,
        "_revenue_display": revenue,
        "_staff_count": staff,
        "_staff_year": 2024,
        "_ceo_fio": "Иванов Иван Иванович",
    }


def ownership(verified, share=0, complete=True, reason=(), region="РЕСПУБЛИКА ТАТАРСТАН"):
    share = Decimal(str(share))
    return SO.OwnershipResult(
        verified=verified,
        share=share,
        direct_share=share,
        indirect_share=Decimal("0"),
        complete=complete,
        source_urls=("https://egrul.nalog.ru/proof",),
        trace=(f"доказанная доля {share}%",),
        reasons=tuple(reason),
        region=region,
    )


class FakeCRM:
    def __init__(self, inns=()):
        self.inns = set(inns)
        self.total = len(self.inns)

    def contains(self, lead):
        return lead.get("_inn") in self.inns


class FakeLocal:
    def __init__(self, inns=()):
        self.inns = set(inns)

    def is_worked(self, inn):
        return inn in self.inns


class FakeSession:
    def __init__(self, rows, metrics):
        self.rows = rows
        self.metrics = metrics
        self.search_kwargs = None
        self.fact_calls = []
        self.contact_calls = []
        self.contact_deadlines = []

    def search(self, okved, revenue_from, **kwargs):
        self.search_kwargs = (okved, revenue_from, kwargs)
        return list(self.rows)

    def card_facts_by_url(self, url, **_kwargs):
        self.fact_calls.append(url)
        return dict(self.metrics[url])

    def contacts_by_url(self, url, *, deadline=None):
        self.contact_calls.append(url)
        self.contact_deadlines.append(deadline)
        return {"emails": ["office@example.test"], "phones": [], "founders": []}


class FakeVerifier:
    def __init__(self, values):
        self.values = values
        self.calls = []
        self.deadlines = []

    def verify(self, name, inn, *, deadline=None):
        self.calls.append((name, inn))
        self.deadlines.append(deadline)
        value = self.values[inn]
        if isinstance(value, Exception):
            raise value
        return value


def check_exact_n_and_order():
    inns = [valid_test_inn(i) for i in range(1, 8)]
    rows = [
        item(inns[0], "ООО Уже в CRM"),
        item(inns[1], "ООО Старая выручка"),
        item(inns[2], "ООО Частная"),
        item(inns[3], "ООО Гос 1"),
        item(inns[4], "ООО Доля 24.9"),
        item(inns[5], "ООО Гос 2"),
        item(inns[6], "ООО Лишняя"),
    ]
    metrics = {row["url"]: facts() for row in rows}
    metrics[rows[1]["url"]] = facts(year=2024)
    session = FakeSession(rows, metrics)
    verifier = FakeVerifier({
        inns[2]: ownership(False, 0),
        inns[3]: ownership(True, 30),
        inns[4]: ownership(False, "24.9"),
        inns[5]: ownership(True, 40),
        inns[6]: ownership(True, 50),
    })
    lines = []
    leads = SR.harvest_state_owned(
        2,
        session=session,
        crm_index=FakeCRM({inns[0]}),
        local_registry=FakeLocal(),
        ownership_verifier=verifier,
        max_pages=7,
        log=lines.append,
    )
    assert [lead["_inn"] for lead in leads] == [inns[3], inns[5]], leads
    search_okved, search_revenue, search_kwargs = session.search_kwargs
    assert search_okved == [] and search_revenue == 2_000_000_000
    assert search_kwargs["max_pages"] == 7
    assert "staff_from" not in search_kwargs and "staff_to" not in search_kwargs, \
        "фильтр по штату удалён 2026-08-19 и не должен возвращаться в поиск"
    assert search_kwargs["region_codes"] == ["16"], \
        "серверный фильтр региона (код 16) обязан уходить в advanced-search"
    assert isinstance(search_kwargs["deadline"], float)
    assert rows[0]["url"] not in session.fact_calls, "CRM-дубликат дошёл до карточки"
    assert inns[1] not in {inn for _, inn in verifier.calls}, "не-2025 дошёл до ownership"
    assert inns[4] in {inn for _, inn in verifier.calls}, "24.9% обязан дойти до ownership"
    assert inns[6] not in {inn for _, inn in verifier.calls}, "поиск не остановился ровно на N"
    assert session.contact_calls == [rows[3]["url"], rows[5]["url"]]
    assert all(isinstance(value, float) for value in verifier.deadlines)
    assert all(isinstance(value, float) for value in session.contact_deadlines)
    for lead in leads:
        assert lead["_revenue_year"] == 2025
        assert lead["_state_share"] >= 25
        assert lead["_state_ownership_urls"]
        assert lead["email"] == "office@example.test"
    assert any("CRM" in line and "1" in line for line in lines), lines
    print("  ✓ порядок CRM→карточка→метрики→ownership и остановка ровно на N")


def check_exhaustion_is_hard():
    inn = valid_test_inn(10)
    low_inn = valid_test_inn(11)
    rows = [item("1234567890", "ООО Испорченный ИНН"),
            item(low_inn, "ООО Низкая выручка 2025"),
            item(inn, "ООО Только частная")]
    session = FakeSession(rows, {row["url"]: facts() for row in rows})
    session.metrics[rows[1]["url"]] = facts(revenue="1,9 млрд руб.")
    verifier = FakeVerifier({inn: ownership(False, 0)})
    try:
        SR.harvest_state_owned(
            2, session=session, crm_index=FakeCRM(), local_registry=FakeLocal(),
            ownership_verifier=verifier,
            max_pages=1, log=lambda _line: None,
        )
    except SR.StateLeadExhausted as exc:
        assert exc.found == 0 and exc.requested == 2
        assert exc.stats["state_below_25"] == 1, exc.stats
        assert exc.stats["invalid_inn"] == 1, exc.stats
        assert exc.stats["revenue"] == 1, exc.stats
    else:
        raise AssertionError("недобор ошибочно принят за готовый результат")
    print("  ✓ исчерпание источника — явная ошибка M/N без запуска следующей стадии")


def check_systemic_ownership_failure_stops():
    rows = [item(valid_test_inn(100 + i), f"ООО Ошибка {i}") for i in range(4)]
    session = FakeSession(rows, {row["url"]: facts() for row in rows})
    verifier = FakeVerifier({
        row["inn"]: SO.StateOwnershipSourceError("ЕГРЮЛ потребовал CAPTCHA") for row in rows
    })
    try:
        SR.harvest_state_owned(
            1, session=session, crm_index=FakeCRM(), local_registry=FakeLocal(),
            ownership_verifier=verifier,
            ownership_error_limit=3, log=lambda _line: None,
        )
    except SR.StateOwnershipUnavailable as exc:
        assert "3" in str(exc) and "CAPTCHA" in str(exc)
        assert len(verifier.calls) == 3
    else:
        raise AssertionError("системный сбой источника был принят за отсутствие госучастия")
    print("  ✓ три подряд сбоя ЕГРЮЛ/Rosim останавливают прогон fail-closed")


def check_systemic_card_failure_stops():
    rows = [item(valid_test_inn(120 + i), f"ООО CAPTCHA {i}") for i in range(4)]

    class BrokenCards(FakeSession):
        def card_facts_by_url(self, link, **_kwargs):
            self.fact_calls.append(link)
            raise RuntimeError("CAPTCHA вместо карточки")

    session = BrokenCards(rows, {})
    verifier = FakeVerifier({})
    try:
        SR.harvest_state_owned(
            1, session=session, crm_index=FakeCRM(), local_registry=FakeLocal(),
            ownership_verifier=verifier, card_error_limit=3,
            log=lambda _line: None)
    except SR.StateOwnershipUnavailable as exc:
        assert "3" in str(exc) and "CAPTCHA" in str(exc)
        assert len(session.fact_calls) == 3 and verifier.calls == []
    else:
        raise AssertionError("системный сбой карточек был выдан за обычный недобор")
    print("  ✓ три подряд сбоя/CAPTCHA карточек останавливают прогон fail-closed")


def check_single_card_schema_failure_stops():
    row = item(valid_test_inn(130), "ООО Частичный drift")

    class DriftCard(FakeSession):
        def card_facts_by_url(self, link, **_kwargs):
            self.fact_calls.append(link)
            raise RPW.RusProfileCardSourceError("нет численности")

    session = DriftCard([row], {})
    try:
        SR.harvest_state_owned(
            1, session=session, crm_index=FakeCRM(), local_registry=FakeLocal(),
            ownership_verifier=FakeVerifier({}), card_error_limit=3,
            log=lambda _line: None)
    except SR.StateOwnershipUnavailable as exc:
        assert "численност" in str(exc)
        assert len(session.fact_calls) == 1
    except SR.StateLeadExhausted as exc:
        raise AssertionError("одиночный schema drift выдан за exhaustion") from exc
    else:
        raise AssertionError("одиночный schema drift был проигнорирован")
    print("  ✓ один schema drift карточки останавливает прогон fail-closed")


def check_single_incomplete_card_skips_company():
    """Карточка без блока данных (боевой ИНН 1650280847) — пропуск, не остановка."""
    gap_inn = valid_test_inn(140)
    ok_inn = valid_test_inn(141)
    rows = [item(gap_inn, "ООО Без блока выручки"), item(ok_inn, "ГУП Полная")]

    class GapCard(FakeSession):
        def card_facts_by_url(self, link, **_kwargs):
            self.fact_calls.append(link)
            if link == f"/id/{gap_inn}":
                raise RPW.RusProfileCardIncomplete("на карточке нет блока выручки")
            return dict(self.metrics[link])

    session = GapCard(rows, {f"/id/{ok_inn}": facts()})
    leads = SR.harvest_state_owned(
        1, session=session, crm_index=FakeCRM(), local_registry=FakeLocal(),
        ownership_verifier=FakeVerifier({ok_inn: ownership(True, 100)}),
        card_error_limit=3, log=lambda _line: None)
    assert [lead["_inn"] for lead in leads] == [ok_inn], leads
    assert len(session.fact_calls) == 2, session.fact_calls
    print("  ✓ одиночная карточка без блока данных отсеивается, прогон продолжается")


def check_local_duplicate_before_card():
    inn = valid_test_inn(20)
    row = item(inn, "ООО Уже отработана локально")
    session = FakeSession([row], {row["url"]: facts()})
    try:
        SR.harvest_state_owned(
            1, session=session, crm_index=FakeCRM(),
            local_registry=FakeLocal({inn}), ownership_verifier=FakeVerifier({}),
            log=lambda _line: None,
        )
    except SR.StateLeadExhausted as exc:
        assert exc.stats["local_duplicate"] == 1
        assert session.fact_calls == []
    else:
        raise AssertionError("локально отработанная компания попала в новый набор")
    print("  ✓ локальный реестр отсекает дубль до открытия карточки")


def check_candidate_limit_is_hard():
    private_inn = valid_test_inn(41)
    public_inn = valid_test_inn(42)
    rows = [item(private_inn, "ООО Частная"), item(public_inn, "ГУП Следующая")]
    session = FakeSession(rows, {
        f"/id/{private_inn}": facts(),
        f"/id/{public_inn}": facts(),
    })
    verifier = FakeVerifier({private_inn: ownership(0), public_inn: ownership(100)})
    try:
        SR.harvest_state_owned(
            1, session=session, crm_index=FakeCRM(), local_registry=FakeLocal(),
            ownership_verifier=verifier, max_candidates=1, max_seconds=60,
            log=lambda _line: None)
    except SR.StateLeadExhausted as exc:
        assert exc.stats["source_candidates"] == 1
        assert verifier.calls == [("ООО Частная", private_inn)]
    else:
        raise AssertionError("кандидат за max_candidates был обработан")
    print("  ✓ max_candidates ограничивает официальный обход и возвращает диагностику")


def check_invalid_name_before_card():
    bad_inn = valid_test_inn(51)
    good_inn = valid_test_inn(52)
    rows = [item(bad_inn, ""), item(good_inn, "ГУП С названием")]
    session = FakeSession(rows, {f"/id/{good_inn}": facts()})
    result = SR.harvest_state_owned(
        1, session=session, crm_index=FakeCRM(), local_registry=FakeLocal(),
        ownership_verifier=FakeVerifier({good_inn: ownership(100)}),
        log=lambda _line: None)
    assert [lead["_inn"] for lead in result] == [good_inn]
    assert session.fact_calls == [f"/id/{good_inn}"]
    print("  ✓ пустое название отклоняется до карточки и не ломает точный N")


def check_deadline_after_ownership_is_hard():
    inn = valid_test_inn(61)
    row = item(inn, "ГУП Медленная проверка")
    session = FakeSession([row], {row["url"]: facts()})
    clock = [0.0]

    class SlowVerifier(FakeVerifier):
        def verify(self, name, checked_inn, *, deadline=None):
            result = super().verify(name, checked_inn, deadline=deadline)
            clock[0] = deadline + 0.001
            return result

    old_monotonic = SLC.time.monotonic
    SLC.time.monotonic = lambda: clock[0]
    try:
        try:
            SR.harvest_state_owned(
                1, session=session, crm_index=FakeCRM(), local_registry=FakeLocal(),
                ownership_verifier=SlowVerifier({inn: ownership(100)}),
                max_seconds=60, log=lambda _line: None)
        except SR.StateLeadExhausted as exc:
            assert exc.found == 0 and exc.stats["time_limit"] == 1
            assert session.contact_calls == []
        else:
            raise AssertionError("лид принят после исчерпания общего deadline")
    finally:
        SLC.time.monotonic = old_monotonic
    print("  ✓ после истечения deadline лид не принимается и контакты не открываются")


def check_deadline_after_contacts_is_hard():
    inn = valid_test_inn(62)
    row = item(inn, "ГУП Медленные контакты")
    clock = [0.0]

    class SlowContacts(FakeSession):
        def contacts_by_url(self, url, *, deadline=None):
            result = super().contacts_by_url(url, deadline=deadline)
            clock[0] = deadline + 0.001
            return result

    session = SlowContacts([row], {row["url"]: facts()})
    old_monotonic = SLC.time.monotonic
    SLC.time.monotonic = lambda: clock[0]
    try:
        try:
            SR.harvest_state_owned(
                1, session=session, crm_index=FakeCRM(), local_registry=FakeLocal(),
                ownership_verifier=FakeVerifier({inn: ownership(100)}),
                max_seconds=60, log=lambda _line: None)
        except SR.StateLeadExhausted as exc:
            assert exc.found == 0 and exc.stats["time_limit"] == 1
        else:
            raise AssertionError("лид принят после late-return контактов за deadline")
    finally:
        SLC.time.monotonic = old_monotonic
    print("  ✓ late-return контактов после deadline не принимается")


def check_deadline_inside_search_is_not_exhaustion():
    class ExpiredSearch(FakeSession):
        def search(self, *_args, **_kwargs):
            raise RPW.RusProfileDeadlineReached("deadline внутри pagination")

    try:
        SR.harvest_state_owned(
            1, session=ExpiredSearch([], {}), crm_index=FakeCRM(),
            local_registry=FakeLocal(), ownership_verifier=FakeVerifier({}),
            log=lambda _line: None)
    except RPW.RusProfileDeadlineReached:
        pass
    except SR.StateLeadExhausted as exc:
        raise AssertionError("deadline внутри поиска превращён в исчерпание") from exc
    else:
        raise AssertionError("deadline внутри поиска был проигнорирован")

    class LateEmptySearch(FakeSession):
        def search(self, *_args, **_kwargs):
            time.sleep(0.02)
            return []

    try:
        SR.harvest_state_owned(
            1, session=LateEmptySearch([], {}), crm_index=FakeCRM(),
            local_registry=FakeLocal(), ownership_verifier=FakeVerifier({}),
            deadline=time.monotonic() + 0.005, log=lambda _line: None)
    except RPW.RusProfileDeadlineReached:
        pass
    except SR.StateLeadExhausted as exc:
        raise AssertionError("поздний пустой ответ search выдан за exhaustion") from exc
    else:
        raise AssertionError("поздний пустой ответ search был принят")
    print("  ✓ deadline внутри pagination остаётся timeout, а не исчерпанием")


def check_invalid_target():
    for value in (0, -1):
        try:
            SR.harvest_state_owned(value, session=FakeSession([], {}), crm_index=FakeCRM(),
                                   local_registry=FakeLocal(),
                                   ownership_verifier=FakeVerifier({}))
        except ValueError:
            pass
        else:
            raise AssertionError(f"target={value} должен быть отклонён")
    print("  ✓ requested_count обязан быть положительным")


def check_region_filter():
    """Госкомпания обязана быть из Татарстана: чужой регион в выдаче отсекается
    до карточки, а принятие требует юрадреса из выписки ЕГРЮЛ (fail-closed)."""
    inns = [valid_test_inn(i) for i in range(70, 75)]
    moscow = item(inns[0], "ГУП Московская")
    moscow["region_name"] = "г. Москва"
    blank_source = item(inns[1], "ГУП Без региона в выдаче")
    blank_source["region_name"] = ""
    moved = item(inns[2], "ГУП Переехавшая")           # выдача врёт, выписка — нет
    no_address = item(inns[3], "ГУП Без адреса в выписке")
    kazan = item(inns[4], "ГУП Казанская")
    rows = [moscow, blank_source, moved, no_address, kazan]
    verifier_rows = {
        inns[1]: ownership(True, 100),
        inns[2]: ownership(True, 100, region="ГОРОД МОСКВА"),
        inns[3]: ownership(True, 100, region=""),
        inns[4]: ownership(True, 100),
    }

    session = FakeSession(rows, {row["url"]: facts() for row in rows})
    leads = SR.harvest_state_owned(
        2, session=session, crm_index=FakeCRM(), local_registry=FakeLocal(),
        ownership_verifier=FakeVerifier(verifier_rows), log=lambda _line: None)
    assert [lead["_inn"] for lead in leads] == [inns[1], inns[4]], leads
    assert moscow["url"] not in session.fact_calls, "чужой регион выдачи дошёл до карточки"
    assert all(lead["_egrul_region"] == "РЕСПУБЛИКА ТАТАРСТАН" for lead in leads)

    # Недобор из-за региона — обычное исчерпание с внятной статистикой.
    session = FakeSession(rows, {row["url"]: facts() for row in rows})
    try:
        SR.harvest_state_owned(
            3, session=session, crm_index=FakeCRM(), local_registry=FakeLocal(),
            ownership_verifier=FakeVerifier(verifier_rows), log=lambda _line: None)
    except SR.StateLeadExhausted as exc:
        assert exc.found == 2 and exc.stats["region_source"] == 1, exc.stats
        assert exc.stats["region_egrul"] == 1 and exc.stats["region_unknown"] == 1, exc.stats
    else:
        raise AssertionError("недобор по региону принят за готовый результат")

    # region="" — осознанное отключение фильтра: регион не проверяется вовсе.
    all_rows = dict(verifier_rows)
    all_rows[inns[0]] = ownership(True, 100, region="ГОРОД МОСКВА")
    session = FakeSession(rows, {row["url"]: facts() for row in rows})
    leads = SR.harvest_state_owned(
        5, session=session, crm_index=FakeCRM(), local_registry=FakeLocal(),
        ownership_verifier=FakeVerifier(all_rows), region="",
        log=lambda _line: None)
    assert len(leads) == 5, [lead["_inn"] for lead in leads]
    assert session.search_kwargs[2]["region_codes"] is None, \
        "region=\"\" обязан выключать и серверный фильтр выдачи"
    print("  ✓ регион: дёшево по выдаче, строго по выписке ЕГРЮЛ, выключается явно")


def check_no_ownership_and_timezone():
    """STATE_LEAD_OWNERSHIP=0: критерии — выручка/регион/пояс, ЕГРЮЛ не зовётся."""
    # «*» отключает регион-фильтр: PowerShell не передаёт детям пустую env,
    # и без явного отключателя дефолт «Татарстан» включался бы молча.
    saved_region = os.environ.get("STATE_LEAD_REGION")
    try:
        os.environ["STATE_LEAD_REGION"] = "*"
        assert SLC.lead_region_query() == ""
        os.environ["STATE_LEAD_REGION"] = "любой"
        assert SLC.lead_region_query() == ""
        os.environ["STATE_LEAD_REGION"] = "Татарстан"
        assert SLC.lead_region_query() == "Татарстан"
    finally:
        if saved_region is None:
            os.environ.pop("STATE_LEAD_REGION", None)
        else:
            os.environ["STATE_LEAD_REGION"] = saved_region

    assert SLC.region_tz_offset("Республика Татарстан") == 3
    assert SLC.region_tz_offset("Калининградская область") == 2
    assert SLC.region_tz_offset("Свердловская область") == 5
    assert SLC.region_tz_offset("Камчатский край") == 12
    assert SLC.region_tz_offset("") is None

    inns = [valid_test_inn(i) for i in range(150, 154)]
    msk = item(inns[0], "ООО Московская")
    msk["region_name"] = "Москва"
    smr = item(inns[1], "АО Самарская")
    smr["region_name"] = "Самарская область"
    nsk = item(inns[2], "ООО Новосибирская")
    nsk["region_name"] = "Новосибирская область"
    blank = item(inns[3], "ООО Без региона")
    blank["region_name"] = ""
    rows = [msk, smr, nsk, blank]

    session = FakeSession(rows, {row["url"]: facts() for row in rows})
    verifier = FakeVerifier({})          # любое обращение к госдоле упало бы KeyError
    leads = SR.harvest_state_owned(
        2, session=session, crm_index=FakeCRM(), local_registry=FakeLocal(),
        ownership_verifier=verifier, region="", verify_ownership=False, tz_limit=2,
        log=lambda _line: None)
    assert [lead["_inn"] for lead in leads] == [inns[0], inns[1]], leads
    assert verifier.calls == [], "госдоля не должна проверяться при verify_ownership=False"
    assert all("_state_share" not in lead for lead in leads)
    assert leads[0]["_source_region"] == "Москва"

    # Новосибирск (+7) и пустой регион при tz-фильтре отсеиваются со статистикой.
    session = FakeSession(rows, {row["url"]: facts() for row in rows})
    try:
        SR.harvest_state_owned(
            3, session=session, crm_index=FakeCRM(), local_registry=FakeLocal(),
            ownership_verifier=FakeVerifier({}), region="", verify_ownership=False,
            tz_limit=2, log=lambda _line: None)
    except SR.StateLeadExhausted as exc:
        assert exc.found == 2, exc.stats
        assert exc.stats["tz_far"] == 1 and exc.stats["tz_unknown"] == 1, exc.stats
    else:
        raise AssertionError("чужой часовой пояс был принят")

    # Регион-фильтр без ownership матчится по выдаче (ЕГРЮЛ недоступен).
    session = FakeSession(rows, {row["url"]: facts() for row in rows})
    leads = SR.harvest_state_owned(
        1, session=session, crm_index=FakeCRM(), local_registry=FakeLocal(),
        ownership_verifier=FakeVerifier({}), region="Самар", verify_ownership=False,
        log=lambda _line: None)
    assert [lead["_inn"] for lead in leads] == [inns[1]], leads
    print("  ✓ без госдоли: пояс и регион по выдаче, ЕГРЮЛ не вызывается")


def check_corrupt_local_registry_is_hard():
    with tempfile.TemporaryDirectory() as folder:
        path = pathlib.Path(folder) / "registry.json"
        path.write_text("{broken", encoding="utf-8")
        try:
            SLC.load_local_registry_strict(str(path), log=lambda _line: None)
        except RuntimeError as exc:
            assert "не читается" in str(exc)
        else:
            raise AssertionError("повреждённый локальный реестр принят как пустой")
    print("  ✓ повреждённый локальный реестр останавливает строгий добор")


def main():
    print("добор госкомпаний — офлайн-регрессии:")
    check_exact_n_and_order()
    check_exhaustion_is_hard()
    check_systemic_ownership_failure_stops()
    check_systemic_card_failure_stops()
    check_single_card_schema_failure_stops()
    check_single_incomplete_card_skips_company()
    check_local_duplicate_before_card()
    check_candidate_limit_is_hard()
    check_invalid_name_before_card()
    check_deadline_after_ownership_is_hard()
    check_deadline_after_contacts_is_hard()
    check_deadline_inside_search_is_not_exhaustion()
    check_invalid_target()
    check_region_filter()
    check_no_ownership_and_timezone()
    check_corrupt_local_registry_is_hard()

    print("test_state_owned_collection: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
