# -*- coding: utf-8 -*-
"""Офлайн-регрессии outreach-пайплайна: реестр, выбор получателя, сборка письма.

Ни сети, ни COM, ни модели — всё проверяемое здесь детерминировано.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import tempfile
from types import SimpleNamespace

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import outreach as OUT                              # noqa: E402
import outreach_letter as LT                        # noqa: E402
import outreach_registry as REG                     # noqa: E402


def _row(email, *, scheme="S.N", score=100, verdict="не проверялся", confidence="средняя"):
    return {"email": email, "scheme": scheme, "score": score,
            "verdict": verdict, "confidence": confidence}


# ------------------------------------------------------------------ реестр ----
def check_registry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "nested", "registry.json")
        reg = REG.Registry(path)
        lead = {"_inn": "7712345678", "name": "ООО «Тест»", "_industry": "mining",
                "contact_person": "Иванов Иван Иванович", "_ceo_post": "Директор",
                "website": "test.ru"}
        reg.upsert(lead)
        reg.mark("7712345678", "parsed", REG.OK)
        reg.save()
        assert os.path.isfile(path), "реестр не сохранён"
        assert not os.path.exists(path + ".tmp"), "временный файл не убран — запись не атомарна"

        again = REG.Registry(path)
        rec = again.get("7712345678")
        assert rec["name"] == "ООО «Тест»"
        assert rec["ceo"] == {"fio": "Иванов Иван Иванович", "post": "Директор"}
        assert again.stage_status("7712345678", "parsed") == REG.OK

        # причина для skip/fail обязательна: стадия 8 иначе бесполезна
        try:
            again.mark("7712345678", "email", REG.SKIP)
        except ValueError:
            pass
        else:
            raise AssertionError("skip без причины должен отвергаться")
        try:
            again.mark("7712345678", "нет_такой", REG.OK)
        except ValueError:
            pass
        else:
            raise AssertionError("неизвестная стадия должна отвергаться")

        # черновик — ещё не отправка: компания обязана остаться в работе
        again.mark_sent("7712345678", "i@test.ru", "Тема", draft=True)
        assert again.was_sent("7712345678") is False
        assert again.is_worked("7712345678") is False
        again.mark_sent("7712345678", "i@test.ru", "Тема", draft=False)
        assert again.was_sent("7712345678") is True
        assert again.is_worked("7712345678") is True

        fresh = again.filter_new([lead, {"_inn": "5000000000", "name": "Другая"}])
        assert [l.get("name") for l in fresh] == ["Другая"], fresh
        # лид без ИНН не отсеиваем: опознать повтор по названию нельзя
        assert len(again.filter_new([{"name": "Безымянная"}])) == 1


def check_backfill() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        deliverables = os.path.join(tmp, "deliverables")
        real = os.path.join(deliverables, "1655505808")
        os.makedirs(real)
        for name in ("a_карта_бизнес-процессов.docx", "a_карта_ролей.docx"):
            with open(os.path.join(real, name), "wb") as fh:
                fh.write(b"x" * (REG.REAL_DOCX_MIN + 10))
        # заготовка меньше порога — компания НЕ отработана
        thin = os.path.join(deliverables, "7700000001")
        os.makedirs(thin)
        with open(os.path.join(thin, "b.docx"), "wb") as fh:
            fh.write(b"x" * 100)
        # ручной бэкап с суффиксом — не ИНН, в реестр не идёт
        os.makedirs(os.path.join(deliverables, "1655505808_before_backup"))

        reg = REG.Registry(os.path.join(tmp, "registry.json"))
        added = reg.backfill_deliverables(root=deliverables, log=lambda *a: None)
        assert added == 1, added
        assert reg.is_worked("1655505808") is True
        assert reg.is_worked("7700000001") is False
        assert "1655505808_before_backup" not in reg.companies

        # повторный бэкфилл не плодит записи
        assert reg.backfill_deliverables(root=deliverables, log=lambda *a: None) == 0


def check_audit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        reg = REG.Registry(os.path.join(tmp, "r.json"))
        reg.mark("100", "parsed", REG.OK)
        reg.mark("100", "onepager", REG.SKIP, "нет готового файла отрасли")
        reg.mark("200", "parsed", REG.OK)
        for stage in ("onepager", "email", "letter", "sent"):
            reg.mark("200", stage, REG.OK)
        # компания из бэкфилла: материалы есть, рассылку не запускали — это не «застряла»
        reg.companies.setdefault("300", {"inn": "300", "stages": {}})["deliverables"] = True

        rows = {row["inn"]: row for row in reg.audit()}
        assert rows["100"]["state"] == "частично"
        assert rows["100"]["last_ok"] == "parsed"
        missed = {item["stage"]: item["reason"] for item in rows["100"]["missing"]}
        assert "нет готового файла отрасли" in missed["onepager"]
        # пропуск одной стадии не должен прятать остальные: их четыре, а не одна
        assert set(missed) == {"onepager", "email", "letter", "sent"}, missed
        assert "не выполнялась" in missed["sent"]

        assert rows["200"]["state"] == "готово" and rows["200"]["missing"] == []
        assert rows["300"]["state"] == "ранее", rows["300"]

        text = REG.format_audit(reg.audit())
        assert "нет готового файла отрасли" in text
        assert "пройдено полностью: 1" in text and "с пропусками: 1" in text
        # бэкфилл-компании свёрнуты в одну строку, а не вываливаются списком
        assert "рассылка по ним не запускалась" in text
        assert "300" not in text


# ------------------------------------------------------- выбор получателя ----
def check_pick_recipient() -> None:
    lead = {"contact_person": "Гребнева Татьяна Николаевна", "email": "info@company.ru"}

    # адрес, отвергнутый почтовым сервером, не должен попасть в письмо никогда
    guess = {"people": [{"fio": "Гребнева Татьяна Николаевна", "emails": [
        _row("grebneva@company.ru", verdict="no", score=900),
        _row("t.grebneva@company.ru", score=500),
    ]}]}
    picked = OUT.pick_recipient(guess, lead)
    assert picked["email"] == "t.grebneva@company.ru", picked
    assert picked["confirmed"] is False

    # опубликованный компанией адрес весомее любой гипотезы с большим счётом
    guess = {"people": [{"fio": "Гребнева Татьяна Николаевна", "emails": [
        _row("hypothesis@company.ru", score=999),
        _row("grebneva@company.ru", scheme="опубликован", score=1,
             confidence="подтверждён (опубликован компанией)", verdict="не требуется"),
    ]}]}
    picked = OUT.pick_recipient(guess, lead)
    assert picked["email"] == "grebneva@company.ru", picked
    assert picked["confirmed"] is True

    # руководитель важнее других сотрудников, даже если он идёт вторым
    guess = {"people": [
        {"fio": "Петров Пётр Петрович", "emails": [_row("petrov@company.ru", score=999)]},
        {"fio": "Гребнева Татьяна Николаевна", "emails": [_row("grebneva@company.ru")]},
    ]}
    assert OUT.pick_recipient(guess, lead)["email"] == "grebneva@company.ru"

    # «НЕ отправлять» по домену — тоже полный запрет
    guess = {"people": [{"fio": "Гребнева Татьяна Николаевна", "emails": [
        _row("x@company.ru", confidence="НЕ отправлять (домен почту не принимает)")]}]}
    assert OUT.pick_recipient(guess, lead) is None
    assert OUT.pick_recipient({"people": []}, lead) is None


def check_recipients() -> None:
    lead = {"email": "info@company.ru"}
    # гипотеза дублируется на общую почту, подтверждённый адрес — нет
    to, cc, note = OUT.build_recipients(
        {"email": "t.grebneva@company.ru", "confirmed": False}, lead)
    assert (to, cc) == ("t.grebneva@company.ru", "info@company.ru"), (to, cc)
    assert "расчётный" in note
    to, cc, note = OUT.build_recipients(
        {"email": "t.grebneva@company.ru", "confirmed": True}, lead)
    assert cc == "" and note == ""
    # общая почта совпала с адресом ЛПР — копия самому себе не нужна
    to, cc, _n = OUT.build_recipients(
        {"email": "Info@company.ru", "confirmed": False}, lead)
    assert cc == ""


# ------------------------------------------------------------------ письмо ----
def check_letter() -> None:
    assert LT.salutation("Гребнева Татьяна Николаевна") == "Татьяна Николаевна"
    # без отчества обращение по имени от незнакомого отправителя фамильярно
    assert LT.salutation("Гребнева Татьяна") == ""
    assert LT.salutation('ООО "Управляющая компания"') == ""

    lead = {"name": "ООО «Тест»", "_inn": "1", "_industry": "mining",
            "contact_person": "Гребнева Татьяна Николаевна", "_ceo_post": "Директор"}
    prompt = LT.build_prompt(lead, pain="Простои оборудования")
    assert "Татьяна Николаевна" in prompt
    assert "Простои оборудования" in prompt
    # то, чего мы не знаем, названо явно — иначе модель это домыслит
    assert "НЕ ИЗВЕСТНО" in prompt and "Регион" in prompt.split("НЕ ИЗВЕСТНО")[1]

    org_lead = dict(lead, contact_person='ООО "Управляющая компания Актив"')
    assert "Обращайся без имени" in LT.build_prompt(org_lead)

    # без one-pager модели прямо запрещается писать «во вложении»: получатель иначе
    # будет искать файл, которого в письме нет
    assert "ВЛОЖЕНИЕ: НЕТ" in LT.build_prompt(lead, has_attachment=False)
    assert "ВЛОЖЕНИЕ: есть" in LT.build_prompt(lead, has_attachment=True)

    body = "Здравствуйте.\n" + "Текст письма про платформу. " * 20
    reply = LT.parse_reply('```json\n' + json.dumps(
        {"subject": "Тема письма про ИИ", "body": body}, ensure_ascii=False) + '\n```')
    assert reply["subject"] == "Тема письма про ИИ"

    # модель всё-таки подписалась — контакты вырезаются, иначе их будет два набора
    with_contacts = body + "\n\nС уважением, Иван\nтел. +7 999 123-45-67\nivan@vendor.ru"
    cleaned = LT.parse_reply(json.dumps(
        {"subject": "Тема", "body": with_contacts}, ensure_ascii=False))
    assert "+7 999" not in cleaned["body"] and "ivan@vendor.ru" not in cleaned["body"]
    assert "С уважением, Иван" in cleaned["body"], "вырезать надо строки с контактами, а не всё"

    for bad in ("не json", json.dumps({"subject": "", "body": body}),
                json.dumps({"subject": "Тема", "body": "коротко"}),
                json.dumps({"subject": "Т" * 200, "body": body})):
        try:
            LT.parse_reply(bad)
        except LT.LetterError:
            continue
        raise AssertionError(f"должно было отвергнуться: {bad[:40]}")

    # Стадия 6б: адрес должен отсеиваться ДО письма. Появилась после боевого
    # прогона, где предложение ушло на corruption@ — ящик для сообщений о коррупции.
    import checko_enrich as CE
    for addr in ("corruption@x.ru", "compliance@x.ru", "hr@x.ru", "vacancy@x.ru",
                 "pretenzii@x.ru", "legal@x.ru", "postmaster@x.ru"):
        kind, _ = CE._classify_email(addr)
        assert kind == "служебная", f"{addr} должен быть служебным, а не «{kind}»"
    for addr in ("ivanov@x.ru", "eremin.a@x.ru", "zakupki@x.ru", "info@x.ru"):
        kind, _ = CE._classify_email(addr)
        assert kind != "служебная", f"{addr} ошибочно признан служебным"

    final = LT.assemble({"subject": "Тема", "body": "Тело письма."})
    assert LT.OPT_OUT in final["body"], "строка отказа обязательна в каждом письме"
    assert LT.VENDOR_REQUISITES in final["body"], "реквизиты обязательны"
    # В подписи ОБА контакта: письмо уходит с ящика одного человека (ответ придёт
    # туда), а телефон в приложенном one-pager принадлежит другому. Один контакт
    # означал бы, что адресат пишет одному, а звонит другому и не понимает, кто есть кто.
    for value in (LT.CONTACT1_NAME, LT.CONTACT1_EMAIL, LT.CONTACT1_PHONE,
                  LT.CONTACT2_NAME, LT.CONTACT2_EMAIL, LT.CONTACT2_PHONE):
        assert value in final["body"], f"в подписи нет «{value}»"


# --------------------------------------------------- пробивка ящиков удалена ---
def check_no_verifier_stage() -> None:
    """Отказ от пробивки ящиков (2026-08-18) необратим на уровне кода.

    Вернуть check_verifier/SMTP-пробу «по памяти» не получится молча: тест
    зафиксирует и сам факт, и то, что стадия 6 зовёт подбор без SMTP."""
    assert not hasattr(OUT, "check_verifier"), \
        "check_verifier вернулся: от пробивки ящиков отказались"
    assert not hasattr(OUT, "_check_mev"), \
        "_check_mev вернулся: облачный добор из пайплайна убран"
    import inspect
    src = inspect.getsource(OUT.stage_email)
    assert "smtp=False" in src, "стадия 6 обязана строить гипотезы БЕЗ SMTP-пробы"


def check_client_filter() -> None:
    """Действующим клиентам не пишем, а невозможность это проверить блокирует --send."""
    import crm_push as CRM

    leads = [
        {"_inn": "6234065445", "name": "АО «Рязаньавтодор»"},   # клиент по ИНН
        {"_inn": "1655206692", "name": "ООО «Клиент без ИНН»"},  # клиент по имени
        {"_inn": "7712040126", "name": "ПАО «Аэрофлот»"},        # не клиент
    ]
    index = CRM.ExistingClients(
        frozenset({"6234065445"}),
        frozenset({CRM._normal_name("ООО «Клиент без ИНН»")}),
        2,
    )

    original_fetch, original_conf = CRM.fetch_existing_clients, CRM.is_configured
    try:
        CRM.is_configured = lambda: True
        CRM.fetch_existing_clients = lambda *a, **k: index
        kept = OUT.filter_clients(leads, sending=True)
        assert [lead["_inn"] for lead in kept] == ["7712040126"], kept

        # CRM недоступна: черновики продолжаем, реальную отправку — нет.
        def boom(*a, **k):
            raise CRM.CRMIndexError("индекс клиентов CRM недоступен: connection refused")

        CRM.fetch_existing_clients = boom
        assert OUT.filter_clients(leads, sending=False) == leads, \
            "в режиме черновиков сбой CRM не должен останавливать прогон"
        try:
            OUT.filter_clients(leads, sending=True)
        except SystemExit as exc:
            assert "--send" in str(exc), exc
        else:
            raise AssertionError("с --send недоступный индекс клиентов обязан быть стопом")

        # Старая CRM (404): отсева нет, но и прогон не рушится.
        CRM.fetch_existing_clients = lambda *a, **k: CRM.ExistingClients(
            frozenset(), frozenset(), 0, supported=False)
        assert OUT.filter_clients(leads, sending=True) == leads

        # CRM не настроена вовсе — легальный режим, отсекать нечем.
        CRM.is_configured = lambda: False
        CRM.fetch_existing_clients = boom
        assert OUT.filter_clients(leads, sending=True) == leads
    finally:
        CRM.fetch_existing_clients, CRM.is_configured = original_fetch, original_conf
    print("  ✓ отсев клиентов: по ИНН и имени, стоп на --send, мягко на черновиках")


def check_sent_persisted_before_crm() -> None:
    """Необратимая отправка должна пережить остановку процесса внутри CRM."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "registry.json")
        reg = REG.Registry(path)
        lead = {"_inn": "7712345678", "name": "ООО Тест",
                "contact_person": "Иванов Иван Иванович"}
        args = SimpleNamespace(
            generate_onepager=False, dry_run=False, use_lead_email=False,
            send=True, no_crm=False)
        saved = {
            name: getattr(OUT, name) for name in (
                "stage_onepager", "stage_email", "check_recipient",
                "stage_letter", "stage_send", "stage_crm")
        }
        observed = []

        async def onepager(*_args, **_kwargs):
            return "", "нет one-pager"

        async def email(*_args, **_kwargs):
            return {"email": "ivanov@example.ru", "confirmed": True}, ""

        async def letter(*_args, **_kwargs):
            return {"subject": "Тема", "body": "Текст"}, ""

        def interrupted_crm(*_args, **_kwargs):
            observed.append(REG.Registry(path).was_sent("7712345678"))
            raise KeyboardInterrupt("остановка в CRM")

        try:
            OUT.stage_onepager = onepager
            OUT.stage_email = email
            OUT.check_recipient = lambda *_args, **_kwargs: (True, "")
            OUT.stage_letter = letter
            OUT.stage_send = lambda *_args, **_kwargs: (
                {"to": "ivanov@example.ru", "subject": "Тема"}, "")
            OUT.stage_crm = interrupted_crm
            try:
                asyncio.run(OUT.process(lead, 1, reg, args, tmp))
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError("остановка CRM была проглочена тестовым контуром")
        finally:
            for name, value in saved.items():
                setattr(OUT, name, value)
        assert observed == [True], "факт отправки не сохранён до CRM"
    print("  ✓ факт отправки атомарно сохранён до необязательной CRM")


def main() -> int:
    check_registry()
    check_backfill()
    check_audit()
    check_pick_recipient()
    check_recipients()
    check_letter()
    check_no_verifier_stage()
    check_client_filter()
    check_sent_persisted_before_crm()
    print("test_outreach: OK — реестр атомарен, отвергнутые адреса не рассылаются, "
          "подпись и отписка дописываются кодом")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
