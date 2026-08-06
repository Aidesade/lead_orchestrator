# -*- coding: utf-8 -*-
"""Офлайн-регрессии EWS-транспорта (Exchange ЦИТ РТ через коннектор mcp-mail).

Ни сети, ни exchangelib, ни коннектора не нужно — они подменены фальшивками.
Главный охраняемый инвариант: ЧЕРНОВИК СОХРАНЯЕТ ВЛОЖЕНИЕ. Их собственный
create_draft вложения теряет, ради этого и написан свой путь; если однажды его
«упростят» обратно к вызову create_draft, этот тест обязан упасть.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import types

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import mail_ews as EWS                              # noqa: E402

ENV_KEYS = ("OUTREACH_EWS_ADDRESS", "OUTREACH_EWS_USER", "OUTREACH_EWS_PASSWORD",
            "OUTREACH_EWS_ENDPOINT")


class _Saved:
    """Фальшивый exchangelib.Message: запоминает всё, что на нём делали."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.attachments = []
        self.saved = 0
        self.sent = 0
        self.folder = None
        self.id = "draft-1"

    def attach(self, attachment):
        self.attachments.append(attachment)

    def save(self):
        self.saved += 1

    def send_and_save(self):
        self.sent += 1


class _Attachment:
    def __init__(self, name="", content=b""):
        self.name = name
        self.content = content


class _Folder:
    name = "Черновики"


class _Account:
    drafts = _Folder()


class _FakeTransport:
    """Фальшивый EwsTransport: тот же интерфейс, что у настоящего."""

    def __init__(self):
        self._account = _Account()
        self.sent = []

    def send(self, raw_mime, message_id):
        self.sent.append((raw_mime, message_id))

    def probe(self):
        return {"ews": {"ok": True}}


def _fake_exchangelib(created):
    """Подменить exchangelib: _save_draft импортирует его внутри функции."""
    mod = types.ModuleType("exchangelib")

    def message(**kwargs):
        item = _Saved(**kwargs)
        created.append(item)
        return item

    mod.Message = message
    mod.FileAttachment = _Attachment
    mod.HTMLBody = str
    return mod


def _with_env(**over):
    """Контекст с заданными переменными EWS (и восстановлением прежних)."""
    saved = {k: os.environ.get(k) for k in ENV_KEYS}

    class _Ctx:
        def __enter__(self):
            for key in ENV_KEYS:
                os.environ.pop(key, None)
            os.environ.update(over)
            return self

        def __exit__(self, *exc):
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            return False

    return _Ctx()


FULL_ENV = {
    "OUTREACH_EWS_ADDRESS": "Artur.Bayrashev@tatar.ru",
    "OUTREACH_EWS_USER": "TATAR\\bayrashev",
    "OUTREACH_EWS_PASSWORD": "secret-must-not-leak",
}


def check_env() -> None:
    with _with_env():
        try:
            EWS.transport_env()
        except EWS.TransportError as exc:
            # в ошибке перечислены ВСЕ недостающие переменные, а не первая
            for key in ("OUTREACH_EWS_ADDRESS", "OUTREACH_EWS_USER", "OUTREACH_EWS_PASSWORD"):
                assert key in str(exc), exc
        else:
            raise AssertionError("пустое окружение должно отвергаться")

    with _with_env(**FULL_ENV):
        env = EWS.transport_env()
        assert env["EMAIL_ADDRESS"] == "Artur.Bayrashev@tatar.ru"
        assert env["EMAIL_USER"] == "TATAR\\bayrashev"
        assert env["EMAIL_EWS_ENDPOINT"] == EWS.DEFAULT_ENDPOINT, env
        assert "mail.tatar.ru" in env["EMAIL_EWS_ENDPOINT"]

    with _with_env(**dict(FULL_ENV, OUTREACH_EWS_ENDPOINT="https://ews.example.ru/x")):
        assert EWS.transport_env()["EMAIL_EWS_ENDPOINT"] == "https://ews.example.ru/x"


def check_build_mime(pdf) -> None:
    import email
    from email import policy

    raw, msg_id = EWS.build_mime(
        {"to": "boss@company.ru", "cc": "info@company.ru", "subject": "Тема",
         "body": "Текст письма.", "attachments": [pdf]},
        sender="Artur.Bayrashev@tatar.ru")
    assert msg_id and msg_id.startswith("<")
    parsed = email.message_from_bytes(raw, policy=policy.default)
    assert parsed["To"] == "boss@company.ru" and parsed["Cc"] == "info@company.ru"
    assert parsed["Subject"] == "Тема"
    assert parsed["From"] == "Artur.Bayrashev@tatar.ru"
    assert "Текст письма." in parsed.get_body(preferencelist=("plain",)).get_content()
    files = list(parsed.iter_attachments())
    assert len(files) == 1, files
    assert files[0].get_filename() == os.path.basename(pdf)
    # тип вложения Exchange восстанавливает по имени, но в MIME он тоже должен быть верным
    assert files[0].get_content_type() == "application/pdf", files[0].get_content_type()

    # скрытая копия у них молча теряется — падаем, а не отправляем «в никуда»
    try:
        EWS.build_mime({"to": "a@b.ru", "subject": "Т", "body": "Т",
                        "bcc": "secret@b.ru", "attachments": []})
    except EWS.TransportError as exc:
        assert "скрыт" in str(exc).lower(), exc
    else:
        raise AssertionError("bcc должен отвергаться, а не пропадать молча")


def check_draft_keeps_attachment(pdf) -> None:
    """Тот самый инвариант, ради которого написан свой путь черновика."""
    created = []
    sys.modules["exchangelib"] = _fake_exchangelib(created)
    try:
        transport = _FakeTransport()
        raw, _mid = EWS.build_mime(
            {"to": "boss@company.ru", "cc": "", "subject": "Тема",
             "body": "Текст.", "attachments": [pdf]}, sender="me@tatar.ru")
        res = EWS._save_draft(transport, raw)
    finally:
        sys.modules.pop("exchangelib", None)

    assert len(created) == 1, created
    item = created[0]
    assert item.saved == 1, "черновик обязан сохраняться"
    assert item.sent == 0, "черновик НЕ должен отправляться"
    assert len(item.attachments) == 1, (
        "черновик потерял вложение — ровно этот дефект есть в create_draft коннектора, "
        "и ради него написан свой путь")
    assert item.attachments[0].name == os.path.basename(pdf)
    assert item.attachments[0].content.startswith(b"%PDF")
    assert item.kwargs["to_recipients"] == ["boss@company.ru"]
    assert item.kwargs["subject"] == "Тема"
    assert item.folder is transport._account.drafts
    assert res["folder"] == "Черновики"


def check_ready_ok() -> None:
    transport = _FakeTransport()
    real_transport = EWS._transport
    EWS._transport = lambda env=None: transport
    try:
        with _with_env(**FULL_ENV):
            info = EWS.check_ready()
            assert info["account"] == "Artur.Bayrashev@tatar.ru", info
            assert "mail.tatar.ru" in info["endpoint"], info
    finally:
        EWS._transport = real_transport


def check_send_message(pdf) -> None:
    created = []
    transport = _FakeTransport()
    real_transport = EWS._transport
    EWS._transport = lambda env=None: transport
    sys.modules["exchangelib"] = _fake_exchangelib(created)
    try:
        with _with_env(**FULL_ENV):
            msg = {"to": "boss@company.ru", "subject": "Тема", "body": "Текст.",
                   "attachments": [pdf]}
            res = EWS.send_message(msg, draft=True)
            assert res["action"] == "draft" and res["attachments"] == 1
            assert res["from"] == "Artur.Bayrashev@tatar.ru"
            assert transport.sent == [], "в режиме черновика письмо уходить не должно"

            res = EWS.send_message(msg, draft=False)
            assert res["action"] == "sent"
            assert len(transport.sent) == 1, "письмо должно уйти ровно один раз"
            raw = transport.sent[0][0]
            # вложение в MIME лежит в base64, поэтому проверяем разбором, а не поиском байт
            import email
            from email import policy
            parsed = email.message_from_bytes(raw, policy=policy.default)
            assert parsed["To"] == "boss@company.ru"
            files = list(parsed.iter_attachments())
            assert len(files) == 1 and files[0].get_payload(decode=True).startswith(b"%PDF")
    finally:
        EWS._transport = real_transport
        sys.modules.pop("exchangelib", None)

    # дефолт функции — черновик; если однажды поменяют, тест обязан упасть
    transport2 = _FakeTransport()
    EWS._transport = lambda env=None: transport2
    sys.modules["exchangelib"] = _fake_exchangelib([])
    try:
        with _with_env(**FULL_ENV):
            assert EWS.send_message({"to": "a@b.ru", "subject": "Т",
                                     "body": "Т"})["action"] == "draft"
            assert transport2.sent == [], "по умолчанию письмо НЕ отправляется"
    finally:
        EWS._transport = real_transport
        sys.modules.pop("exchangelib", None)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        pdf = os.path.join(tmp, "onepager.pdf")
        with open(pdf, "wb") as fh:
            fh.write(b"%PDF-1.4 fake")
        check_env()
        check_build_mime(pdf)
        check_draft_keeps_attachment(pdf)
        check_ready_ok()
        check_send_message(pdf)
    print("test_mail_ews: OK — черновик сохраняет вложение, bcc не теряется молча, "
          "по умолчанию письмо не уходит")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
