# -*- coding: utf-8 -*-
"""Офлайн-регрессии прямого и косвенного государственного участия."""
from __future__ import annotations

import email.utils
import hashlib
import io
import json
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import state_ownership as SO  # noqa: E402


EGRUL_DIRECT = """Выписка из ЕГРЮЛ
Полное наименование на русском языке
ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ТЕСТ"
2
ГРН и дата внесения в ЕГРЮЛ записи
Сведения об участниках / учредителях юридического лица
10
ГРН и дата внесения в ЕГРЮЛ сведений о
данном лице
111
01.01.2025
11
Полное наименование
РОССИЙСКАЯ ФЕДЕРАЦИЯ
12
Размер доли (в процентах)
30
13
ГРН и дата внесения в ЕГРЮЛ сведений о
данном лице
222
01.01.2025
14
Фамилия
Имя
Отчество
ИВАНОВ
ИВАН
ИВАНОВИЧ
15
ИНН
123456789012
16
Размер доли (в процентах)
70
Сведения об учете в налоговом органе
"""

EGRUL_FOREIGN = """Выписка из ЕГРЮЛ
Полное наименование на русском языке
ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ОПТИМУМ"
2
ГРН и дата внесения в ЕГРЮЛ записи
Сведения об участниках / учредителях юридического лица
32
ГРН и дата внесения в ЕГРЮЛ сведений о
данном лице
2247711675484
20.11.2024
33
Полное наименование
SAFI FOOD TRADING L.L.C
34
Страна происхождения
Объединенные Арабские Эмираты
40
Номинальная стоимость доли (в рублях)
176500000
41
Размер доли (в процентах)
100
Сведения об учете в налоговом органе
"""


def entity(inn, name, owners=(), *, kind="llc"):
    return SO.Entity(inn=inn, name=name, kind=kind, owners=tuple(owners),
                     source_url=f"https://egrul.nalog.ru/vyp-download/{inn}")


def owner(name, share, inn="", kind="legal"):
    return SO.Owner(name=name, inn=inn, share=Decimal(str(share)), kind=kind)


class FakeEgrul:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def entity(self, inn, **_kwargs):
        self.calls.append(inn)
        value = self.rows[inn]
        if isinstance(value, Exception):
            raise value
        return value


class FakeRosim:
    def __init__(self, rows=None):
        self.rows = rows or {}

    def lookup(self, name, inn):
        return self.rows.get(inn)


def check_egrul_parser():
    parsed = SO.parse_egrul_text(EGRUL_DIRECT, inn="1650000000", source_url="https://egrul.nalog.ru/x")
    assert parsed.name.endswith('"ТЕСТ"'), parsed
    assert len(parsed.owners) == 2, parsed.owners
    assert parsed.owners[0].kind == "public" and parsed.owners[0].share == Decimal("30")
    assert parsed.owners[1].kind == "person" and parsed.owners[1].inn == "123456789012"

    foreign = SO.parse_egrul_text(EGRUL_FOREIGN, inn="5009032045", source_url="https://egrul.nalog.ru/y")
    assert len(foreign.owners) == 1
    assert foreign.owners[0].kind == "foreign"
    assert foreign.owners[0].share == Decimal("100")
    print("  ✓ разбор текущих участников ЕГРЮЛ: государство, физлицо, иностранное юрлицо")


def check_direct_and_threshold():
    direct = entity("1", "ООО Тест", [
        owner("Российская Федерация", 30, kind="public"),
        owner("Иванов", 70, inn="123456789012", kind="person"),
    ])
    result = SO.OwnershipVerifier(FakeEgrul({"1": direct}), FakeRosim()).verify("ООО Тест", "1")
    assert result.verified and result.share == Decimal("30") and result.direct_share == Decimal("30")

    exact = entity("2", "ООО Ровно", [owner("Республика Татарстан", 25, kind="public")])
    result = SO.OwnershipVerifier(FakeEgrul({"2": exact}), FakeRosim()).verify("ООО Ровно", "2")
    assert not result.verified and result.complete and result.share == Decimal("25")
    print("  ✓ прямое участие и строгий порог >25% (ровно 25% не проходит)")


def check_indirect_math():
    parent_inn = "1650000020"
    child = entity("10", "ООО Дочка", [owner("АО Материнская", 80, inn=parent_inn)])
    parent = entity(parent_inn, "АО Материнская", [
        owner("Российская Федерация", 40, kind="public")])
    verifier = SO.OwnershipVerifier(FakeEgrul({"10": child, parent_inn: parent}), FakeRosim())
    result = verifier.verify("ООО Дочка", "10")
    assert result.verified, result
    assert result.share == Decimal("32.00"), result
    assert result.direct_share == 0 and result.indirect_share == Decimal("32.00")
    assert any("80" in step and "40" in step and "32" in step for step in result.trace), result.trace

    mixed = entity("30", "ООО Смешанная", [
        owner("Российская Федерация", 10, kind="public"),
        owner("АО Материнская", 50, inn=parent_inn),
    ])
    result = SO.OwnershipVerifier(
        FakeEgrul({"30": mixed, parent_inn: parent}), FakeRosim()).verify("ООО Смешанная", "30")
    assert result.share == Decimal("30.00") and result.verified, result
    print("  ✓ косвенная доля: перемножение по цепочке и сумма независимых путей")


def check_cycles_and_unknown():
    a_inn, b_inn = "1650000040", "1650000041"
    a = entity(a_inn, "ООО А", [owner("ООО Б", 100, inn=b_inn)])
    b = entity(b_inn, "ООО Б", [owner("ООО А", 100, inn=a_inn)])
    result = SO.OwnershipVerifier(
        FakeEgrul({a_inn: a, b_inn: b}), FakeRosim()).verify("ООО А", a_inn)
    assert not result.verified and not result.complete
    assert any("цикл" in reason.lower() for reason in result.reasons), result.reasons

    ao = entity("50", "АО Неизвестное", (), kind="ao")
    result = SO.OwnershipVerifier(FakeEgrul({"50": ao}), FakeRosim()).verify("АО Неизвестное", "50")
    assert not result.verified and not result.complete

    historical = entity("51", "АО Исторические учредители", [
        owner("Российская Федерация", 30, kind="public"),
        owner("Иванов", 70, inn="123456789012", kind="person"),
    ], kind="ao")
    result = SO.OwnershipVerifier(
        FakeEgrul({"51": historical}), FakeRosim()).verify(historical.name, historical.inn)
    assert not result.verified and not result.complete and result.share == 0, result
    assert any("акционер" in reason.lower() for reason in result.reasons), result.reasons
    try:
        SO.OwnershipVerifier(FakeEgrul({}), FakeRosim()).verify(
            "АО Просрочено", "1650000049", deadline=0)
    except SO.StateOwnershipDeadline:
        pass
    else:
        raise AssertionError("истёкший deadline начал обход ownership-графа")
    print("  ✓ циклы и закрытые акционеры АО дают unknown, а не ложный допуск")


def check_rosim_exact_name():
    target_inn = "1650000049"
    evidence = SO.RosimEvidence(
        name='ПАО "Газпром"', share=Decimal("38.37"),
        source_url="https://rosim.gov.ru/doc/current", row="строка 7", inn=target_inn,
    )
    registry = SO.RosimRegistry({target_inn: evidence})
    ao = entity(target_inn, 'ПАО "Газпром"', (), kind="ao")
    verifier = SO.OwnershipVerifier(FakeEgrul({target_inn: ao}), registry)
    result = verifier.verify('ПАО "Газпром"', target_inn)
    assert result.verified and result.share == Decimal("38.37") and not result.complete
    assert any("строка 7" in step for step in result.trace)

    mismatch = SO.RosimEvidence(
        name='АО "Совершенно другая компания"', share=Decimal("38.37"),
        source_url="https://rosim.gov.ru/doc/current", row="строка 9", inn=target_inn,
    )
    mismatch_registry = SO.RosimRegistry({target_inn: mismatch})
    assert mismatch_registry.lookup('ПАО "Газпром"', target_inn) is None
    result = SO.OwnershipVerifier(
        FakeEgrul({target_inn: ao}), mismatch_registry).verify(ao.name, target_inn)
    assert not result.verified and result.share == 0, result

    low = SO.RosimEvidence(
        name='АО "Низкая доля"', share=Decimal("20"),
        source_url="https://rosim.gov.ru/doc/current", row="строка 8", inn="1650000056",
    )
    low_registry = FakeRosim({low.inn: low})
    low_ao = entity(low.inn, low.name, (), kind="ao")
    result = SO.OwnershipVerifier(FakeEgrul({low.inn: low_ao}), low_registry).verify(
        low.name, low.inn)
    assert not result.verified and not result.complete and result.share == Decimal("20")

    other_inn = "1650000063"
    same_name_private = entity(other_inn, 'ООО "Газпром"', (), kind="ao")
    result = SO.OwnershipVerifier(FakeEgrul({other_inn: same_name_private}), registry).verify(
        'ООО "Газпром"', other_inn)
    assert not result.verified and not result.complete
    print("  ✓ Росимущество привязано к ИНН, ОПФ/имя не дают ложного совпадения")


def check_public_legal_forms():
    for idx, name in enumerate((
        "ФЕДЕРАЛЬНОЕ ГОСУДАРСТВЕННОЕ УНИТАРНОЕ ПРЕДПРИЯТИЕ ТЕСТ",
        "МУНИЦИПАЛЬНОЕ УНИТАРНОЕ ПРЕДПРИЯТИЕ ТЕСТ",
        "ГОСУДАРСТВЕННАЯ КОРПОРАЦИЯ ТЕСТ",
        "ПУБЛИЧНО-ПРАВОВАЯ КОМПАНИЯ ТЕСТ",
    ), 70):
        result = SO.OwnershipVerifier(
            FakeEgrul({str(idx): entity(str(idx), name, (), kind="public")}),
            FakeRosim(),
        ).verify(name, str(idx))
        assert result.verified and result.share == Decimal("100"), (name, result)
    print("  ✓ ГУП/МУП, госкорпорации и публично-правовые компании = 100%")


def check_invalid_graph_and_independent_public_edges():
    invalid = entity("90", "ООО Некорректное", [
        owner("Российская Федерация", 60, kind="public"),
        owner("Иванов", 70, inn="123456789012", kind="person"),
    ])
    result = SO.OwnershipVerifier(FakeEgrul({"90": invalid}), FakeRosim()).verify(
        invalid.name, invalid.inn)
    assert not result.verified and not result.complete and result.share == 0
    assert any("структурно" in reason for reason in result.reasons)

    fractional_overflow = entity("91", "ООО Дробное переполнение", [
        owner("Российская Федерация", "30.01", kind="public"),
        owner("Иванов", 70, inn="123456789012", kind="person"),
    ])
    result = SO.OwnershipVerifier(
        FakeEgrul({"91": fractional_overflow}), FakeRosim()).verify(
            fractional_overflow.name, fractional_overflow.inn)
    assert not result.verified and not result.complete and result.share == 0

    inn = "1650000049"
    mixed = entity(inn, "АО Смешанное", [
        owner("Российская Федерация", 20, kind="public"),
        owner("Город Москва", 20, kind="public"),
        owner("Иванов", 60, inn="123456789012", kind="person"),
    ])
    evidence = SO.RosimEvidence(
        mixed.name, Decimal("20"), "https://rosim.gov.ru/current.xlsx",
        "Лист1, строка 2", inn)
    result = SO.OwnershipVerifier(
        FakeEgrul({inn: mixed}), FakeRosim({inn: evidence})).verify(mixed.name, inn)
    assert result.verified and result.direct_share == Decimal("40")

    overflow_inn = "1650000056"
    overflow = entity(overflow_inn, "АО Переполнение", [
        owner("Удмуртская Республика", 80, kind="public"),
        owner("Иванов", 20, inn="123456789012", kind="person"),
    ])
    federal = SO.RosimEvidence(
        overflow.name, Decimal("30"), "https://rosim.gov.ru/current.xlsx",
        "Лист1, строка 3", overflow_inn)
    result = SO.OwnershipVerifier(
        FakeEgrul({overflow_inn: overflow}),
        FakeRosim({overflow_inn: federal}),
    ).verify(overflow.name, overflow_inn)
    assert not result.verified and result.share == 0
    assert any("100%" in reason for reason in result.reasons)

    assert SO._public_owner_name("Удмуртская Республика")
    assert SO._public_level("Удмуртская Республика") == "regional"
    assert SO._public_level(
        "Министерство промышленности и торговли Российской Федерации") == "federal"
    print("  ✓ некорректный граф отклоняется; независимые публичные доли суммируются")


def check_rosim_percent_cell():
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Наименование", "ИНН", "Доля Российской Федерации"])
    sheet.append(["АО Тест", "1650000049", 0.30])
    sheet["C2"].number_format = "0.00%"
    data = io.BytesIO()
    workbook.save(data)
    rows = SO.RosimRegistry._parse(
        data.getvalue(), ".xlsx", "https://rosim.gov.ru/current.xlsx")
    assert rows["1650000049"].share == Decimal("30.00")
    print("  ✓ XLSX-ячейка 0.30 с процентным форматом читается как 30%")


def check_rosim_strict_source():
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Наименование", "ИНН", "Доля Российской Федерации"])
    sheet.append(["АО Тест", "1650000049", 30])
    blob = io.BytesIO()
    workbook.save(blob)
    payload = blob.getvalue()
    url = "https://rosim.gov.ru/opendata/current.xlsx"

    class Response:
        def __init__(self, final_url, modified):
            self.final_url = final_url
            self.headers = {"Last-Modified": modified}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return payload

        def geturl(self):
            return self.final_url

    response = [Response(
        url, email.utils.format_datetime(datetime.now(timezone.utc) - timedelta(days=1)))]

    class Opener:
        def open(self, _request, timeout):
            assert timeout == 60
            return response[0]

    old_build = SO.urllib.request.build_opener
    old_url = os.environ.get("STATE_ROSIM_URL")
    old_file = os.environ.pop("STATE_ROSIM_FILE", None)
    os.environ["STATE_ROSIM_URL"] = url
    SO.urllib.request.build_opener = lambda *_args: Opener()
    try:
        registry = SO.RosimRegistry.from_environment()
        evidence = registry.lookup("АО Тест", "1650000049")
        assert evidence and evidence.sha256 == hashlib.sha256(payload).hexdigest()
        assert evidence.published_at

        try:
            SO.RosimRegistry.from_environment(deadline=SO.time.monotonic() - 1)
        except SO.StateOwnershipDeadline:
            pass
        else:
            raise AssertionError("истёкший deadline начал загрузку Росимущества")

        response[0] = Response(
            url, email.utils.format_datetime(datetime.now(timezone.utc) - timedelta(days=90)))
        try:
            SO.RosimRegistry.from_environment()
        except SO.StateOwnershipSourceError as exc:
            assert "актуальность" in str(exc)
        else:
            raise AssertionError("просроченный XLSX Росимущества был принят")

        response[0] = Response(
            "https://example.org/redirect.xlsx",
            email.utils.format_datetime(datetime.now(timezone.utc)))
        try:
            SO.RosimRegistry.from_environment()
        except SO.StateOwnershipSourceError as exc:
            assert "перенаправ" in str(exc)
        else:
            raise AssertionError("redirect документа Росимущества был принят")
    finally:
        SO.urllib.request.build_opener = old_build
        if old_url is None:
            os.environ.pop("STATE_ROSIM_URL", None)
        else:
            os.environ["STATE_ROSIM_URL"] = old_url
        if old_file is not None:
            os.environ["STATE_ROSIM_FILE"] = old_file
    print("  ✓ Росимущество: HTTPS, freshness, redirect guard и SHA-256 provenance")


def check_egrul_artifact_provenance():
    inn = "1650000049"
    pdf = b"%PDF-1.7\nimmutable-test-artifact"
    digest = hashlib.sha256(pdf).hexdigest()
    source_url = "https://egrul.nalog.ru/vyp-download/temporary-token"
    document_date = "2026-08-16"
    assert SO._egrul_document_date(
        "Выписка из Единого государственного реестра юридических лиц\n"
        "16.08.2026 № ЮЭ9965-26") == document_date

    with tempfile.TemporaryDirectory() as folder:
        client = SO.EgrulClient(cache_dir=folder, ttl_h=24)
        client._download_text = lambda *_args, **_kwargs: (
            EGRUL_DIRECT, source_url, pdf, document_date)
        parsed = client.entity(inn)
        artifact = pathlib.Path(folder) / f"egrul_{inn}.pdf"
        metadata = json.loads(
            (pathlib.Path(folder) / f"egrul_{inn}.json").read_text(encoding="utf-8"))
        assert artifact.read_bytes() == pdf
        assert metadata["sha256"] == digest and metadata["document_date"] == document_date
        assert parsed.source_sha256 == digest and parsed.source_date == document_date
        assert pathlib.Path(parsed.source_artifact) == artifact

        verifier = SO.OwnershipVerifier(FakeEgrul({inn: parsed}), FakeRosim())
        result = verifier.verify(parsed.name, inn)
        assert any(digest in line and document_date in line for line in result.trace)

        cached = SO.EgrulClient(cache_dir=folder, ttl_h=24)
        cached._download_text = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("валидный архивный PDF не должен скачиваться повторно"))
        assert cached.entity(inn).source_sha256 == digest

        artifact.write_bytes(b"tampered")
        try:
            cached.entity(inn)
        except AssertionError as exc:
            assert "скачиваться" in str(exc)
        else:
            raise AssertionError("подменённый архивный PDF был принят из кэша")
    print("  ✓ ЕГРЮЛ: исходный PDF, дата и SHA-256 сохраняются и входят в trace")


def main():
    print("госучастие — офлайн-регрессии:")
    check_egrul_parser()
    check_direct_and_threshold()
    check_indirect_math()
    check_cycles_and_unknown()
    check_rosim_exact_name()
    check_public_legal_forms()
    check_invalid_graph_and_independent_public_edges()
    check_rosim_percent_cell()
    check_rosim_strict_source()
    check_egrul_artifact_provenance()
    print("test_state_ownership: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
