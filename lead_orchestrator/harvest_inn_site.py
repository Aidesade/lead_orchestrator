# -*- coding: utf-8 -*-
"""
Снятие ИНН/ОГРН с собственного сайта лида (бесплатный Layer 0).

Многие бизнесы (в т.ч. ИП) публикуют реквизиты в футере или на страницах
«Реквизиты»/«Оферта»/«Политика»/«Контакты» (часто обязательно при онлайн-записи
и приёме оплаты). Берём ТОЛЬКО помеченные «ИНН …»/«ОГРН …» цифры и проверяем
контрольной суммой (inn_util) — чтобы не схватить телефон/счёт. ИНН затем
резолвится в ЛПР через Dadata (dadata_enrich).

Сайты салонов почти все статические -> грузим обычным HTTP (без браузера, быстро).
Соцсети/агрегаторы (vk/instagram/dikidi/yclients/2gis/...) пропускаем — там не
реквизиты салона. Найденный ИНН пишем в lead["_inn"], источник — в lead["_inn_src"].

CLI:
  py harvest_inn_site.py "D:\\лиды\\…json" [--limit N] [--inplace]
"""
import argparse
import gzip
import html as htmllib
import io
import json
import re
import socket
import time
import urllib.request
from urllib.parse import urlparse

from inn_util import valid_inn, valid_ogrn

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

SKIP_HOSTS = ("vk.com", "vk.ru", "instagram.", "facebook.", "t.me", "telegram.",
              "ok.ru", "wa.me", "api.whatsapp", "dikidi.", "yclients.", "2gis.",
              "taplink.", "linktr.ee", "youtube.", "zoon.", "yandex.ru/maps",
              "flamp.", "avito.")

PATHS = ("", "/kontakty", "/contacts", "/contact", "/rekvizity", "/requisites",
         "/oferta", "/publichnaya-oferta", "/offer", "/dogovor-oferty",
         "/policy", "/privacy", "/politika-konfidencialnosti",
         "/personal-data", "/about", "/o-nas", "/info")

# помеченные реквизиты: «ИНН 7701234567», «ОГРНИП 312774…»
RE_INN = re.compile(r"ИНН[^0-9]{0,8}(\d[\d\s]{8,13}\d)", re.I)
RE_OGRN = re.compile(r"ОГРН(?:ИП)?[^0-9]{0,8}(\d[\d\s]{11,16}\d)", re.I)
RE_TAG = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)


def _norm_url(site):
    site = site.strip()
    if not site.startswith("http"):
        site = "http://" + site
    return site


def _host(site):
    try:
        return urlparse(_norm_url(site)).netloc.lower()
    except Exception:
        return ""


DOMAIN_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}(:\d+)?$")


def is_own_site(site):
    """True только для настоящего латинского домена-сайта (не соцсеть/агрегатор
    и не мусор вроде «Онлайн-запись», который в базе лежит в поле website)."""
    if not site:
        return False
    h = _host(site)
    if not h or not DOMAIN_RE.match(h):
        return False
    full = h + urlparse(_norm_url(site)).path.lower()
    return not any(b in full for b in SKIP_HOSTS)


def _fetch(url, timeout=12):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "ru,en;q=0.8",
        "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        ct = r.headers.get("Content-Type", "")
        enc = "utf-8"
        m = re.search(r"charset=([\w\-]+)", ct)
        if m:
            enc = m.group(1)
        return raw.decode(enc, "replace")


def _scan(page_html):
    """Вернуть (inn, ogrn) — первые валидные помеченные реквизиты, или (None,None).
    Срезаем script/style, затем ВСЕ теги и декодируем html-entities — иначе
    «ИНН</span><span>7707…» рвёт привязку метки к цифрам."""
    text = RE_TAG.sub(" ", page_html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = htmllib.unescape(text)
    inn = ogrn = None
    for m in RE_INN.finditer(text):
        d = re.sub(r"\D", "", m.group(1))
        if valid_inn(d):
            inn = d
            break
    for m in RE_OGRN.finditer(text):
        d = re.sub(r"\D", "", m.group(1))
        if valid_ogrn(d):
            ogrn = d
            break
    return inn, ogrn


def harvest_one(site, max_pages=6):
    """Обойти сайт лида и вернуть (inn, ogrn, page) или (None,None,None)."""
    base = _norm_url(site).rstrip("/")
    tried = 0
    for path in PATHS:
        if tried >= max_pages:
            break
        url = base + path
        try:
            html = _fetch(url)
        except Exception:
            continue
        tried += 1
        inn, ogrn = _scan(html)
        if inn or ogrn:
            return inn, ogrn, (path or "/")
    return None, None, None


def harvest(leads, limit=None, log=print):
    """Проставить _inn/_ogrn/_inn_src лидам с собственным сайтом. Возвращает статистику."""
    targets = [l for l in leads
               if is_own_site(l.get("website")) and not l.get("_inn")]
    if limit:
        # равномерная выборка для замера
        step = max(1, len(targets) // limit)
        targets = targets[::step][:limit]
    log(f"=== Снятие ИНН с сайтов: кандидатов {len(targets)} "
        f"(из {len(leads)} лидов) ===")
    hit = 0
    for i, l in enumerate(targets, 1):
        inn, ogrn, page = harvest_one(l["website"])
        if inn or ogrn:
            hit += 1
            l["_inn"] = inn or ""
            l["_ogrn"] = ogrn or ""
            l["_inn_src"] = f"site{page}"
            log(f"  [{i:>3}] OK  {l['name'][:32]:34} ИНН={inn or '-'} "
                f"ОГРН={ogrn or '-'}  {l['website'][:30]} {page}")
        elif i % 20 == 0:
            log(f"  [{i:>3}] … обработано, найдено {hit}")
        time.sleep(0.05)
    log(f"=== ИНН с сайта получен у {hit}/{len(targets)} кандидатов "
        f"({100*hit/max(1,len(targets)):.0f}% от имеющих свой сайт) ===")
    return {"candidates": len(targets), "hits": hit}


def main():
    socket.setdefaulttimeout(15)
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path")
    ap.add_argument("--limit", type=int, default=None, help="замер на выборке N")
    ap.add_argument("--inplace", action="store_true", help="перезаписать JSON с _inn")
    a = ap.parse_args()
    leads = json.load(open(a.json_path, encoding="utf-8"))
    stats = harvest(leads, limit=a.limit)
    if a.inplace and not a.limit:
        json.dump(leads, open(a.json_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"[saved] {a.json_path}")
    print(f"[stats] {stats}")


if __name__ == "__main__":
    main()
