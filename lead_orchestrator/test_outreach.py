# -*- coding: utf-8 -*-
"""Офлайн-регрессии outreach-пайплайна: реестр, выбор получателя, сборка письма.

Ни сети, ни COM, ни модели — всё проверяемое здесь детерминировано.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

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

    final = LT.assemble({"subject": "Тема", "body": "Тело письма."})
    assert LT.OPT_OUT in final["body"], "строка отказа обязательна в каждом письме"
    assert LT.VENDOR_REQUISITES in final["body"], "реквизиты обязательны"
    # В подписи ОБА контакта: письмо уходит с ящика одного человека (ответ придёт
    # туда), а телефон в приложенном one-pager принадлежит другому. Один контакт
    # означал бы, что адресат пишет одному, а звонит другому и не понимает, кто есть кто.
    for value in (LT.CONTACT1_NAME, LT.CONTACT1_EMAIL, LT.CONTACT1_PHONE,
                  LT.CONTACT2_NAME, LT.CONTACT2_EMAIL, LT.CONTACT2_PHONE):
        assert value in final["body"], f"в подписи нет «{value}»"


# ----------------------------------------------------- прекондишен стадии 4 ----
def check_verifier_precondition() -> None:
    """Облачный добор — самостоятельный слой, но не замена верификатора ЦИТ РТ."""
    import email_guess as EG

    saved = {k: os.environ.get(k) for k in
             ("EMAIL_VERIFIER_URL", "EMAIL_MEV_ENABLE", "EMAIL_MEV_API_KEY")}
    saved_credits = EG.mev_credits
    try:
        os.environ.pop("EMAIL_VERIFIER_URL", None)
        os.environ.pop("EMAIL_MEV_ENABLE", None)
        os.environ.pop("EMAIL_MEV_API_KEY", None)

        # Слой выключен и верификатора нет -> прежнее поведение: жёсткая ошибка
        try:
            OUT.check_verifier(required=True)
            raise AssertionError("без верификатора стадия 4 обязана падать")
        except SystemExit as exc:
            assert "EMAIL_VERIFIER_URL" in str(exc), str(exc)
        assert OUT.check_verifier(required=False)["alive"] is False

        # Живой облачный слой заменяет отсутствующий верификатор ЦИТ РТ, но
        # раскрывает остаток квоты до первой компании, а не на сотой.
        os.environ["EMAIL_MEV_ENABLE"] = "1"
        os.environ["EMAIL_MEV_API_KEY"] = "K"
        EG.mev_credits = lambda *a, **kw: (4200, "")
        got = OUT.check_verifier(required=True)
        assert got["alive"] is False, "верификатора ЦИТ РТ всё ещё нет"
        assert got["mev"]["credits"] == 4200, got

        # Битый ключ облака не отменяет требования иметь хоть какой-то верификатор
        EG.mev_credits = lambda *a, **kw: (None, "unauthorized")
        try:
            OUT.check_verifier(required=True)
            raise AssertionError("мёртвый облачный слой не должен считаться верификатором")
        except SystemExit:
            pass
    finally:
        EG.mev_credits = saved_credits
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> int:
    check_registry()
    check_backfill()
    check_audit()
    check_pick_recipient()
    check_recipients()
    check_letter()
    check_verifier_precondition()
    print("test_outreach: OK — реестр атомарен, отвергнутые адреса не рассылаются, "
          "подпись и отписка дописываются кодом")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
