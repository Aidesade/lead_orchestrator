# -*- coding: utf-8 -*-
"""Офлайн-регрессии стадии 9 и GET-проверки лидов в CRM. Сеть и CRM НЕ нужны.

Главные инварианты, которые здесь охраняются:
  * стадия 9 НИКОГДА не бросает исключение — письмо уже ушло, падать нельзя;
  * без CRM_URL/CRM_INGEST_TOKEN стадия 9 молча выключена;
  * служебные ключи лида маппятся в контракт CRM;
  * GET-индекс перед новым сбором, наоборот, fail-closed.

Запуск:  py test_crm_push.py
"""
from __future__ import annotations

import io
import json
import pathlib
import sys
import urllib.error

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import crm_push as CRM                              # noqa: E402

LEAD = {
    "name": "АО «Рязаньавтодор»",
    "_inn": "6234065445",
    "_ogrn": "1096234000642",
    "website": "https://avtodor-rzn.ru",
    "phone": "+7 (4912) 55-01-70",
    "email": "info@avtodor-rzn.ru",
    "_industry": "construction",
    "_revenue": 6146868000,
    "_revenue_year": "2025",
    "contact_person": "Руденко Сергей Александрович",
    "_ceo_post": "Генеральный директор",
}
SENT = {"to": "rudenko@avtodor-rzn.ru", "subject": "Тема", "at": "2026-08-10 09:15:00"}


class FakeResponse(io.BytesIO):
    """Минимальный контекст-менеджер вместо ответа urlopen."""
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _configure(monkey_url="http://crm.local", monkey_token="t0ken"):
    import os
    os.environ["CRM_URL"] = monkey_url
    os.environ["CRM_INGEST_TOKEN"] = monkey_token
    os.environ["CRM_PUSH"] = "1"


def check_payload_mapping():
    payload = CRM.build_payload(LEAD, SENT, draft=False, onepager=r"C:\tmp\construction.pdf")
    assert payload["inn"] == "6234065445", payload
    assert payload["ogrn"] == "1096234000642", payload
    assert payload["company_name"] == "АО «Рязаньавтодор»", payload
    assert payload["industry"] == "construction", payload
    assert payload["revenue"] == 6146868000, payload
    assert payload["revenue_year"] == "2025", payload
    assert payload["contact_person"] == "Руденко Сергей Александрович", payload
    assert payload["contact_post"] == "Генеральный директор", payload
    assert payload["sent_to"] == "rudenko@avtodor-rzn.ru", payload
    assert payload["sent_at"] == "2026-08-10 09:15:00", payload
    assert payload["sent_draft"] is False, payload
    assert payload["onepager"] == "construction.pdf", payload
    collected = CRM.build_payload(dict(LEAD, _crm_note="письмо не отправлялось"), {}, draft=True)
    assert collected["sent_draft"] is True and collected["note"] == "письмо не отправлялось"
    assert not any(k.startswith("_") for k in payload), payload
    print("  ✓ маппинг лида в контракт CRM")


def check_disabled_without_keys():
    import os
    for key in ("CRM_URL", "CRM_INGEST_TOKEN"):
        os.environ.pop(key, None)
    assert CRM.is_configured() is False
    ok, note = CRM.push_lead(LEAD, SENT)
    assert ok is False and "не настроена" in note, note
    _configure()
    os.environ["CRM_PUSH"] = "0"
    assert CRM.is_configured() is False
    os.environ["CRM_PUSH"] = "1"
    assert CRM.is_configured() is True
    for unsafe in ("file:///tmp/crm.json", "ftp://crm.local", "https://user:pass@crm.local"):
        os.environ["CRM_URL"] = unsafe
        assert CRM.is_configured() is False, unsafe
    _configure()
    print("  ✓ без ключей стадия выключена, CRM_PUSH=0 выключает при заданных")


def check_success(monkeypatched):
    _configure()
    sent_body = {}

    def fake_urlopen(req, timeout=None):
        sent_body["url"] = req.full_url
        sent_body["token"] = req.get_header("X-ingest-token")
        sent_body["json"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse(json.dumps({"id": 7, "created": True, "status": "email_sent"}).encode())

    monkeypatched(fake_urlopen)
    ok, note = CRM.push_lead(LEAD, SENT)
    assert ok is True, note
    assert "#7" in note and "заведён" in note, note
    assert sent_body["url"].endswith("/api/leads/ingest"), sent_body
    assert sent_body["token"] == "t0ken", sent_body
    assert sent_body["json"]["company_name"] == "АО «Рязаньавтодор»", sent_body
    print("  ✓ успешная выгрузка: адрес, токен, тело, текст результата")


def check_errors_never_raise(monkeypatched):
    _configure()
    cases = [
        (urllib.error.HTTPError("u", 401, "unauth", None, io.BytesIO(b'{"detail":"bad token"}')), "токен"),
        (urllib.error.HTTPError("u", 503, "off", None, io.BytesIO(b'{"detail":"off"}')), "выключен"),
        (urllib.error.HTTPError("u", 500, "boom", None, io.BytesIO(b"")), "HTTP 500"),
        (urllib.error.URLError("connection refused"), "недоступна"),
        (RuntimeError("что-то совсем неожиданное"), "RuntimeError"),
    ]
    for exc, expect in cases:
        def fake_urlopen(req, timeout=None, _exc=exc):
            raise _exc
        monkeypatched(fake_urlopen)
        ok, note = CRM.push_lead(LEAD, SENT)
        assert ok is False, (exc, note)
        assert expect in note, (expect, note)
    print("  ✓ любая ошибка -> (False, причина), исключение наружу не летит")


def check_no_company_name(monkeypatched):
    _configure()

    def fake_urlopen(req, timeout=None):
        raise AssertionError("запрос не должен уходить без названия компании")

    monkeypatched(fake_urlopen)
    ok, note = CRM.push_lead({"_inn": "123"}, SENT)
    assert ok is False and "названия" in note, note
    print("  ✓ лид без названия компании в CRM не уходит")


def check_ping_creates_nothing(monkeypatched):
    _configure()
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append((req.full_url, req.data))
        if req.full_url.endswith("/health"):
            return FakeResponse(b'{"status":"ok"}')
        raise urllib.error.HTTPError(req.full_url, 422, "unprocessable", None, io.BytesIO(b"{}"))

    monkeypatched(fake_urlopen)
    ok, note = CRM.ping()
    assert ok is True, note
    assert "токен принят" in note, note
    bodies = [body for url, body in seen if body]
    assert bodies == [b"{}"], bodies
    print("  ✓ --ping проверяет связь и токен, но не создаёт лид")


def check_lookup_index(monkeypatched):
    """GET-индекс — жёсткая прекондиция нового сбора, а не мягкая стадия 9."""
    _configure()
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["token"] = req.get_header("X-ingest-token")
        return FakeResponse(json.dumps({
            "items": [
                {"id": 1, "inn": "6234065445", "ogrn": "1096234000642",
                 "company_name": "АО «Рязаньавтодор»"},
                {"id": 2, "inn": None, "ogrn": None,
                 "company_name": "ООО «Старый лид»"},
                {"id": 3, "inn": "1234567890", "ogrn": "1096234000643",
                 "company_name": "АО «Лид с испорченными реквизитами»"},
            ],
            "total": 3,
        }, ensure_ascii=False).encode("utf-8"))

    monkeypatched(fake_urlopen)
    index = CRM.fetch_existing_leads()
    assert index.total == 3
    assert index.inns == frozenset({"6234065445"})
    assert index.ogrns == frozenset({"1096234000642"})
    assert index.contains({"_inn": "6234065445", "name": "другое имя"})
    assert index.contains({"name": "  ООО \"Старый лид\"  "})
    assert index.contains({"name": "АО Лид с испорченными реквизитами"})
    assert "1234567890" not in index.inns and "1096234000643" not in index.ogrns
    assert not index.contains({"name": "ООО «Старый лид плюс»"})
    assert not index.contains({"name": "ООО «Старый лид+»"}), "пунктуация имени значима"
    assert seen == {
        "url": "http://crm.local/api/leads/ingest/index",
        "method": "GET",
        "token": "t0ken",
    }, seen
    print("  ✓ GET-индекс: ИНН/ОГРН, точный fallback имени, минимальный запрос")


def check_clients_index(monkeypatched):
    """Индекс клиентов: имя — полноправный ключ, ИНН приходит не у всех."""
    _configure()
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        return FakeResponse(json.dumps({
            "items": [
                {"id": 1, "name": "АО «Рязаньавтодор»", "inn": "6234065445"},
                {"id": 2, "name": "ООО «Клиент без ИНН»", "inn": None},
                {"id": 3, "name": "АО «Клиент с мусорным ИНН»", "inn": "1234567890"},
            ],
            "total": 3,
        }, ensure_ascii=False).encode("utf-8"))

    monkeypatched(fake_urlopen)
    index = CRM.fetch_existing_clients()
    assert index.supported
    assert index.total == 3
    assert index.inns == frozenset({"6234065445"}), index.inns
    assert index.contains({"_inn": "6234065445", "name": "имя не совпадает"})
    # клиент, заведённый руками: ИНН взять неоткуда, спасает только имя
    assert index.contains({"name": "  ООО \"Клиент без ИНН\"  "})
    assert index.contains({"_inn": "9999999999", "name": "ООО «Клиент без ИНН»"})
    # невалидный ИНН в ключи не попадает — иначе отсеяли бы чужую компанию
    assert "1234567890" not in index.inns
    assert not index.contains({"_inn": "1234567890", "name": "АО «Совсем другая»"})
    assert not index.contains({"name": "ООО «Клиент без ИНН плюс»"})
    assert seen["url"] == "http://crm.local/api/leads/ingest/clients", seen
    assert seen["method"] == "GET"
    print("  ✓ индекс клиентов: ИНН и имя, мусорный ИНН отбрасывается")


def check_clients_404_is_not_empty(monkeypatched):
    """404 = «CRM не умеет», а НЕ «клиентов нет» — и это должно быть видно."""
    _configure()

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"{}"))

    monkeypatched(fake_urlopen)
    index = CRM.fetch_existing_clients()
    assert index.supported is False
    assert index.total == 0
    assert not index.contains({"_inn": "6234065445", "name": "АО «Рязаньавтодор»"})
    note = CRM.client_facts(index)
    assert "НЕ РАБОТАЕТ" in note, note
    # «клиентов 0» и «отсев выключен» обязаны читаться по-разному
    empty = CRM.ExistingClients(frozenset(), frozenset(), 0)
    assert "НЕ РАБОТАЕТ" not in CRM.client_facts(empty)
    print("  ✓ 404: отсев выключен явно, а не молча выдаёт «клиентов нет»")


def check_clients_fail_closed(monkeypatched):
    """Сбой и кривая схема фатальны: тихо пустой список = рассылка по своим."""
    _configure()

    def broken_schema(req, timeout=None):
        return FakeResponse(json.dumps({"items": [{"id": 1}], "total": 1}).encode("utf-8"))

    monkeypatched(broken_schema)
    try:
        CRM.fetch_existing_clients()
    except CRM.CRMIndexError as exc:
        assert "клиент" in str(exc).lower(), exc
    else:
        raise AssertionError("клиент без названия обязан быть ошибкой")

    def bad_total(req, timeout=None):
        return FakeResponse(json.dumps(
            {"items": [{"id": 1, "name": "X", "inn": None}], "total": 7}).encode("utf-8"))

    monkeypatched(bad_total)
    try:
        CRM.fetch_existing_clients()
    except CRM.CRMIndexError:
        pass
    else:
        raise AssertionError("несогласованный total обязан быть ошибкой")

    def server_error(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, io.BytesIO(b"{}"))

    monkeypatched(server_error)
    try:
        CRM.fetch_existing_clients(attempts=1)
    except CRM.CRMIndexError:
        pass
    else:
        raise AssertionError("HTTP 500 обязан быть ошибкой, а не пустым индексом")
    print("  ✓ индекс клиентов fail-closed: схема, total и HTTP 500")


def check_lookup_fail_closed(monkeypatched):
    import os
    for key in ("CRM_URL", "CRM_INGEST_TOKEN"):
        os.environ.pop(key, None)
    try:
        CRM.fetch_existing_leads()
    except CRM.CRMIndexError as exc:
        assert "не настроена" in str(exc)
    else:
        raise AssertionError("без CRM lookup должен падать до RusProfile")

    _configure(monkey_token="top-secret-token")

    def must_not_open(_req, timeout=None):
        raise AssertionError("истёкший общий deadline начал CRM-запрос")

    monkeypatched(must_not_open)
    try:
        CRM.fetch_existing_leads(deadline=CRM.time.monotonic() - 1)
    except CRM.CRMIndexError as exc:
        assert "лимит" in str(exc).lower()
    else:
        raise AssertionError("истёкший CRM deadline был проигнорирован")

    attempts = {"n": 0}

    def flaky(req, timeout=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise urllib.error.URLError("temporary")
        return FakeResponse(b'{"items": [], "total": 0}')

    old_sleep = CRM.time.sleep
    CRM.time.sleep = lambda _seconds: None
    try:
        monkeypatched(flaky)
        assert CRM.fetch_existing_leads().total == 0
        assert attempts["n"] == 3

        def unauthorized(req, timeout=None):
            body = json.dumps({"detail": "bad top-secret-token"}).encode()
            raise urllib.error.HTTPError(req.full_url, 401, "bad", None, io.BytesIO(body))

        monkeypatched(unauthorized)
        try:
            CRM.fetch_existing_leads()
        except CRM.CRMIndexError as exc:
            assert "401" in str(exc)
            assert "top-secret-token" not in str(exc)
        else:
            raise AssertionError("401 должен быть жёсткой ошибкой")

        monkeypatched(lambda req, timeout=None: FakeResponse(
            b'{"items": [{"id": 1, "inn": "1", "company_name": "X"}], "total": 2}'))
        try:
            CRM.fetch_existing_leads()
        except CRM.CRMIndexError as exc:
            assert "total" in str(exc)
        else:
            raise AssertionError("несогласованный ответ CRM нельзя принимать")
    finally:
        CRM.time.sleep = old_sleep
    print("  ✓ GET-индекс fail-closed: конфиг, ретраи, 401, схема, редактирование токена")


def check_atomic_batch_and_no_redirect(monkeypatched):
    _configure()
    run_id = "11111111-1111-4111-8111-111111111111"
    seen = {}

    def ok(req, timeout=None):
        seen["payload"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse(json.dumps({
            "run_id": run_id, "ids": [10], "created": 1, "replayed": False,
        }).encode("utf-8"))

    monkeypatched(ok)
    result = CRM.create_researched_batch([LEAD], run_id)
    assert result["created"] == 1
    item = seen["payload"]["items"][0]
    assert "sent_draft" not in item and "sent_to" not in item
    assert item["inn"] == LEAD["_inn"]

    detail = io.BytesIO(json.dumps({"detail": {"code": "duplicate_inn"}}).encode())
    monkeypatched(lambda req, timeout=None: (_ for _ in ()).throw(
        urllib.error.HTTPError(req.full_url, 409, "conflict", None, detail)))
    try:
        CRM.create_researched_batch([LEAD], run_id, attempts=1)
    except CRM.CRMBatchError as exc:
        assert "conflict" in str(exc)
    else:
        raise AssertionError("created=false/conflict нельзя считать успешным новым лидом")

    request = urllib.request.Request("https://crm.test/x", headers={"X-Ingest-Token": "secret"})
    assert CRM._NoRedirect().redirect_request(
        request, None, 302, "found", {}, "https://evil.test/x") is None
    print("  ✓ atomic create-only batch и запрет redirect с CRM-токеном")


def main():
    original = CRM._urlopen

    def monkeypatched(fn):
        CRM._urlopen = fn

    print("CRM — офлайн-регрессии:")
    try:
        check_payload_mapping()
        check_disabled_without_keys()
        check_success(monkeypatched)
        check_errors_never_raise(monkeypatched)
        check_no_company_name(monkeypatched)
        check_ping_creates_nothing(monkeypatched)
        check_lookup_index(monkeypatched)
        check_clients_index(monkeypatched)
        check_clients_404_is_not_empty(monkeypatched)
        check_clients_fail_closed(monkeypatched)
        check_lookup_fail_closed(monkeypatched)
        check_atomic_batch_and_no_redirect(monkeypatched)
    finally:
        CRM._urlopen = original
    print("все проверки пройдены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
