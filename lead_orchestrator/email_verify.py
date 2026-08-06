# -*- coding: utf-8 -*-
r"""
email_verify — проверка существования почтового ящика БЕЗ отправки письма.

Python-порт ядра Reacher (github.com/reacherhq/check-if-email-exists) на голом
stdlib: ни Docker, ни WSL, ни Rust-бинаря, ни внешних сервисов. Формат ответа
совместим с Reacher (`is_reachable` + `syntax`/`mx`/`smtp`/`misc`), поэтому код,
умеющий разбирать его JSON, работает и с этим модулем — и наоборот.

Что делает (порядок как у оригинала):
  1. синтаксис адреса;
  2. MX домена (DNS-over-HTTPS, фолбэк на A/AAAA — implicit MX, RFC 5321);
  3. SMTP-диалог до RCPT TO, БЕЗ DATA: письмо не отправляется никогда;
  4. catch-all (два случайных адреса), переполненный ящик, отключённый ящик;
  5. misc: одноразовый домен, ролевой ящик, публичный сервис.

ОТЛИЧИЯ ОТ ОРИГИНАЛА (сознательные, не «недоделки»):
  * Reacher возвращает `invalid` и когда ящика нет, и когда просто не смог
    подключиться к MX. При закрытом исходящем порте 25 это пометило бы
    несуществующими ВСЕ адреса разом. Здесь «не удалось проверить» — это
    `unknown`, а `invalid` ставится только когда сервер ответил на RCPT отказом
    по адресату (5.1.1), а не по политике (5.7.x).
  * Отказ по политике («ваш IP в чёрном списке») отделён от «нет такого ящика»:
    про адресата он не говорит ничего.
  * Одна SMTP-сессия на домен на НЕСКОЛЬКО адресов (у Reacher — сессия на адрес):
    так вежливее к чужому серверу и быстрее.
  * Вес вердикта зависит от приёмника домена: Яндекс 360 отвечает честно, mail.ru
    «принимает» что угодно от незнакомых пробников, Exchange Online принимает всё
    и проверяет получателя уже после DATA (см. email_guess.smtp_trust).

Представляться чужим доменом нельзя: приёмник смотрит SPF/PTR, и отказ по политике
неотличим от «ящика нет». HELO/MAIL FROM берутся из EMAIL_GUESS_HELO /
EMAIL_GUESS_MAIL_FROM; без них SMTP-стадия не выполняется.

CLI:
  py email_verify.py ivanov@company.ru
  py email_verify.py ivanov@company.ru petrov@company.ru --json
  py email_verify.py --serve 8080      # HTTP-API, совместимый с Reacher
"""
import argparse
import json
import os
import random
import re
import socket
import sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import email_finder as EF
import email_guess as EG

# Ролевые ящики: письмо уйдёт не человеку, а в отдел. Ящик при этом обычно
# существует — поэтому это пометка, а не приговор.
ROLE_PREFIXES = EF.GENERIC_KW | EF.COOP_KW | EF.TITLE_KW | EG.RU_ROLE_WORDS

# Одноразовые ящики. Полный список (5000+ доменов) живёт в отдельных проектах и
# ежедневно обновляется; здесь — только самые ходовые, чтобы не тянуть зависимость
# ради случая, который в B2B-лидгене почти не встречается.
DISPOSABLE_DOMAINS = {
    "mailinator.com", "10minutemail.com", "guerrillamail.com", "temp-mail.org",
    "tempmail.com", "throwawaymail.com", "yopmail.com", "getnada.com",
    "trashmail.com", "sharklasers.com", "maildrop.cc", "dispostable.com",
    "fakeinbox.com", "mytemp.email", "temp-mail.ru", "vremenaya-pochta.ru",
    "1secmail.com", "moakt.com", "mohmal.com", "emailondeck.com",
}

_SYNTAX_RE = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~\-]+"
                        r"(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~\-]+)*@[a-z0-9\-.]+$", re.I)

# Ответы сервера, по которым видно СОСТОЯНИЕ ящика, а не факт его существования.
_FULL_INBOX = re.compile(r"full|quota|4\.2\.2|over.?quota|mailbox.?is.?full", re.I)
_DISABLED = re.compile(r"disabled|inactive|suspended|5\.2\.1|no longer|deactivat", re.I)


# ------------------------------------------------------------------ стадии ----
def check_syntax(email):
    """Разбор адреса. Reacher-совместимо: {username, domain, is_valid_syntax}."""
    email = (email or "").strip()
    local, _, domain = email.rpartition("@")
    ok = bool(local and domain and _SYNTAX_RE.match(email) and len(email) <= 254
              and len(local) <= 64 and "." in domain and ".." not in email)
    return {"address": email, "username": local, "domain": domain.lower(),
            "is_valid_syntax": ok}


def check_mx(domain):
    """MX домена. Reacher-совместимо: {accepts_mail, records}."""
    state = EG.mail_domain_state(domain)
    return {"accepts_mail": bool(state.get("accepts_mail")),
            "records": state.get("mx") or [],
            "provider": state.get("provider") or EG.mail_provider(state.get("mx")),
            "via": state.get("via", ""), "note": state.get("note", ""),
            "_state": state.get("accepts_mail")}    # True/False/None — None это «не проверено»


def check_misc(email):
    """Свойства самого адреса, без обращения к серверу."""
    local, _, domain = (email or "").rpartition("@")
    domain = domain.lower()
    norm = re.sub(r"[._\-+]", "", local.lower())
    tokens = [t for t in re.split(r"[._\-+]", local.lower()) if t]
    return {"is_disposable": domain in DISPOSABLE_DOMAINS,
            "is_role_account": norm in ROLE_PREFIXES or any(t in ROLE_PREFIXES for t in tokens),
            "is_b2c": domain in EF.FREE_PROVIDERS}


def _blank_smtp(reason=""):
    return {"can_connect_smtp": False, "is_deliverable": False, "is_catch_all": False,
            "has_full_inbox": False, "is_disabled": False, "policy_blocked": False,
            "checked": False, "reason": reason, "code": 0, "message": ""}


def _greet(session, helo):
    """Поздороваться так, как ждёт современный сервер.

    Голый HELO без TLS многие антиспам-фильтры закрывают на месте («Connection
    unexpectedly closed»), из-за чего проверяемый домен выглядел недоступным.
    Порядок: EHLO -> STARTTLS (если предложен) -> повторный EHLO; если сервер
    старый и EHLO не понял — откат на HELO."""
    code, _ = session.ehlo(helo)
    if code >= 400:
        session.helo(helo)
        return
    try:
        if session.has_extn("starttls"):
            session.starttls()
            session.ehlo(helo)
    except Exception:                              # noqa: BLE001 — TLS не обязателен для RCPT
        pass


def check_smtp_many(domain, addresses, mx_hosts=None, timeout=10, helo=None,
                    mail_from=None, port=25):
    """SMTP-стадия сразу для нескольких адресов ОДНОГО домена — одна сессия.

    Возвращает {адрес: smtp-словарь}. DATA не отправляется, письма не уходят."""
    addresses = list(addresses or [])[:5]
    out = {a: _blank_smtp("проверка не выполнялась") for a in addresses}
    if not (domain and addresses):
        return out
    helo = helo or os.environ.get("EMAIL_GUESS_HELO", "").strip()
    mail_from = mail_from or os.environ.get("EMAIL_GUESS_MAIL_FROM", "").strip()
    if not (helo and mail_from):
        for a in addresses:
            out[a] = _blank_smtp("не заданы EMAIL_GUESS_HELO / EMAIL_GUESS_MAIL_FROM")
        return out

    hosts = list(mx_hosts or [])
    if not hosts:
        state = EG.mail_domain_state(domain)
        hosts = state.get("mx") or ([domain] if state.get("accepts_mail") else [])
    if not hosts:
        for a in addresses:
            out[a] = _blank_smtp("у домена нет MX и нет A-записи")
        return out

    import smtplib
    session, last_error = None, ""
    for host in hosts[:2]:                         # запасной MX — но не ценой минуты ожидания
        try:
            # хост задаём В КОНСТРУКТОРЕ: connect() не заполняет _host, и тогда
            # starttls() падает с «server_hostname cannot be an empty string»,
            # а следом рвётся вся сессия — из-за этого домены выглядели недоступными
            session = smtplib.SMTP(host, port, timeout=timeout)
            _greet(session, helo)
            session.mail(mail_from)
            break
        except Exception as exc:                   # noqa: BLE001 — этот MX не пустил, пробуем следующий
            last_error = str(exc)[:80]
            try:
                if session is not None:
                    session.close()
            except Exception:
                pass
            session = None
    if session is None:
        for a in addresses:
            out[a] = _blank_smtp(f"не удалось проверить: {last_error or 'сервер не ответил'}")
        return out
    try:
        for a in addresses:
            out[a] = _blank_smtp("")
            out[a]["can_connect_smtp"] = True

        # catch-all: два случайных адреса, чтобы один случайно совпавший ящик или
        # частное правило не выдали домен за принимающий всё подряд
        accepted = 0
        for _ in range(2):
            noise = "".join(random.choice("0123456789abcdef") for _ in range(12))
            code, msg = session.rcpt(f"zz{noise}@{domain}")
            if EG._classify_rcpt(code, msg) == "ok":
                accepted += 1
        catch_all = accepted > 0
        for a in addresses:
            out[a]["is_catch_all"] = catch_all
            out[a]["checked"] = True
        if catch_all:
            for a in addresses:
                out[a]["reason"] = "домен принимает любой адрес (catch-all)"
            return out

        for a in addresses:
            code, msg = session.rcpt(a)
            text = msg.decode("utf-8", "replace") if isinstance(msg, bytes) else str(msg)
            kind = EG._classify_rcpt(code, msg)
            row = out[a]
            row.update(code=code, message=text[:200])
            # «превышена квота» (552/452) — это НЕ временный сбой связи: ящик
            # существует, просто сейчас не примет письмо
            if code in (452, 552) and _FULL_INBOX.search(text):
                row.update(is_deliverable=True, has_full_inbox=True,
                           reason="ящик существует, но переполнен")
                continue
            if kind == "ok":
                row["is_deliverable"] = True
            elif kind == "no":
                row["has_full_inbox"] = bool(_FULL_INBOX.search(text))
                row["is_disabled"] = bool(_DISABLED.search(text))
                row["reason"] = "сервер отказал по адресату"
            elif kind == "policy":
                row["policy_blocked"] = True
                row["reason"] = "отказ по политике сервера — об адресате не сказано"
            else:
                row["checked"] = False
                row["reason"] = "временный отказ (greylisting или лимит)"
                break                              # дальше не давим на чужой сервер
    except (OSError, socket.timeout, Exception) as exc:   # noqa: BLE001
        for a in addresses:
            if not out[a]["checked"]:
                out[a] = _blank_smtp(f"не удалось проверить: {str(exc)[:80]}")
    finally:
        if session is not None:
            try:
                session.quit()
            except Exception:
                pass
    return out


# ------------------------------------------------------------- сборка ответа ----
def _reachable(smtp, misc, mx):
    """Итог в терминах Reacher: safe | risky | invalid | unknown.

    Отличие от оригинала — там `invalid` возвращается ещё и при неудачном коннекте,
    из-за чего закрытый порт 25 «уничтожает» весь список адресов. Здесь такой случай
    честно называется unknown."""
    if mx.get("_state") is False:
        return "invalid", "домен не принимает почту"
    if not smtp.get("checked") or not smtp.get("can_connect_smtp"):
        return "unknown", smtp.get("reason") or "SMTP-проверка не выполнена"
    if smtp.get("policy_blocked"):
        return "unknown", "сервер отклонил по политике — про ящик ничего не известно"
    if smtp.get("is_deliverable"):
        if misc.get("is_disposable") or misc.get("is_role_account") or smtp.get("is_catch_all"):
            return "risky", "ящик принимает почту, но адрес ролевой/одноразовый или домен catch-all"
        if smtp.get("has_full_inbox"):
            return "risky", "ящик существует, но переполнен"
        return "safe", "ящик подтверждён почтовым сервером"
    if smtp.get("is_catch_all"):
        return "unknown", "домен принимает любой адрес — существование не проверить"
    if smtp.get("is_disabled"):
        return "invalid", "ящик отключён"
    return "invalid", smtp.get("reason") or "сервер сообщил, что такого ящика нет"


def check_email(email, timeout=10, **kw):
    """Один адрес -> Reacher-совместимый результат."""
    return check_emails([email], timeout=timeout, **kw)[0]


def check_emails(emails, timeout=10, helo=None, mail_from=None, port=25):
    """Несколько адресов -> список результатов. Адреса одного домена проверяются
    одной SMTP-сессией."""
    results, by_domain = [], {}
    parsed = []
    for email in emails or []:
        syntax = check_syntax(email)
        parsed.append(syntax)
        if syntax["is_valid_syntax"]:
            by_domain.setdefault(syntax["domain"], []).append(syntax["address"])

    mx_cache, smtp_cache = {}, {}
    for domain, addrs in by_domain.items():
        mx_cache[domain] = check_mx(domain)
        skip = ""
        if domain in EF.FREE_PROVIDERS:
            skip = "публичный почтовый сервис — личный ящик не проверяем"
        elif mx_cache[domain]["_state"] is False:
            skip = "домен не принимает почту"
        elif EG.smtp_trust(mx_cache[domain]["provider"]) == "none":
            # Exchange Online принимает почти любой адрес и проверяет получателя уже
            # после DATA — сессию тратить незачем, вердикта она не даст
            skip = (f"{mx_cache[domain]['provider']} отвечает «принято» на любой адрес — "
                    f"проба ничего не докажет")
        if skip:
            smtp_cache.update({a: _blank_smtp(skip) for a in addrs})
            continue
        smtp_cache.update(check_smtp_many(
            domain, addrs, mx_hosts=mx_cache[domain]["records"], timeout=timeout,
            helo=helo, mail_from=mail_from, port=port))

    for syntax in parsed:
        addr = syntax["address"]
        if not syntax["is_valid_syntax"]:
            results.append({"input": addr, "is_reachable": "invalid",
                            "reason": "некорректный адрес", "syntax": syntax,
                            "mx": {}, "smtp": _blank_smtp("адрес не разобран"),
                            "misc": check_misc(addr)})
            continue
        mx = mx_cache.get(syntax["domain"], {})
        smtp = smtp_cache.get(addr, _blank_smtp("проверка не выполнялась"))
        misc = check_misc(addr)
        state, reason = _reachable(smtp, misc, mx)
        provider = mx.get("provider") or ""
        results.append({
            "input": addr, "is_reachable": state, "reason": reason,
            "syntax": syntax,
            "mx": {k: mx.get(k) for k in ("accepts_mail", "records", "provider", "via", "note")},
            "smtp": smtp, "misc": misc,
            # чей ответ мы разбирали и насколько ему можно верить — этого у Reacher нет,
            # а без этого «safe» от mail.ru выглядит так же весомо, как от Яндекса
            "trust": EG.smtp_trust(provider) if provider else "unknown",
        })
    return results


def to_verdict(result):
    """Reacher-результат -> ok | no | unknown для остального пайплайна."""
    state = (result or {}).get("is_reachable")
    smtp = (result or {}).get("smtp") or {}
    if state == "safe":
        return "ok"
    if state == "risky":
        # ролевой ящик или catch-all: письмо дойдёт, но адресат не гарантирован
        return "ok" if smtp.get("is_deliverable") and not smtp.get("is_catch_all") else "unknown"
    if state == "invalid":
        # «нет ящика» засчитываем, только если сервер это действительно сказал
        return "no" if (smtp.get("can_connect_smtp") and not smtp.get("policy_blocked")) \
            or (result.get("mx") or {}).get("accepts_mail") is False else "unknown"
    return "unknown"


# ------------------------------------------------------- HTTP-API как у Reacher ----
def serve(port=8080, host="127.0.0.1"):
    """Поднять локальный HTTP-API, совместимый с Reacher.

    Нужен, чтобы EMAIL_VERIFIER_URL мог указывать сюда же — тогда переход на
    настоящий Reacher (или обратно) не требует правок кода."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):                          # noqa: N802 — имя задано BaseHTTPRequestHandler
            if not self.path.rstrip("/").endswith("check_email"):
                self._send(404, {"error": "unknown endpoint"})
                return
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 16384)
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                email = (data.get("to_email") or "").strip()
            except (ValueError, TypeError):
                self._send(400, {"error": "bad json"})
                return
            if not email:
                self._send(400, {"error": "to_email is required"})
                return
            self._send(200, check_email(
                email, helo=data.get("hello_name"), mail_from=data.get("from_email"),
                port=int(data.get("smtp_port") or 25)))

        def log_message(self, fmt, *args):          # тише в консоли пайплайна
            pass

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"email_verify: слушаю http://{host}:{port}/v1/check_email "
          f"(совместимо с Reacher). Ctrl+C — остановить.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен")
    finally:
        srv.server_close()


def main():
    ap = argparse.ArgumentParser(
        description="Проверка существования почтового ящика без отправки письма "
                    "(Python-порт ядра Reacher, без Docker и WSL)")
    ap.add_argument("emails", nargs="*", help="адреса для проверки")
    ap.add_argument("--json", action="store_true", help="выдать сырой JSON")
    ap.add_argument("--serve", type=int, metavar="PORT",
                    help="поднять HTTP-API, совместимый с Reacher")
    ap.add_argument("--timeout", type=float, default=10, help="таймаут SMTP, с (10)")
    ap.add_argument("--port", type=int, default=25, help="порт SMTP (25)")
    a = ap.parse_args()

    if a.serve:
        serve(a.serve)
        return
    if not a.emails:
        ap.error("укажите адреса либо --serve <порт>")

    results = check_emails(a.emails, timeout=a.timeout, port=a.port)
    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    for r in results:
        smtp, mx = r["smtp"], r["mx"]
        print(f"\n{r['input']}")
        print(f"  вердикт: {r['is_reachable']} — {r['reason']}")
        print(f"  доверие ответу: {r['trust']} (почта на: {mx.get('provider') or '—'})")
        print(f"  MX: {', '.join(mx.get('records') or []) or 'нет'}"
              + (f" [{mx['note']}]" if mx.get("note") else ""))
        print(f"  SMTP: подключение={smtp['can_connect_smtp']}, "
              f"принимает={smtp['is_deliverable']}, catch-all={smtp['is_catch_all']}"
              + (", переполнен" if smtp.get("has_full_inbox") else "")
              + (", отключён" if smtp.get("is_disabled") else "")
              + (f", ответ сервера: {smtp['code']} {smtp['message'][:60]}"
                 if smtp.get("code") else ""))
        misc = r["misc"]
        marks = [n for n, v in (("одноразовый", misc["is_disposable"]),
                                ("ролевой", misc["is_role_account"]),
                                ("публичный сервис", misc["is_b2c"])) if v]
        if marks:
            print(f"  пометки: {', '.join(marks)}")


if __name__ == "__main__":
    main()
