# -*- coding: utf-8 -*-
r"""Отправка письма через Exchange ЦИТ РТ по EWS — второй транспорт стадии 7.

Зачем рядом с ``outlook_send``: тот работает только на Windows с ЗАПУЩЕННЫМ Outlook
Desktop, то есть рассылку нельзя ни увести на сервер, ни положить в Docker. Этот
транспорт ходит на ``https://mail.tatar.ru/EWS/Exchange.asmx`` по HTTP и работает
headless — за счёт коннектора ЦИТ РТ ``mcp-mail`` (gitlab.dtc.tatar/starship/mcp-mail).

Берём из mcp-mail РОВНО ОДИН класс — ``EwsTransport``. Он самодостаточен: тянет
только ``exchangelib``, ни их сервисный слой, ни BaseAgent, ни брокер учёток не
подключаются (цепочка импортов проверена: ews.py -> exchangelib + transport.py -> abc).
Их верхний уровень (``operations.send_email_impl``) сознательно не используем: это
двухфазный plan/confirm с хранилищем планов и аудитом, рассчитанный на MCP-сервис.

⚠️ **Их ``create_draft`` теряет вложения.** В ``transports/ews.py`` метод ``send``
перекладывает вложения из MIME в письмо, а ``create_draft`` — нет (в нём нет цикла
``iter_attachments``; это зафиксировано их же тестом ``tests/test_ews_drafts.py``).
Для нашего пайплайна это неприемлемо: черновик существует ровно затем, чтобы человек
глазами проверил ТО ЖЕ письмо, которое уйдёт, — а тихо пропавший one-pager делает
проверку обманом. Поэтому черновик собираем сами (``_save_draft``), повторяя логику
их ``send``, но вызывая ``save()`` вместо ``send_and_save()``.

Ещё два осознанных отличия от их транспорта, оба защитные:
  * ``Bcc`` они молча игнорируют (читают только ``To``/``Cc``) — мы на скрытой копии
    падаем с ошибкой, чтобы адресат не «потерялся» незаметно;
  * ``From`` из MIME Exchange игнорирует, отправитель всегда сам ящик — это как раз
    то, что нужно, но об этом должно быть написано, а не подразумеваться.

Настройка (пароль только через окружение, в аргументы и логи не попадает):
  set OUTREACH_TRANSPORT=ews
  set OUTREACH_EWS_ADDRESS=Artur.Bayrashev@tatar.ru
  set OUTREACH_EWS_USER=TATAR\bayrashev        (или UPN — как входите в Outlook)
  set OUTREACH_EWS_PASSWORD=...
  set OUTREACH_EWS_ENDPOINT=...                (по умолчанию mail.tatar.ru)

Отладка:
  py mail_ews.py --check
"""
from __future__ import annotations

import argparse
import mimetypes
import os
import sys
from email.message import EmailMessage
from email.utils import make_msgid

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from outlook_send import validate                   # noqa: E402 — проверка письма общая для транспортов

# Пресет ЦИТ РТ (их app/providers.py). Autodiscover у них выключен намеренно,
# поэтому endpoint задаётся явно и здесь.
DEFAULT_ENDPOINT = "https://mail.tatar.ru/EWS/Exchange.asmx"
DEFAULT_MCP_MAIL = os.path.join(os.path.dirname(SCRIPTS), "mcp_mail", "mcp-mail-main")


class TransportError(RuntimeError):
    """Нет настроек, нет коннектора или Exchange не пустил."""


def _connector_src():
    """Путь к пакету mcp-mail. Он живёт в ``integrations/src`` (см. их pyproject)."""
    root = (os.environ.get("MCP_MAIL_DIR") or DEFAULT_MCP_MAIL).strip()
    src = os.path.join(root, "integrations", "src")
    if not os.path.isdir(src):
        raise TransportError(
            f"не найден коннектор mcp-mail: {src}. Распакуй архив из "
            f"gitlab.dtc.tatar/starship/mcp-mail в {root} либо задай MCP_MAIL_DIR")
    return src


def transport_env():
    """env-словарь в том виде, в каком его ждёт EwsTransport.

    Пароль читается прямо из окружения и НИКУДА больше не кладётся: ни в аргументы,
    ни в лог, ни в реестр."""
    address = (os.environ.get("OUTREACH_EWS_ADDRESS") or "").strip()
    user = (os.environ.get("OUTREACH_EWS_USER") or "").strip()
    password = os.environ.get("OUTREACH_EWS_PASSWORD") or ""
    missing = [name for name, value in (
        ("OUTREACH_EWS_ADDRESS", address),
        ("OUTREACH_EWS_USER", user),
        ("OUTREACH_EWS_PASSWORD", password)) if not value]
    if missing:
        raise TransportError(
            f"не заданы переменные {', '.join(missing)} — по EWS ходить нечем. "
            f"Либо задай их, либо оставь OUTREACH_TRANSPORT=outlook")
    return {
        "EMAIL_ADDRESS": address,
        "EMAIL_USER": user,
        "EMAIL_PASSWORD": password,
        "EMAIL_EWS_ENDPOINT": (os.environ.get("OUTREACH_EWS_ENDPOINT")
                               or DEFAULT_ENDPOINT).strip(),
    }


def _transport(env=None):
    src = _connector_src()
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        from integrations.connectors.email.transports.ews import EwsTransport
    except ImportError as exc:
        raise TransportError(
            f"коннектор mcp-mail не импортируется ({str(exc)[:90]}); "
            f"нужен exchangelib: py -m pip install exchangelib==5.5.1") from exc
    try:
        return EwsTransport(env or transport_env())
    except TransportError:
        raise
    except Exception as exc:                        # noqa: BLE001 — exchangelib отдаёт свои типы
        raise TransportError(f"Exchange не принял подключение: {str(exc)[:160]}") from exc


# ------------------------------------------------------------------- MIME ----
def build_mime(msg, sender=""):
    """Наше письмо -> байты RFC-5322. Транспорт ЦИТ РТ принимает именно их.

    Тип вложения Exchange восстанавливает по имени файла: их код создаёт
    ``FileAttachment`` без ``content_type``. Для ``.pdf`` этого достаточно, но
    расширение в имени обязано быть настоящим."""
    if msg.get("bcc"):
        raise TransportError(
            "EWS-транспорт ЦИТ РТ не поддерживает скрытую копию: их send читает "
            "только To и Cc, и Bcc пропал бы молча. Убери bcc или шли через Outlook")
    mime = EmailMessage()
    if sender:
        # Exchange всё равно подставит адрес самого ящика — заголовок нужен, чтобы
        # сохранённый на диск .eml выглядел корректно при разборе глазами
        mime["From"] = sender
    mime["To"] = msg["to"]
    if msg.get("cc"):
        mime["Cc"] = msg["cc"]
    mime["Subject"] = msg["subject"]
    mime["Message-ID"] = make_msgid()
    mime.set_content(msg["body"])
    for path in msg.get("attachments") or []:
        guessed = mimetypes.guess_type(path)[0] or "application/octet-stream"
        maintype, _slash, subtype = guessed.partition("/")
        with open(path, "rb") as fh:
            mime.add_attachment(fh.read(), maintype=maintype, subtype=subtype,
                                filename=os.path.basename(path))
    return mime.as_bytes(), mime["Message-ID"]


def _save_draft(transport, raw_mime):
    """Черновик С ВЛОЖЕНИЯМИ — то, чего не умеет их ``create_draft``.

    Повторяет разбор MIME из их ``send`` (``transports/ews.py``), но заканчивается
    ``save()`` вместо ``send_and_save()``. Лезем к ``transport._account``, потому что
    публичного доступа к аккаунту у их транспорта нет, а поднимать вторую сессию к
    Exchange ради черновика — хуже."""
    import email
    from email import policy
    from exchangelib import FileAttachment, HTMLBody, Message

    account = getattr(transport, "_account", None)
    if account is None:
        raise TransportError(
            "у EwsTransport больше нет ._account — коннектор mcp-mail изменился, "
            "черновик с вложением собрать нечем (см. комментарий в mail_ews._save_draft)")

    source = email.message_from_bytes(raw_mime, policy=policy.default)
    plain = source.get_body(preferencelist=("plain",))
    html = source.get_body(preferencelist=("html",))
    body = plain.get_content() if plain else ""
    if not body and html:
        body = HTMLBody(html.get_content())
    message = Message(
        account=account,
        subject=source["Subject"] or "",
        body=body,
        to_recipients=[a for _n, a in _addresses(source, "To")],
        cc_recipients=[a for _n, a in _addresses(source, "Cc")] or None,
    )
    for part in source.iter_attachments():
        message.attach(FileAttachment(
            name=part.get_filename() or "attachment",
            content=part.get_payload(decode=True) or b""))
    message.folder = account.drafts
    message.save()
    return {"draft_id": str(getattr(message, "id", "") or ""),
            "folder": str(getattr(account.drafts, "name", "Drafts"))}


def _addresses(source, header):
    from email.utils import getaddresses
    return getaddresses(source.get_all(header, []))


# -------------------------------------------------------------- интерфейс ----
def check_ready(needle=None):
    """Прекондишен стадии 5 для EWS. Контракт тот же, что у outlook_send.check_ready."""
    del needle                                      # у EWS ящик один — тот, что в настройках
    env = transport_env()
    transport = _transport(env)
    probe = {}
    try:
        probe = transport.probe() or {}
    except Exception as exc:                        # noqa: BLE001 — сеть/учётка
        raise TransportError(f"Exchange не отвечает: {str(exc)[:160]}") from exc
    state = (probe.get("ews") or {})
    if state and not state.get("ok", True):
        raise TransportError(f"Exchange отклонил проверку: {str(state)[:160]}")
    return {"account": env["EMAIL_ADDRESS"], "accounts": [env["EMAIL_ADDRESS"]],
            "endpoint": env["EMAIL_EWS_ENDPOINT"]}


def send_message(msg, draft=True, account=None):
    """Черновик (по умолчанию) либо отправка. Контракт совпадает с outlook_send."""
    del account                                     # ящик задаётся настройками EWS, не на вызове
    clean = validate(msg)
    env = transport_env()
    transport = _transport(env)
    raw_mime, message_id = build_mime(clean, sender=env["EMAIL_ADDRESS"])
    try:
        if draft:
            _save_draft(transport, raw_mime)
            action = "draft"
        else:
            transport.send(raw_mime, message_id)
            action = "sent"
    except TransportError:
        raise
    except Exception as exc:                        # noqa: BLE001 — exchangelib отдаёт свои типы
        raise TransportError(f"Exchange отказал: {str(exc)[:160]}") from exc
    return {
        "action": action,
        "from": env["EMAIL_ADDRESS"],
        "to": clean["to"],
        "subject": clean["subject"],
        "attachments": len(clean["attachments"]),
    }


def main():
    ap = argparse.ArgumentParser(description="отправка через Exchange ЦИТ РТ (EWS)")
    ap.add_argument("--check", action="store_true", help="прекондишен: настройки и связь")
    ap.add_argument("--to", default="")
    ap.add_argument("--subject", default="")
    ap.add_argument("--body", default="")
    ap.add_argument("--attach", action="append", default=[])
    ap.add_argument("--send", action="store_true",
                    help="ОТПРАВИТЬ по-настоящему (по умолчанию — черновик)")
    a = ap.parse_args()
    try:
        if a.check or not a.to:
            info = check_ready()
            print(f"[ok] Exchange отвечает: {info['account']} через {info['endpoint']}")
            return 0
        res = send_message({"to": a.to, "subject": a.subject, "body": a.body,
                            "attachments": a.attach}, draft=not a.send)
        print(f"[{res['action']}] {res['from']} -> {res['to']}: {res['subject']} "
              f"(вложений {res['attachments']})")
        return 0
    except (TransportError, ValueError) as exc:
        print(f"[ошибка] {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
