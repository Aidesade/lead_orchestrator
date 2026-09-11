# -*- coding: utf-8 -*-
r"""
Проверка ОФИЦИАЛЬНОГО САЙТА и телефонов компании — стадия ФАЗЫ 1 сразу после
карточки RusProfile.

Зачем: контакты RusProfile/Checko — перепечатка данных ЕГРЮЛ, у которой нет даты.
На прозвоне 200 компаний около 40 номеров оказались мёртвыми (замер, из-за
которого написан `contact_source_probe.py`). Сайт самой компании свежее, поэтому
телефон с сайта считается основным, а номер карточки сохраняется рядом.

Что делается по компании:
  1. **существует ли сайт** — отвечает ли домен из карточки и его ли это сайт
     (ИНН в реквизитах страницы; «похоже на название» доказательством не считается);
  2. **какие телефоны опубликованы** — `tel:`-ссылки и текст контактных страниц;
  3. сверка с контактами лида: `_phone_verified` / `_email_verified`; пустая почта
     добирается с сайта прежним ранжированием (`webutil.apply_best`).

Поля в лиде: `_site_verdict` · `_site_reachable` · `_site_pages` ·
`_site_inn_confirmed` · `_site_phones` · `_site_emails` · `_phone_verified` ·
`_email_verified` · `_phone_rusprofile` (номер карточки) · `_phone_src`.

⚠️ **Отрасль ЖКХ (`water`) стадию НЕ проходит** — телефоны там добываются иначе
(решение 2026-09-11), и компания без сайта не должна из-за этого выпадать из
сбора. Список отраслей-исключений — `LEAD_SITE_VERIFY_SKIP` (дефолт `water`,
синоним `жкх`); пустое значение осознанно выключает исключения.

⚠️ **Гейт жёсткий:** компания без живого сайта или без телефонов на нём из выдачи
ВЫБРАСЫВАЕТСЯ. Поэтому сбор берёт из выдачи резерв сверх N (`site_reserve`) —
как для гейта ССЧ, иначе «N на отрасль» молча превратится в недобор.

Тумблеры: `LEAD_SITE_VERIFY` (1), `LEAD_SITE_VERIFY_SKIP` (`water`),
`LEAD_SITE_RESERVE` (30% от N, не меньше 3), `LEAD_SITE_VERIFY_WORKERS` (8),
`LEAD_SITE_PAGES` (6 попыток загрузки на компанию), `LEAD_SITE_TIMEOUT` (9 с на
страницу).

CLI:
  py site_verify.py "D:\лиды\leads_mining.json"           # отчёт -> *_verified.json
  py site_verify.py "D:\лиды\leads_mining.json" --gate     # ещё и выбросить непрошедших
"""
import argparse
import collections
import concurrent.futures as futures
import json
import os
import re
import socket
import sys
import time
import urllib.parse

import email_finder as ef
from harvest_inn_site import PATHS, _fetch, _norm_url, _scan, is_own_site
from webutil import apply_best

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

DEFAULT_PAGES = 6          # ПОПЫТОК загрузки на компанию (root + контакты/реквизиты)
DEFAULT_TIMEOUT = 9        # секунд на страницу
DEFAULT_WORKERS = 8
SKIP_DEFAULT = "water"     # ЖКХ: телефоны добываются иначе, стадию не проходит
# Вердикты, по которым компания выбрасывается из сбора.
DROP_VERDICTS = ("no_site", "unreachable", "no_phone")
_SKIP_ALIASES = {"жкх": "water", "zhkh": "water"}
_OFF = ("0", "false", "no", "off", "нет")


def _int_env(name, default, low=1, high=64):
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return max(low, min(high, int(float(raw))))
    except ValueError:
        return default


def enabled():
    """Включена ли стадия (`LEAD_SITE_VERIFY`, дефолт 1)."""
    return str(os.environ.get("LEAD_SITE_VERIFY", "1") or "").strip().lower() not in _OFF


def skip_industries():
    """Отрасли, которые стадию не проходят (`LEAD_SITE_VERIFY_SKIP`, дефолт ЖКХ).

    Пустое значение переменной — осознанное «проверять всех»; отличить его от
    «переменная не задана» можно только по `None`, поэтому дефолт подставляется
    именно при отсутствии ключа."""
    raw = os.environ.get("LEAD_SITE_VERIFY_SKIP")
    raw = SKIP_DEFAULT if raw is None else raw
    out = set()
    for token in str(raw).replace(";", ",").split(","):
        token = token.strip().lower()
        if token:
            out.add(_SKIP_ALIASES.get(token, token))
    return out


def is_skipped(lead):
    """Лид отрасли-исключения (ЖКХ): сайт не проверяем и из сбора не выбрасываем."""
    industry = str((lead or {}).get("_industry") or "").strip().lower()
    return bool(industry) and industry in skip_industries()


def site_reserve(per_industry):
    """Сколько компаний сверх N брать из выдачи под отсев по сайту (0 — стадия выключена).

    Дефолт — 30% от N, не меньше 3. Цифра оценочная: боевого замера доли компаний
    без живого сайта пока нет, поэтому `LEAD_SITE_RESERVE` задаёт резерв явно —
    как `LEAD_STAFF_RESERVE` для гейта ССЧ."""
    if not enabled():
        return 0
    raw = str(os.environ.get("LEAD_SITE_RESERVE", "") or "").strip()
    if not raw:
        return max(3, (int(per_industry) + 2) // 3)
    try:
        return max(0, int(raw))
    except ValueError as exc:
        raise ValueError(
            "LEAD_SITE_RESERVE должен быть целым числом компаний") from exc


# ── телефоны ────────────────────────────────────────────────────────────────
# `tel:`-ссылка — самый однозначный источник: там номер уже отделён от прочих
# цифр страницы. В тексте номер принимается только с кодом страны (+7/8) либо с
# кодом города в скобках — иначе в телефоны попали бы ИНН, ОГРН и номера счетов.
_TEL_HREF = re.compile(r"tel:\s*([+0-9][0-9\s()\-.]{5,24})", re.I)
_PHONE_TEXT = re.compile(
    r"(?:\+7|\b8)[\s\-\u2013\u2014().]{0,3}\(?\d{3,4}\)?[\s\-\u2013\u2014().]{0,3}"
    r"\d{2,3}[\s\-\u2013\u2014().]{0,3}\d{2}[\s\-\u2013\u2014().]{0,3}\d{2}"
    r"|\(\d{3,5}\)[\s\-.]{0,3}\d{2,3}[\s\-.]{0,3}\d{2}[\s\-.]{0,3}\d{2}")


def normalize_phone(raw, local_ok=False):
    """«8 (843) 292-11-22» -> «+78432921122». None — это не телефон.

    `local_ok` — принимать 10 цифр без кода страны: так номер пишут в `tel:` и в
    формате «(843) 292-11-22». В произвольном тексте 10 цифр подряд — чаще ИНН."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10 and local_ok:
        return "+7" + digits
    return None


def phone_tail(value):
    """Последние 10 цифр — форма сравнения номеров разных написаний."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else ""


def extract_phones(html):
    """Телефоны страницы: сначала `tel:`-ссылки, затем текст."""
    found = []
    for raw in _TEL_HREF.findall(html or ""):
        value = normalize_phone(raw, local_ok=True)
        if value:
            found.append(value)
    for raw in _PHONE_TEXT.findall(ef._strip_tags(html or "")):
        value = normalize_phone(raw, local_ok=raw.lstrip().startswith("("))
        if value:
            found.append(value)
    return list(dict.fromkeys(found))


def lead_phones(lead):
    """Номера, которые уже есть у лида (список `_phones` или строка `phone`)."""
    lead = lead or {}
    rows = [str(p).strip() for p in (lead.get("_phones") or []) if str(p).strip()]
    if not rows:
        rows = [p.strip() for p in re.split(r"[,;/]", lead.get("phone") or "") if p.strip()]
    return rows


# ── обход сайта ─────────────────────────────────────────────────────────────
def _empty_report():
    """Отчёт обхода до первой страницы: им же отвечаем на сбой обхода."""
    return {"reachable": False, "visited": [], "phones": [], "emails": [],
            "candidates": [], "text": "", "inn": "", "ogrn": "", "error": "",
            "deadline": False, "dns": None}


def _dns_ok(url):
    """Резолвится ли домен. «Домена нет» и «нас не пустили» — разные вещи.

    Замер 2026-09-11 по живым сайтам: kamaz.ru, tatavtodor.ru и sibur.ru
    читаются обычным HTTP, а tatneft.ru не отвечает ни по http, ни по https
    (WAF/гео — домен при этом резолвится). Гейт в обоих случаях один, но в
    логе и в лиде причина разная, иначе «сайта нет» не отличить от «нас не
    пустили» — это ровно та ошибка, что уже стоила пайплайну верификатора почты."""
    host = urllib.parse.urlsplit(_norm_url(url)).hostname or ""
    if not host:
        return False
    try:
        socket.getaddrinfo(host, None)
        return True
    except Exception:                       # noqa: BLE001 — не резолвится = ответ «нет»
        return False


def scan_site(website, pages=None, timeout=None, deadline=None, fetch=None, want=()):
    """Обойти контактные страницы сайта и вернуть отчёт. Единственное место стадии,
    которое ходит в сеть — и только на сайт самой компании.

    `want` — «хвосты» телефонов лида: как только сайт жив, телефоны найдены, все
    искомые номера подтверждены и реквизиты прочитаны, обход прекращается.

    ⚠️ Бюджет `pages` считает ПОПЫТКИ, а не удачные страницы (отсюда срез
    `PATHS[:pages]`): на сайте без /rekvizity и /oferta неудачная попытка стоит
    того же таймаута, что удачная, и бюджет по удачным страницам растянул бы
    проверку одной компании на все 18 путей `harvest_inn_site.PATHS`."""
    fetch = fetch or _fetch
    pages = pages or _int_env("LEAD_SITE_PAGES", DEFAULT_PAGES, 1, 20)
    timeout = timeout or _int_env("LEAD_SITE_TIMEOUT", DEFAULT_TIMEOUT, 2, 60)
    base = _norm_url(website).rstrip("/")
    report = _empty_report()
    phones, texts = [], []
    want = {tail for tail in want if tail}
    for path in PATHS[:pages]:
        if deadline is not None and time.monotonic() >= deadline:
            report["deadline"] = True
            break
        try:
            html = fetch(base + path, timeout=timeout)
        except Exception as exc:            # noqa: BLE001 — страница-404 не сбой стадии
            report["error"] = type(exc).__name__
            continue
        report["reachable"] = True
        report["visited"].append(path or "/")
        texts.append(ef._strip_tags(html))
        phones.extend(extract_phones(html))
        for cand in ef.extract_candidates(html, path or "/"):
            if not ef.is_junk(cand.get("email", "")):
                report["candidates"].append(cand)
        if not report["inn"]:
            inn, ogrn = _scan(html)
            report["inn"] = inn or ""
            report["ogrn"] = ogrn or ""
        if phones and report["inn"] and want <= {phone_tail(p) for p in phones}:
            break
    report["phones"] = list(dict.fromkeys(phones))
    report["emails"] = list(dict.fromkeys(
        cand["email"].lower() for cand in report["candidates"]))
    report["text"] = " ".join(texts)
    if not report["reachable"] and not report["deadline"]:
        report["dns"] = _dns_ok(website)
    return report


def _email_confirmed(email, site_emails):
    """Почта лида подтверждена сайтом: тот же адрес или тот же домен."""
    email = str(email or "").strip().lower()
    if not email or "@" not in email:
        return False
    if email in site_emails:
        return True
    domain = email.split("@")[-1]
    return any(domain == addr.split("@")[-1] for addr in site_emails if "@" in addr)


def _apply_phones(lead, site_phones):
    """Телефоны сайта — основные; номера карточки сохраняются и идут следом.

    Ни один известный номер не теряется: `_phone_rusprofile` хранит строку
    карточки, `_phones` — весь список (сайт впереди), `phone` — та же строка под
    ширину таблиц и контракт CRM."""
    card = lead_phones(lead)
    site_tails = {phone_tail(p) for p in site_phones}
    rest = [p for p in card if phone_tail(p) not in site_tails]
    if card:
        lead.setdefault("_phone_rusprofile", ", ".join(card)[:90])
    lead["_phones"] = list(site_phones) + rest
    lead["phone"] = ", ".join(lead["_phones"])[:90]
    lead["_phone_src"] = "офиц. сайт"


def verify_lead(lead, pages=None, timeout=None, deadline=None, fetch=None):
    """Проверить сайт одной компании и записать результат в лид.

    Вердикт: `ok` · `skip` (ЖКХ или стадия выключена) · `no_site` · `unreachable`
    · `no_phone` · `deadline` (общий лимит прогона истёк до первой страницы).
    Исключений наружу не бросает: сбой проверки — это вердикт, а не крах сбора."""
    if not isinstance(lead, dict) or not enabled():
        return "skip"
    if is_skipped(lead):
        lead["_site_verdict"] = "skip"
        lead["_site_checked"] = False
        return "skip"
    website = lead.get("website")
    if not is_own_site(website):
        lead["_site_verdict"] = "no_site"
        lead["_site_checked"] = True
        lead["_site_reachable"] = False
        return "no_site"

    tails = {tail for tail in (phone_tail(p) for p in lead_phones(lead)) if tail}
    try:
        report = scan_site(website, pages=pages, timeout=timeout, deadline=deadline,
                           fetch=fetch, want=tails)
    except Exception as exc:                # noqa: BLE001 — стадия не валит сбор
        report = _empty_report()
        report["error"] = type(exc).__name__
    lead["_site_checked"] = True
    lead["_site_reachable"] = bool(report["reachable"])
    lead["_site_pages"] = list(report["visited"])
    if report["inn"]:
        # ИНН в реквизитах — единственное доказательство, что сайт принадлежит
        # именно этой компании: на агрегаторе её название тоже есть.
        lead["_site_inn_confirmed"] = (
            report["inn"] == str(lead.get("_inn") or "").strip())
    if not report["reachable"]:
        if report["deadline"]:
            lead["_site_verdict"] = "deadline"
            return "deadline"
        lead["_site_verdict"] = "unreachable"
        if report["error"]:
            lead["_site_error"] = report["error"]
        if report["dns"] is not None:
            lead["_site_dns_ok"] = bool(report["dns"])
        return "unreachable"

    lead["_site_phones"] = list(report["phones"])
    lead["_site_emails"] = list(report["emails"])
    lead["_phone_verified"] = bool(tails & {phone_tail(p) for p in report["phones"]})
    lead["_email_verified"] = _email_confirmed(lead.get("email"), report["emails"])
    # Почта добирается, только если её НЕТ: найденную раньше не понижаем.
    if report["candidates"] and not str(lead.get("email") or "").strip():
        if apply_best(lead, report["candidates"], report["text"]):
            lead["_email_src"] = "офиц. сайт"
    if not report["phones"]:
        lead["_site_verdict"] = "no_phone"
        return "no_phone"
    _apply_phones(lead, report["phones"])
    lead["_site_verdict"] = "ok"
    return "ok"


def gate(leads, log=print, workers=None, pages=None, timeout=None, fetch=None):
    """Проверить сайты пачкой и выбросить компании без живого сайта/телефонов.

    -> (оставшиеся лиды, статистика вердиктов). Стадия выключена — лиды как есть."""
    leads = list(leads or [])
    if not enabled() or not leads:
        return leads, {}
    workers = workers or _int_env("LEAD_SITE_VERIFY_WORKERS", DEFAULT_WORKERS, 1, 32)
    skipped = sorted(skip_industries())
    log(f"[сайт] проверка сайта и телефонов: {len(leads)} компаний, потоков {workers}"
        + (f" | без проверки отрасли: {', '.join(skipped)}" if skipped else ""))
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        verdicts = list(pool.map(
            lambda lead: verify_lead(lead, pages=pages, timeout=timeout, fetch=fetch),
            leads))
    stats = collections.Counter(verdicts)
    kept = [lead for lead, verdict in zip(leads, verdicts)
            if verdict not in DROP_VERDICTS]
    confirmed = sum(1 for lead in kept if lead.get("_phone_verified"))
    no_dns = sum(1 for lead in leads if lead.get("_site_dns_ok") is False)
    log(f"[сайт] прошло {len(kept)} из {len(leads)} | телефон с сайта {stats['ok']} "
        f"(номер карточки подтверждён у {confirmed}) | без сайта {stats['no_site']} | "
        f"сайт не отвечает {stats['unreachable']}"
        + (f" (из них домена нет {no_dns}, остальных не пустил сервер)" if no_dns else "")
        + f" | без телефонов на сайте {stats['no_phone']}"
        + (f" | без проверки {stats['skip']}" if stats["skip"] else ""))
    return kept, dict(stats)


def verify_contacts(leads, log=print):
    """Совместимость с прежним CLI: проверить всех и вернуть статистику вердиктов."""
    _, stats = gate(list(leads or []), log=log)
    return stats


def main():
    ap = argparse.ArgumentParser(
        description="Проверка сайта и телефонов лидов (стадия ФАЗЫ 1)")
    ap.add_argument("path", help="JSON базы лидов")
    ap.add_argument("--gate", action="store_true",
                    help="выбросить компании без живого сайта или телефонов на нём")
    ap.add_argument("--workers", type=int, default=None)
    a = ap.parse_args()
    leads = json.load(open(a.path, encoding="utf-8"))
    kept, stats = gate(leads, workers=a.workers)
    rows = kept if a.gate else leads
    out = a.path.replace(".json", "_verified.json")
    json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[stats] {stats}")
    print(f"written: {out} ({len(rows)} компаний)")


if __name__ == "__main__":
    main()
