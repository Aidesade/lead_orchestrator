# -*- coding: utf-8 -*-
r"""
Транспорт писем через SMTP Яндекса — второе (и единственное кроме Outlook) место,
откуда письмо уходит наружу.

Зачем понадобился, хотя есть `outlook_send`. Тот работает только с ящиком,
заведённым в профиле Outlook Desktop (COM + `SendUsingAccount`). Рассылка же
ведётся с `AI-CIT.RT@yandex.com`, которого в профиле нет и который добавлять туда
не требуется: у Яндекса есть штатный SMTP с паролем приложения.

Контракт СОВПАДАЕТ с `outlook_send` намеренно — `validate` / `send_message` /
`check_ready` с теми же аргументами и той же формой результата, чтобы outreach мог
переключать транспорт одной переменной, а не ветвиться в каждой стадии.

ОТЛИЧИЕ, которое надо знать: у SMTP нет папки «Черновики». `draft=True` здесь
означает «письмо собрано и проверено, но не отправлено» — оно кладётся файлом
`.eml` в `ORQ_DRAFTS_DIR`. Так дефолт остаётся безопасным (как в Outlook-ветке,
где по умолчанию `Save()`, а не `Send()`), и черновик можно открыть глазами.

Почему пауза не опция. Яндекс режет массовую отправку с одного ящика: без темпа
рассылка обрывается на середине с 5.7.1, а домен получает отметку. Темп задаёт
вызывающий (`outreach --pace`), здесь же стоит минимальная защита от залпа.

CLI:
  py yandex_send.py --check                       # логин, без отправки
  py yandex_send.py --to a@b.ru --subject Тема --body Текст          # черновик .eml
  py yandex_send.py --to a@b.ru --subject Тема --body Текст --send   # отправка
"""
from __future__ import annotations

import argparse
import os
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from project_env import load_project_env
    load_project_env()
except Exception:                                  # noqa: BLE001 — без .env тоже работаем
    pass

SMTP_HOST = os.environ.get("YANDEX_SMTP_HOST") or "smtp.yandex.ru"
SMTP_PORT = int(os.environ.get("YANDEX_SMTP_PORT") or 465)
SMTP_USER = os.environ.get("YANDEX_SMTP_USER") or ""
SMTP_PASSWORD = os.environ.get("YANDEX_SMTP_PASSWORD") or ""
FROM_NAME = os.environ.get("YANDEX_SMTP_FROM_NAME") or "АО «ЦИТ РТ»"
DRAFTS_DIR = os.environ.get("ORQ_DRAFTS_DIR") or r"D:\orq_outreach\drafts"
# Минимальная пауза между письмами одной сессии: защита от залпа, если вызывающий
# забыл про --pace. Реальный темп задаётся снаружи и обычно больше.
MIN_PAUSE = float(os.environ.get("YANDEX_SMTP_MIN_PAUSE") or 3)

_last_sent = 0.0


class YandexSendError(RuntimeError):
    """Транспорт недоступен или отверг письмо."""


def _safe(text):
    """Убрать пароль из текста ошибки: SMTP-ответы иногда цитируют логин целиком."""
    out = str(text)
    if SMTP_PASSWORD:
        out = out.replace(SMTP_PASSWORD, "<скрыто>")
    return out


def validate(msg):
    """Те же проверки, что в outlook_send: письмо должно быть пригодным ДО транспорта."""
    to = (msg.get("to") or "").strip()
    if not to:
        raise ValueError("получатель не задан — письмо без адресата не создаём")
    subject = (msg.get("subject") or "").strip()
    if not subject:
        raise ValueError("пустая тема — такое письмо уйдёт в спам")
    body = (msg.get("body") or "").strip()
    if not body:
        raise ValueError("пустое тело письма")
    attachments = []
    for path in msg.get("attachments") or []:
        full = os.path.abspath(str(path))
        if not os.path.isfile(full):
            raise ValueError(f"вложение не найдено: {full}")
        attachments.append(full)
    return {"to": to, "subject": subject, "body": body,
            "cc": (msg.get("cc") or "").strip(),
            "bcc": (msg.get("bcc") or "").strip(),
            "attachments": attachments}


def compose(msg, sender=None):
    """dict -> EmailMessage. Отдельно от отправки, чтобы тест собирал письмо офлайн."""
    sender = sender or SMTP_USER
    mail = EmailMessage()
    mail["From"] = formataddr((FROM_NAME, sender)) if FROM_NAME else sender
    mail["To"] = msg["to"]
    if msg.get("cc"):
        mail["Cc"] = msg["cc"]
    mail["Subject"] = msg["subject"]
    mail["Date"] = formatdate(localtime=True)
    mail["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    mail.set_content(msg["body"])
    for path in msg.get("attachments") or []:
        with open(path, "rb") as handle:
            data = handle.read()
        mail.add_attachment(data, maintype="application", subtype="pdf",
                            filename=os.path.basename(path))
    return mail


def _connect():
    if not (SMTP_USER and SMTP_PASSWORD):
        raise YandexSendError(
            "не заданы YANDEX_SMTP_USER / YANDEX_SMTP_PASSWORD. Пароль — это пароль "
            "ПРИЛОЖЕНИЯ из ID.Yandex, обычный пароль аккаунта SMTP не примет")
    try:
        session = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT,
                                   context=ssl.create_default_context(), timeout=30)
        session.login(SMTP_USER, SMTP_PASSWORD)
        return session
    except smtplib.SMTPAuthenticationError as exc:
        raise YandexSendError(
            f"SMTP отверг логин: {_safe(exc)}. Проверь, что пароль приложения выпущен "
            f"для «Почты» и что у ящика включён доступ по протоколам") from exc
    except Exception as exc:                       # noqa: BLE001 — сеть, TLS, таймаут
        raise YandexSendError(f"SMTP недоступен: {_safe(exc)[:160]}") from exc


def check_ready(needle=None):
    """Готов ли транспорт: логин без отправки. Форма ответа — как у outlook_send."""
    del needle                                     # совместимость сигнатуры
    session = _connect()
    try:
        return {"ok": True, "from": SMTP_USER, "transport": f"{SMTP_HOST}:{SMTP_PORT}"}
    finally:
        try:
            session.quit()
        except Exception:                          # noqa: BLE001
            pass


def save_draft(mail, to):
    """Черновик = файл .eml. У SMTP нет папки «Черновики», а терять текст нельзя."""
    os.makedirs(DRAFTS_DIR, exist_ok=True)
    safe_to = "".join(ch if ch.isalnum() or ch in "._-@" else "_" for ch in to)
    path = os.path.join(DRAFTS_DIR, f"{int(time.time() * 1000)}_{safe_to}.eml")
    with open(path, "wb") as handle:
        handle.write(bytes(mail))
    return path


def send_message(msg, draft=True, account=None, session=None):
    """Собрать письмо и положить в черновики (draft=True) либо отправить.

    draft=True — дефолт, как и в outlook_send: массовая рассылка необратима, и
    случайный запуск не должен её начинать.

    session позволяет переиспользовать одно SMTP-соединение на всю рассылку:
    логин на каждое письмо Яндекс считает подозрительным поведением."""
    clean = validate(msg)
    mail = compose(clean, sender=account or SMTP_USER)

    if draft:
        return {"action": "draft", "from": account or SMTP_USER, "to": clean["to"],
                "subject": clean["subject"], "attachments": len(clean["attachments"]),
                "path": save_draft(mail, clean["to"])}

    global _last_sent
    gap = time.time() - _last_sent
    if _last_sent and gap < MIN_PAUSE:
        time.sleep(MIN_PAUSE - gap)

    own = session is None
    session = session or _connect()
    try:
        recipients = [clean["to"]]
        for extra in (clean.get("cc"), clean.get("bcc")):
            if extra:
                recipients += [part.strip() for part in extra.split(",") if part.strip()]
        session.send_message(mail, from_addr=account or SMTP_USER, to_addrs=recipients)
    except smtplib.SMTPRecipientsRefused as exc:
        raise YandexSendError(f"адресат отвергнут: {_safe(exc)[:160]}") from exc
    except Exception as exc:                       # noqa: BLE001
        raise YandexSendError(f"письмо не ушло: {_safe(exc)[:160]}") from exc
    finally:
        _last_sent = time.time()
        if own:
            try:
                session.quit()
            except Exception:                      # noqa: BLE001
                pass

    return {"action": "sent", "from": account or SMTP_USER, "to": clean["to"],
            "subject": clean["subject"], "attachments": len(clean["attachments"])}


def main():
    parser = argparse.ArgumentParser(description="отправка письма через SMTP Яндекса")
    parser.add_argument("--check", action="store_true", help="проверить логин и выйти")
    parser.add_argument("--to", default="")
    parser.add_argument("--subject", default="")
    parser.add_argument("--body", default="")
    parser.add_argument("--attach", action="append", default=[])
    parser.add_argument("--send", action="store_true",
                        help="ОТПРАВИТЬ (без флага письмо ложится в черновики .eml)")
    args = parser.parse_args()

    if args.check:
        info = check_ready()
        print(f"[ok] SMTP {info['transport']}, отправитель: {info['from']}")
        return 0

    if not args.to:
        parser.error("нужен --to (или --check)")
    result = send_message({"to": args.to, "subject": args.subject, "body": args.body,
                           "attachments": args.attach}, draft=not args.send)
    print(f"[{result['action']}] {result['to']} | вложений: {result['attachments']}"
          + (f" | {result.get('path')}" if result.get("path") else ""))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except YandexSendError as error:
        raise SystemExit(f"Яндекс SMTP: {error}")
