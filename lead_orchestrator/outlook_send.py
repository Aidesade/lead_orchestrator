# -*- coding: utf-8 -*-
r"""Отправка письма через запущенный Outlook Desktop — стадии 5 и 7.

Почему не smtplib и не MCP-сервер:
  * своего SMTP-релея у пайплайна нет, а ящик ``@tatar.ru`` уже заведён в профиле
    Outlook и ходит через почтовый сервер ЦИТ РТ, где настроены PTR и SPF —
    отправляя через него, мы наследуем его репутацию, а не строим свою с нуля;
  * MCP-инструмент ``outlook.send_email`` вложений НЕ поддерживает (в нём только
    To/Subject/Body/HTML), а one-pager нужно приложить файлом. Механизм выбора
    аккаунта (``SendUsingAccount``) взят оттуда же — он рабочий.

Модуль синхронный. COM требует инициализации на КАЖДОМ потоке, который его трогает
(``CoInitialize``), поэтому дескриптор кэшируется в thread-local: вызов из
``asyncio.to_thread`` не должен ронять процесс на второй компании.

⚠️ По умолчанию письмо СОХРАНЯЕТСЯ В ЧЕРНОВИКИ, а не отправляется. Реальная отправка —
только явным ``draft=False``. Это не перестраховка: отправка необратима, а ошибка в
шаблоне на сотне компаний стоит домена.

Отладка:
  py outlook_send.py --check                       # прекондишен: Outlook, аккаунт
  py outlook_send.py --to a@b.ru --subject Тест --body Привет   # черновик
"""
from __future__ import annotations

import argparse
import os
import sys
import threading

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Ящик, от имени которого идёт рассылка. Подстрока: в профиле аккаунт называется
# полным адресом (Artur.Bayrashev@tatar.ru), а домен неизменен.
DEFAULT_FROM = (os.environ.get("OUTREACH_FROM") or "tatar.ru").strip()

OL_MAIL_ITEM = 0
# dispid свойства MailItem.SendUsingAccount: через обычный setattr pywin32 его не
# отдаёт (свойство только для записи через Invoke) — приём из outlook-desktop-mcp
_DISPID_SEND_USING_ACCOUNT = 64209

_local = threading.local()


class OutlookError(RuntimeError):
    """Outlook не запущен, нет нужного аккаунта или COM отказал."""


# Общее имя ошибки транспорта: outreach.py выбирает между этим модулем и mail_ews
# (Exchange по EWS) и не должен знать, у какого из них как называется исключение.
TransportError = OutlookError


# ------------------------------------------------------------- чистая логика ----
def account_address(acc):
    """Адрес аккаунта Outlook. У Exchange-аккаунтов SmtpAddress бывает пустым —
    тогда годится DisplayName, он у них равен адресу."""
    for attr in ("SmtpAddress", "DisplayName"):
        try:
            value = (getattr(acc, attr, "") or "").strip()
        except Exception:                          # noqa: BLE001 — COM бросает на пустых полях
            value = ""
        if value:
            return value
    return ""


def pick_account(accounts, needle=None):
    """Найти аккаунт по подстроке адреса. Возвращает (аккаунт, адрес) или (None, "").

    Точное совпадение адреса приоритетнее подстроки: если в профиле есть и
    ``mail@tatar.ru``, и ``mail@tatar.ru.example.com``, подстрока найдёт оба."""
    needle = (needle or DEFAULT_FROM).strip().lower()
    by_substring = (None, "")
    for acc in accounts or []:
        addr = account_address(acc)
        if not addr:
            continue
        low = addr.lower()
        if low == needle:
            return acc, addr
        if needle in low and by_substring[0] is None:
            by_substring = (acc, addr)
    return by_substring


def validate(msg):
    """Проверить письмо ДО того, как трогать COM. Возвращает нормализованный dict."""
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
    return {
        "to": to,
        "subject": subject,
        "body": body,
        "html": (msg.get("html") or "").strip(),
        "cc": (msg.get("cc") or "").strip(),
        "bcc": (msg.get("bcc") or "").strip(),
        "attachments": attachments,
    }


def compose(mail, msg, account=None):
    """Заполнить готовый MailItem. Вынесено отдельно, чтобы тест мог подсунуть
    фальшивый объект и проверить порядок полей без запущенного Outlook."""
    if account is not None:
        mail._oleobj_.Invoke(*(_DISPID_SEND_USING_ACCOUNT, 0, 8, 0, account))
    mail.To = msg["to"]
    mail.Subject = msg["subject"]
    mail.Body = msg["body"]
    if msg.get("cc"):
        mail.CC = msg["cc"]
    if msg.get("bcc"):
        mail.BCC = msg["bcc"]
    if msg.get("html"):
        mail.HTMLBody = msg["html"]
    for path in msg.get("attachments") or []:
        mail.Attachments.Add(path)
    return mail


def deliver(mail, draft=True):
    """Черновик или отправка. Единственное место, где письмо уходит наружу."""
    if draft:
        mail.Save()
        return "draft"
    mail.Send()
    return "sent"


# ------------------------------------------------------------------- COM ----
def _outlook():
    """Дескриптор Outlook для ТЕКУЩЕГО потока (COM инициализируется по потокам)."""
    handle = getattr(_local, "outlook", None)
    if handle is not None:
        return handle
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise OutlookError(
            "нет pywin32 — поставь: py -m pip install pywin32 (пин в requirements.txt)"
        ) from exc
    try:
        pythoncom.CoInitialize()
        handle = win32com.client.Dispatch("Outlook.Application")
        handle.GetNamespace("MAPI").CurrentUser.Name        # ранняя проверка живого профиля
    except Exception as exc:                       # noqa: BLE001 — COM отдаёт свои типы ошибок
        raise OutlookError(
            f"Outlook Desktop (Classic) не отвечает: {str(exc)[:120]}. "
            f"Запусти Outlook и повтори; «новый» Outlook (olk.exe) не поддерживается"
        ) from exc
    _local.outlook = handle
    return handle


def accounts():
    """Список аккаунтов профиля."""
    return list(_outlook().Session.Accounts)


def _require_account(needle=None):
    """Аккаунт отправителя или ЖЁСТКАЯ ошибка со списком того, что есть в профиле.

    Общий вход и для прекондишена, и для отправки: перепутанный ящик обнаруживается
    одинаково — до первой компании и перед каждым письмом.
    Возвращает (аккаунт, адрес, все аккаунты профиля)."""
    needle = needle or DEFAULT_FROM
    every = accounts()
    acc, addr = pick_account(every, needle)
    if not acc:
        known = ", ".join(account_address(a) for a in every) or "ни одного"
        raise OutlookError(
            f"в профиле Outlook нет аккаунта с «{needle}». Есть: {known}. "
            f"Добавь ящик или задай другой в OUTREACH_FROM")
    return acc, addr, every


def check_ready(needle=None):
    """Прекондишен стадии 5. Возвращает dict; исключение — если ящика нет.

    Падаем ЖЁСТКО и до первой компании: пайплайн, который отработал сбор и ресёрч,
    а потом не смог отправить, тратит деньги впустую."""
    _acc, addr, every = _require_account(needle)
    return {"account": addr, "accounts": [account_address(a) for a in every]}


def send_message(msg, draft=True, account=None):
    """Создать письмо и положить в черновики (draft=True) либо отправить.

    Возвращает dict с фактическим результатом — вызывающий обязан записать его
    в реестр, иначе повторный прогон напишет тому же человеку второй раз."""
    clean = validate(msg)
    acc, addr, _every = _require_account(account)
    mail = _outlook().CreateItem(OL_MAIL_ITEM)
    compose(mail, clean, acc)
    action = deliver(mail, draft=draft)
    return {
        "action": action,
        "from": addr,
        "to": clean["to"],
        "subject": clean["subject"],
        "attachments": len(clean["attachments"]),
    }


def main():
    ap = argparse.ArgumentParser(description="отправка письма через Outlook Desktop")
    ap.add_argument("--check", action="store_true", help="прекондишен: Outlook и ящик отправителя")
    ap.add_argument("--to", default="")
    ap.add_argument("--subject", default="")
    ap.add_argument("--body", default="")
    ap.add_argument("--attach", action="append", default=[])
    ap.add_argument("--from-account", dest="from_account", default=None)
    ap.add_argument("--send", action="store_true",
                    help="ОТПРАВИТЬ по-настоящему (по умолчанию — черновик)")
    a = ap.parse_args()

    try:
        if a.check or not a.to:
            info = check_ready(a.from_account)
            print(f"[ok] Outlook отвечает, отправитель: {info['account']}")
            print(f"     аккаунты профиля: {', '.join(info['accounts'])}")
            return 0
        res = send_message(
            {"to": a.to, "subject": a.subject, "body": a.body, "attachments": a.attach},
            draft=not a.send, account=a.from_account)
        print(f"[{res['action']}] {res['from']} -> {res['to']}: {res['subject']} "
              f"(вложений {res['attachments']})")
        return 0
    except (OutlookError, ValueError) as exc:
        print(f"[ошибка] {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
