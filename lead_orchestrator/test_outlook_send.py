# -*- coding: utf-8 -*-
"""Офлайн-регрессии транспорта Outlook: выбор аккаунта, сборка письма, черновик vs отправка.

Запущенный Outlook НЕ нужен — COM-объекты подменены фальшивками. Главный инвариант,
который здесь охраняется: по умолчанию письмо СОХРАНЯЕТСЯ, а не отправляется.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import outlook_send as MAIL                         # noqa: E402


class FakeOle:
    def __init__(self):
        self.calls = []

    def Invoke(self, *args):
        self.calls.append(args)


class FakeAttachments:
    def __init__(self):
        self.added = []

    def Add(self, path):
        self.added.append(path)


class FakeMail:
    def __init__(self):
        self._oleobj_ = FakeOle()
        self.Attachments = FakeAttachments()
        self.saved = 0
        self.sent = 0
        self.HTMLBody = None

    def Save(self):
        self.saved += 1

    def Send(self):
        self.sent += 1


class FakeAccount:
    def __init__(self, smtp="", display="", raises=False):
        self._smtp = smtp
        self.DisplayName = display
        self._raises = raises

    @property
    def SmtpAddress(self):
        if self._raises:
            raise RuntimeError("COM: свойство недоступно")
        return self._smtp


def check_pick_account() -> None:
    tatar = FakeAccount(smtp="Artur.Bayrashev@tatar.ru")
    other = FakeAccount(smtp="someone@outlook.com")
    acc, addr = MAIL.pick_account([other, tatar], "tatar.ru")
    assert acc is tatar and addr == "Artur.Bayrashev@tatar.ru", addr

    # точное совпадение адреса весомее подстроки
    lookalike = FakeAccount(smtp="fake@tatar.ru.example.com")
    exact = FakeAccount(smtp="mail@tatar.ru")
    acc, addr = MAIL.pick_account([lookalike, exact], "mail@tatar.ru")
    assert addr == "mail@tatar.ru", addr

    assert MAIL.pick_account([other], "tatar.ru") == (None, "")
    assert MAIL.pick_account([], "tatar.ru") == (None, "")

    # у Exchange-аккаунта SmtpAddress может отсутствовать — адрес берётся из DisplayName
    exchange = FakeAccount(display="Artur.Bayrashev@tatar.ru", raises=True)
    assert MAIL.account_address(exchange) == "Artur.Bayrashev@tatar.ru"
    assert MAIL.account_address(FakeAccount()) == ""


def check_validate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        pdf = os.path.join(tmp, "onepager.pdf")
        with open(pdf, "wb") as fh:
            fh.write(b"%PDF-1.4")
        clean = MAIL.validate({"to": " boss@company.ru ", "subject": " Тема ",
                               "body": " Текст ", "attachments": [pdf]})
        assert clean["to"] == "boss@company.ru"
        assert clean["attachments"] == [os.path.abspath(pdf)]
        assert clean["cc"] == "" and clean["html"] == ""

        for bad, why in (
                ({"subject": "Т", "body": "Т"}, "без адресата"),
                ({"to": "a@b.ru", "body": "Т"}, "без темы"),
                ({"to": "a@b.ru", "subject": "Т"}, "без тела"),
                ({"to": "a@b.ru", "subject": "Т", "body": "Т",
                  "attachments": [os.path.join(tmp, "нет.pdf")]}, "вложение отсутствует")):
            try:
                MAIL.validate(bad)
            except ValueError:
                continue
            raise AssertionError(f"должно было отвергнуться: {why}")


def check_compose() -> None:
    mail = FakeMail()
    acc = FakeAccount(smtp="Artur.Bayrashev@tatar.ru")
    msg = MAIL.validate({"to": "boss@company.ru", "cc": "info@company.ru",
                         "subject": "Тема", "body": "Текст"})
    MAIL.compose(mail, msg, acc)
    # отправляющий аккаунт выставляется через Invoke: обычный setattr Outlook не примет
    assert mail._oleobj_.calls, "SendUsingAccount не выставлен — письмо уйдёт не с того ящика"
    dispid, _a, _b, _c, passed = mail._oleobj_.calls[0]
    assert dispid == MAIL._DISPID_SEND_USING_ACCOUNT
    assert passed is acc
    assert mail.To == "boss@company.ru" and mail.CC == "info@company.ru"
    assert mail.Subject == "Тема" and mail.Body == "Текст"
    assert mail.HTMLBody is None, "HTML не задавали — Outlook должен слать текст"
    assert mail.Attachments.added == []

    with tempfile.TemporaryDirectory() as tmp:
        pdf = os.path.join(tmp, "p.pdf")
        with open(pdf, "wb") as fh:
            fh.write(b"%PDF-1.4")
        mail2 = FakeMail()
        MAIL.compose(mail2, MAIL.validate(
            {"to": "a@b.ru", "subject": "Т", "body": "Т", "attachments": [pdf]}), acc)
        assert mail2.Attachments.added == [os.path.abspath(pdf)], mail2.Attachments.added


def check_deliver() -> None:
    draft = FakeMail()
    assert MAIL.deliver(draft, draft=True) == "draft"
    assert (draft.saved, draft.sent) == (1, 0), "черновик не должен уходить наружу"

    real = FakeMail()
    assert MAIL.deliver(real, draft=False) == "sent"
    assert (real.saved, real.sent) == (0, 1)

    # дефолт функции — именно черновик; если однажды поменяют, тест обязан упасть
    default = FakeMail()
    MAIL.deliver(default)
    assert (default.saved, default.sent) == (1, 0), "по умолчанию письмо НЕ отправляется"


def main() -> int:
    check_pick_account()
    check_validate()
    check_compose()
    check_deliver()
    print("test_outlook_send: OK — аккаунт tatar.ru, вложение прикладывается, "
          "по умолчанию черновик")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
