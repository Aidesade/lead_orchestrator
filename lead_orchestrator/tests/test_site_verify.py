# -*- coding: utf-8 -*-
"""Офлайн-регрессии стадии ФАЗЫ 1 «сайт и телефоны» (`site_verify`).

В сеть не ходим: обход сайта получает подставной `fetch`. Проверяется то, из-за
чего стадия и появилась — телефон с сайта важнее телефона карточки, мёртвый сайт
выбрасывает компанию из сбора, а отрасль ЖКХ стадию не проходит вовсе."""
from __future__ import annotations

import contextlib
import os
import pathlib
import sys
import time
import urllib.error

HERE = pathlib.Path(__file__).resolve().parent
APP = HERE.parent / "app"          # код пайплайна лежит рядом, в app/
sys.path.insert(0, str(APP))

import site_verify as SV  # noqa: E402

# Резолв домена — тоже сеть: в тестах он всегда «домена нет», а различение
# «домена нет» / «нас не пустил сервер» проверяется по самому полю.
SV._dns_ok = lambda _url: False


def valid_test_inn(number):
    body = f"{770000000 + int(number):09d}"
    weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    control = sum(int(body[i]) * weights[i] for i in range(9)) % 11 % 10
    return body + str(control)


INN = valid_test_inn(1)
SITE = "company.test"

ROOT = f"""<html><body>
  <a href="tel:+7 (843) 292-11-22">+7 (843) 292-11-22</a>
  <footer>ООО «Компания», ИНН {INN}</footer>
</body></html>"""

CONTACTS = """<html><body>
  <p>Приёмная: 8 (843) 000-11-22</p>
  <p>Отдел закупок: (843) 100-20-30</p>
  <a href="mailto:office@company.test">office@company.test</a>
</body></html>"""

NO_PHONES = f"""<html><body><p>О компании</p><p>ИНН {INN}</p>
  <a href="mailto:office@company.test">office@company.test</a></body></html>"""


@contextlib.contextmanager
def env(**values):
    """Временно выставить переменные окружения (None — удалить)."""
    saved = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def fake_fetch(pages, calls=None):
    """Подставной загрузчик: путь -> html; чего нет — сетевая ошибка.

    Обход зовёт `fetch("http://<домен><путь>")`, поэтому домен просто отрезается:
    «http://company.test» -> «/», «http://company.test/kontakty» -> «/kontakty»."""
    def fetch(url, timeout=None):
        path = ("/" + url.split(SITE, 1)[-1].lstrip("/")).rstrip("/") or "/"
        if calls is not None:
            calls.append(path)
        if path not in pages:
            raise urllib.error.URLError("404")
        return pages[path]
    return fetch


def lead(**extra):
    row = {
        "name": "ООО «Компания»",
        "website": SITE,
        "phone": "+7 (843) 000-11-22",
        "_phones": ["+7 (843) 000-11-22"],
        "email": "",
        "_inn": INN,
        "_industry": "processing",
    }
    row.update(extra)
    return row


def check_site_phone_wins():
    """Телефоны сайта — основные, номер карточки сохраняется рядом."""
    row = lead()
    with env(LEAD_SITE_VERIFY="1", LEAD_SITE_VERIFY_SKIP=None):
        verdict = SV.verify_lead(row, fetch=fake_fetch({"/": ROOT, "/kontakty": CONTACTS}))
    assert verdict == "ok", verdict
    assert row["_site_verdict"] == "ok"
    assert row["_site_reachable"] is True
    # сайт впереди, ни один известный номер не потерян
    assert row["_phones"][0] == "+78432921122", row["_phones"]
    assert row["phone"].startswith("+78432921122"), row["phone"]
    assert row["_phone_rusprofile"] == "+7 (843) 000-11-22", row["_phone_rusprofile"]
    assert row["_phone_src"] == "офиц. сайт"
    # номер карточки нашёлся на /kontakty -> подтверждён
    assert row["_phone_verified"] is True, row.get("_site_phones")
    assert "+78430001122" in row["_site_phones"], row["_site_phones"]
    # (843) 100-20-30 без кода страны — тоже телефон, а не мусор
    assert "+78431002030" in row["_site_phones"], row["_site_phones"]
    # ИНН в реквизитах доказывает, что сайт принадлежит именно этой компании
    assert row["_site_inn_confirmed"] is True
    # почты у лида не было -> добрана с сайта
    assert row["email"] == "office@company.test", row["email"]
    assert row["_email_src"] == "офиц. сайт"


def check_card_phone_not_lost_when_site_has_other_numbers():
    """Номер карточки не найден на сайте: он остаётся в списке, но не первым."""
    row = lead(phone="+7 (999) 888-77-66", _phones=["+7 (999) 888-77-66"])
    with env(LEAD_SITE_VERIFY="1"):
        verdict = SV.verify_lead(row, fetch=fake_fetch({"/": ROOT}))
    assert verdict == "ok", verdict
    assert row["_phone_verified"] is False
    assert row["_phones"] == ["+78432921122", "+7 (999) 888-77-66"], row["_phones"]


def check_dead_site_is_dropped():
    """Сайт не отвечает -> компания выбрасывается из сбора."""
    rows = [lead(), lead(name="ООО «Вторая»")]
    with env(LEAD_SITE_VERIFY="1"):
        kept, stats = SV.gate(rows, log=lambda *_a: None,
                              fetch=fake_fetch({}), workers=2)
    assert kept == [], kept
    assert stats.get("unreachable") == 2, stats
    assert rows[0]["_site_verdict"] == "unreachable"
    assert rows[0]["_site_reachable"] is False
    # причина отказа различима: домена нет vs сервер не пустил (WAF, таймаут)
    assert rows[0]["_site_dns_ok"] is False, rows[0].get("_site_dns_ok")
    assert rows[0]["_site_error"], rows[0].get("_site_error")


def check_site_without_phones_is_dropped():
    """Сайт жив, но телефонов на нём нет -> тоже отсев (ради этого стадия и есть)."""
    row = lead()
    with env(LEAD_SITE_VERIFY="1"):
        kept, stats = SV.gate([row], log=lambda *_a: None,
                              fetch=fake_fetch({"/": NO_PHONES}))
    assert kept == [], kept
    assert stats.get("no_phone") == 1, stats
    assert row["_site_verdict"] == "no_phone"
    # телефон карточки при этом не тронут: подменять его нечем
    assert row["phone"] == "+7 (843) 000-11-22", row["phone"]


def check_missing_or_foreign_site_is_dropped():
    """Пустой сайт и соцсеть вместо сайта — не «сайт компании»."""
    with env(LEAD_SITE_VERIFY="1"):
        empty = lead(website="")
        social = lead(website="https://vk.com/company")
        kept, stats = SV.gate([empty, social], log=lambda *_a: None,
                              fetch=fake_fetch({"/": ROOT}))
    assert kept == [], kept
    assert stats.get("no_site") == 2, stats
    assert empty["_site_verdict"] == "no_site"


def check_zhkh_is_not_checked():
    """Отрасль ЖКХ стадию не проходит и из сбора НЕ выбрасывается."""
    def explode(url, timeout=None):
        raise AssertionError(f"ЖКХ не должен ходить на сайт: {url}")

    row = lead(_industry="water", website="")
    with env(LEAD_SITE_VERIFY="1", LEAD_SITE_VERIFY_SKIP=None):
        kept, stats = SV.gate([row], log=lambda *_a: None, fetch=explode)
    assert kept == [row], kept
    assert stats.get("skip") == 1, stats
    assert row["_site_verdict"] == "skip"
    assert row["phone"] == "+7 (843) 000-11-22"      # телефон карточки не тронут
    # синоним «жкх» в тумблере означает ту же отрасль
    with env(LEAD_SITE_VERIFY="1", LEAD_SITE_VERIFY_SKIP="жкх"):
        assert SV.is_skipped({"_industry": "water"}) is True
    # пустое значение тумблера — осознанное «проверять всех», включая ЖКХ
    with env(LEAD_SITE_VERIFY="1", LEAD_SITE_VERIFY_SKIP=""):
        assert SV.is_skipped({"_industry": "water"}) is False
        checked = lead(_industry="water")
        assert SV.verify_lead(checked, fetch=fake_fetch({"/": ROOT})) == "ok"


def check_disabled_stage_changes_nothing():
    """LEAD_SITE_VERIFY=0 — лиды проходят как раньше, полей стадии нет."""
    row = lead()
    with env(LEAD_SITE_VERIFY="0"):
        kept, stats = SV.gate([row], log=lambda *_a: None, fetch=fake_fetch({}))
        assert SV.site_reserve(50) == 0
    assert kept == [row], kept
    assert stats == {}, stats
    assert "_site_verdict" not in row
    assert row["phone"] == "+7 (843) 000-11-22"


def check_reserve():
    """Резерв выдачи под отсев по сайту: 30% от N, не меньше 3; тумблер — явно."""
    with env(LEAD_SITE_VERIFY="1", LEAD_SITE_RESERVE=None):
        assert SV.site_reserve(50) == 17, SV.site_reserve(50)
        assert SV.site_reserve(5) == 3, SV.site_reserve(5)
    with env(LEAD_SITE_VERIFY="1", LEAD_SITE_RESERVE="40"):
        assert SV.site_reserve(50) == 40
    with env(LEAD_SITE_VERIFY="1", LEAD_SITE_RESERVE="не число"):
        try:
            SV.site_reserve(50)
        except ValueError:
            pass
        else:
            raise AssertionError("мусор в LEAD_SITE_RESERVE обязан остановить прогон")


def check_phone_parsing():
    """Реквизиты — не телефоны: 10–13 цифр подряд в тексте номером не считаются."""
    html = (f"<p>ИНН {INN} ОГРН 1021602830370 р/с 40702810000000012345</p>"
            "<p>Телефон: 8 (843) 292-11-22</p>")
    assert SV.extract_phones(html) == ["+78432921122"], SV.extract_phones(html)
    assert SV.normalize_phone(INN) is None
    assert SV.normalize_phone("+7 843 292 11 22") == "+78432921122"
    assert SV.normalize_phone("8(843)292-11-22") == "+78432921122"
    assert SV.normalize_phone("8432921122", local_ok=True) == "+78432921122"
    assert SV.normalize_phone("8432921122") is None      # без явного разрешения — нет
    assert SV.phone_tail("+7 (843) 292-11-22") == "8432921122"
    assert SV.phone_tail("292-11-22") == ""


def check_email_verification():
    """Почта карточки подтверждается сайтом по адресу или домену, чужая — нет."""
    same = lead(email="sales@company.test")
    other = lead(email="director@mail.ru")
    with env(LEAD_SITE_VERIFY="1"):
        pages = {"/": ROOT, "/kontakty": CONTACTS}
        assert SV.verify_lead(same, fetch=fake_fetch(pages)) == "ok"
        assert SV.verify_lead(other, fetch=fake_fetch(pages)) == "ok"
    assert same["_email_verified"] is True
    assert other["_email_verified"] is False
    # существующая почта не подменяется найденной на сайте
    assert other["email"] == "director@mail.ru"


def check_budget_and_deadline():
    """Обход ограничен бюджетом страниц, а истёкший дедлайн — отдельный вердикт."""
    calls = []
    row = lead(phone="", _phones=[])
    with env(LEAD_SITE_VERIFY="1"):
        SV.verify_lead(row, pages=2, fetch=fake_fetch({"/kontakty": CONTACTS}, calls))
    assert len(calls) <= 2, calls          # мёртвый root тоже тратит бюджет
    row2 = lead()
    with env(LEAD_SITE_VERIFY="1"):
        verdict = SV.verify_lead(row2, deadline=time.monotonic() - 1,
                                 fetch=fake_fetch({"/": ROOT}))
    assert verdict == "deadline", verdict
    assert row2["_site_verdict"] == "deadline"


def check_gate_keeps_order_and_stats():
    """Гейт сохраняет порядок оставшихся и честно считает вердикты."""
    good1 = lead(name="ООО «Первая»")
    dead = lead(name="ООО «Мёртвая»", website="dead.test")
    good2 = lead(name="ООО «Третья»")
    zhkh = lead(name="ООО «Водоканал»", _industry="water", website="")
    pages = {"/": ROOT, "/kontakty": CONTACTS}

    def fetch(url, timeout=None):
        if "dead.test" in url:
            raise urllib.error.URLError("timeout")
        return fake_fetch(pages)(url, timeout)

    with env(LEAD_SITE_VERIFY="1", LEAD_SITE_VERIFY_SKIP=None):
        kept, stats = SV.gate([good1, dead, good2, zhkh], log=lambda *_a: None,
                              fetch=fetch, workers=4)
    assert [row["name"] for row in kept] == [
        "ООО «Первая»", "ООО «Третья»", "ООО «Водоканал»"], [r["name"] for r in kept]
    assert stats.get("ok") == 2 and stats.get("unreachable") == 1, stats
    assert stats.get("skip") == 1, stats


def check_wired_into_phase1():
    """Стадия подключена к ОБОИМ сборам RusProfile, а не только в CLI-флаге.

    Гейт живёт в коде сбора, и его молчаливое удаление никакой другой тест не
    заметит: прогон просто снова начнёт отдавать компании с мёртвыми телефонами."""
    import inspect

    import orchestrator  # noqa: WPS433 — импорт тяжёлый, поэтому внутри проверки
    import state_lead_collection as SLC

    collect = inspect.getsource(orchestrator._collect)
    assert "SV.gate(" in collect, "отраслевой сбор больше не проверяет сайт"
    assert "SV.site_reserve(" in collect, "резерв выдачи под отсев по сайту потерян"
    state = inspect.getsource(SLC._with_session)
    assert "SV.verify_lead(" in state, "госдобор больше не проверяет сайт"
    assert "SV.DROP_VERDICTS" in state, "госдобор перестал отсеивать по вердикту"
    cli = inspect.getsource(orchestrator.main)
    assert "--no-site-verify" in cli, "флаг отключения стадии пропал из CLI"


def main():
    check_site_phone_wins()
    check_card_phone_not_lost_when_site_has_other_numbers()
    check_dead_site_is_dropped()
    check_site_without_phones_is_dropped()
    check_missing_or_foreign_site_is_dropped()
    check_zhkh_is_not_checked()
    check_disabled_stage_changes_nothing()
    check_reserve()
    check_phone_parsing()
    check_email_verification()
    check_budget_and_deadline()
    check_gate_keeps_order_and_stats()
    check_wired_into_phase1()
    print("test_site_verify: OK — сайт с телефонами обязателен, телефон сайта "
          "приоритетнее карточки, ЖКХ стадию не проходит")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
