# -*- coding: utf-8 -*-
r"""
verify_host_check — годится ли ЭТА машина для проверки почтовых ящиков.

Массовая проверка существования ящиков (email_verify) упирается не в код, а в
репутацию адреса, с которого идёт SMTP-сессия. Практика по 69 компаниям с рабочей
Windows-машины: подтвердить удалось 5, остальные серверы отвечали
«Client host rejected: cannot find your reverse» либо рвали соединение.

Скрипт проверяет ровно то, из-за чего это происходит, и говорит, что чинить:
  1. открыт ли ИСХОДЯЩИЙ порт 25 (в облаках закрыт по умолчанию, открывают заявкой);
  2. есть ли PTR (обратная запись) у внешнего IP и совпадает ли она с HELO-именем;
  3. есть ли SPF у домена, которым представляемся в MAIL FROM;
  4. принимает ли нас живой почтовый сервер (контрольная сессия без письма).

Запускать НА ТОЙ машине, с которой планируется проверка:
  py verify_host_check.py
  py verify_host_check.py --helo mail.example.ru --from verify@example.ru
"""
import argparse
import json
import os
import socket
import sys
import urllib.request

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DOH = "https://dns.google/resolve"
# на этих серверах проверяем, пустят ли нас: разные политики и разные владельцы
CONTROL_MX = (("mx01.nicmail.ru", "проверяет мягко"),
              ("mx.yandex.net", "Яндекс 360"),
              ("mxs.mail.ru", "mail.ru"))


def _dns(name, rtype):
    try:
        url = f"{DOH}?name={urllib.parse.quote(name)}&type={rtype}" if False else \
            f"{DOH}?name={name}&type={rtype}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read(1 << 20).decode("utf-8"))
    except Exception as exc:
        return {"_error": str(exc)[:80]}


def external_ip():
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                ip = r.read(64).decode("utf-8").strip()
            if ip.count(".") == 3:
                return ip
        except Exception:
            continue
    return ""


def check_port25():
    """Открыт ли исходящий 25 — без него проверка невозможна в принципе."""
    results = []
    for host, note in CONTROL_MX:
        try:
            s = socket.create_connection((host, 25), timeout=8)
            banner = s.recv(200).decode("utf-8", "replace").strip()
            s.close()
            results.append((host, note, True, banner[:70]))
        except Exception as exc:
            results.append((host, note, False, str(exc)[:60]))
    return results


def check_ptr(ip):
    """PTR — главная причина отказов: сервер смотрит, есть ли у IP обратное имя."""
    if not ip:
        return None, "внешний IP не определён"
    rev = ".".join(reversed(ip.split("."))) + ".in-addr.arpa"
    data = _dns(rev, "PTR")
    if data.get("_error"):
        return None, f"DNS недоступен: {data['_error']}"
    names = [a.get("data", "").rstrip(".") for a in data.get("Answer", [])
             if a.get("type") == 12]
    return (names[0] if names else ""), ""


def check_spf(domain):
    """SPF домена из MAIL FROM: без него нас считают подделкой отправителя."""
    data = _dns(domain, "TXT")
    if data.get("_error"):
        return None, f"DNS недоступен: {data['_error']}"
    for ans in data.get("Answer", []):
        txt = (ans.get("data") or "").strip('"')
        if txt.lower().startswith("v=spf1"):
            return txt, ""
    return "", ""


def live_probe(helo, mail_from):
    """Контрольная сессия: примет ли нас реальный сервер. Письмо не отправляется."""
    import smtplib
    out = []
    for host, note in CONTROL_MX:
        try:
            s = smtplib.SMTP(host, 25, timeout=12)
            code, msg = s.ehlo(helo)
            if code >= 400:
                s.helo(helo)
            s.mail(mail_from)
            code, msg = s.rcpt(f"zzcheck0000@{host.split('.', 1)[1]}")
            text = msg.decode("utf-8", "replace") if isinstance(msg, bytes) else str(msg)
            s.quit()
            out.append((host, note, code, text[:70]))
        except Exception as exc:
            out.append((host, note, 0, f"СБОЙ: {str(exc)[:60]}"))
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Проверить, годится ли эта машина для проверки почтовых ящиков")
    ap.add_argument("--helo", default=os.environ.get("EMAIL_GUESS_HELO", ""),
                    help="имя, которым представляемся (EMAIL_GUESS_HELO)")
    ap.add_argument("--from", dest="mail_from",
                    default=os.environ.get("EMAIL_GUESS_MAIL_FROM", ""),
                    help="адрес в MAIL FROM (EMAIL_GUESS_MAIL_FROM)")
    a = ap.parse_args()

    problems, warnings = [], []
    print("=== 1. Исходящий порт 25 ===")
    for host, note, ok, detail in check_port25():
        print(f"  {host:18} {'ОТКРЫТ ' if ok else 'ЗАКРЫТ '} ({note}) {detail}")
    if not any(ok for _, _, ok, _ in check_port25()):
        problems.append("исходящий порт 25 закрыт — у провайдера нужно открыть его заявкой")

    print("\n=== 2. Внешний IP и обратная запись (PTR) ===")
    ip = external_ip()
    print(f"  внешний IP: {ip or 'не определён'}")
    ptr, err = check_ptr(ip)
    if err:
        print(f"  PTR: {err}")
        warnings.append(err)
    elif ptr:
        print(f"  PTR: {ptr}")
        if a.helo and ptr.lower() != a.helo.lower():
            print(f"       ⚠ не совпадает с HELO «{a.helo}»")
            warnings.append(f"PTR ({ptr}) не совпадает с HELO ({a.helo}) — часть серверов это режет")
    else:
        print("  PTR: НЕТ")
        problems.append("у IP нет обратной записи (PTR) — именно из-за этого приходит "
                        "«cannot find your reverse»; PTR настраивается в панели провайдера")

    print("\n=== 3. SPF домена отправителя ===")
    domain = a.mail_from.rsplit("@", 1)[-1] if "@" in a.mail_from else ""
    if not domain:
        print("  адрес MAIL FROM не задан (EMAIL_GUESS_MAIL_FROM)")
        problems.append("не задан EMAIL_GUESS_MAIL_FROM — без него проверка не запускается")
    else:
        spf, err = check_spf(domain)
        if err:
            print(f"  {err}")
        elif spf:
            print(f"  {domain}: {spf[:100]}")
            if ip and ip not in spf and "include" not in spf and "a" not in spf.split():
                warnings.append(f"IP {ip} явно не указан в SPF домена {domain}")
        else:
            print(f"  {domain}: SPF НЕ НАЙДЕН")
            problems.append(f"у домена {domain} нет SPF-записи — добавьте TXT «v=spf1 ip4:{ip or '<IP>'} ~all»")

    if a.helo and a.mail_from:
        print("\n=== 4. Контрольная сессия с живыми серверами (письмо не отправляется) ===")
        for host, note, code, detail in live_probe(a.helo, a.mail_from):
            verdict = ("принимает проверку" if code in (250, 251, 550, 553)
                       else "ОТКАЗ" if code else "не соединились")
            print(f"  {host:18} {str(code):4} {verdict:20} {detail}")
    else:
        print("\n=== 4. Контрольная сессия пропущена: задайте --helo и --from ===")

    print("\n" + "=" * 70)
    if problems:
        print("НЕ ГОТОВО. Что мешает:")
        for p in problems:
            print(f"  ✗ {p}")
    else:
        print("ГОТОВО: препятствий для массовой проверки не видно.")
    for w in warnings:
        print(f"  ⚠ {w}")
    print("\nПосле устранения запускать так:")
    print("  set EMAIL_GUESS_HELO=<имя из PTR>   &&  set EMAIL_GUESS_MAIL_FROM=<ящик на своём домене>")
    print("  py email_guess.py --leads <leads.json> --smtp")
    print("либо поднять верификатор на этой машине и ходить в него с рабочей:")
    print("  py email_verify.py --serve 8080      (на рабочей: EMAIL_VERIFIER_URL=http://<ip>:8080)")
    return 1 if problems else 0


if __name__ == "__main__":
    import urllib.parse                            # noqa: F401 — используется в _dns
    raise SystemExit(main())
