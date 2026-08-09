# -*- coding: utf-8 -*-
r"""
Источник лидов ГИР БО ФНС — перечисление РЕГИОНА по префиксам ИНН.

Зачем понадобился отдельный источник, хотя есть OfData и Checko. Оба матчат
основной ОКВЭД ТОЧНО, а в реестре крупный бизнес зарегистрирован на
детализированных кодах: у Татнефти 19.20.1, у КАМАЗа 29.10.4. Проверено вживую
2026-08-09: поиск OfData по 19.20 в Татарстане возвращает 16 компаний, и
Татнефти среди них нет. Чтобы не промахиваться, пришлось бы опрашивать все
уровни справочника — 788 кодов только по четырём отраслям, то есть 788 запросов
платного API ещё до первого контакта.

ГИР БО (bo.nalog.gov.ru) решает это иначе и бесплатно: его поиск принимает
ПРЕФИКС ИНН, а первые четыре цифры ИНН юрлица — код налоговой инспекции, то
есть регион. Перебрав префиксы 16xx, получаем все юрлица Татарстана, и в каждой
записи сразу есть ОКВЭД, регион и выручка (bfo.gainSum). Отрасль фильтруем уже
у себя — префиксно, а значит без промахов по детализированным кодам.

КОНТРАКТ (сверен вживую 2026-08-09):
  GET /advanced-search/organizations/search?query=<префикс ИНН>&page=N&size=200
  -> {"content": [{"inn", "shortName", "ogrn", "region", "okved2",
                   "bfo": {"period": "2025", "gainSum": <тыс. руб>}}],
      "totalElements": N, "totalPages": N}

Ограничения, найденные замером (не по документации, её нет):
  * size — до 200 записей на страницу;
  * offset (page*size) — строго меньше 10 000, дальше HTTP 500. Поэтому префикс,
    у которого больше 10 000 организаций, дробится на пятизначный (1650 -> 16500..16509);
  * пустой query ничего не возвращает — нужен именно префикс;
  * gainSum отдаётся в ТЫСЯЧАХ рублей;
  * фильтров по региону, ОКВЭД и выручке на стороне сервера НЕТ, всё клиентски.

Контактов здесь нет и быть не может — их добирает OfData /company
(`ofdata_contacts_pass`). Этот модуль отвечает только за «кто есть в регионе и
сколько заработал».

CLI:
  py source_girbo.py --region 16 --min-revenue 1e9 --year 2025 --limit 100 \
      --industries processing,construction --out D:\лиды\rt.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SEARCH = "https://bo.nalog.gov.ru/advanced-search/organizations/search"
CARD = "https://bo.nalog.gov.ru/organizations-card/{}"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://bo.nalog.gov.ru/",
}
PAGE_SIZE = 200
MAX_OFFSET = 10_000          # page*size >= 10000 -> HTTP 500
PAUSE = 0.2                  # темп: ГИР БО троттлит, а его отказ неотличим от «пусто»


class GirboSourceError(RuntimeError):
    """Сеть или неожиданный ответ ГИР БО."""


def log(message):
    print(message, flush=True)


def _digits(value):
    return re.sub(r"\D", "", str(value or ""))


def _get(params, timeout=30, retries=3):
    url = SEARCH + "?" + urllib.parse.urlencode(params)
    last = ""
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code == 500:                    # чаще всего это выход за offset
                return {}
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
        except Exception as exc:                   # noqa: BLE001 — сеть, таймаут, битый JSON
            last = type(exc).__name__
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
    raise GirboSourceError(f"поиск не выполнен ({last})")


def count_for(prefix):
    """Сколько организаций отдаёт ГИР БО по префиксу ИНН (1 запрос)."""
    data = _get({"query": prefix, "page": 0, "size": 1})
    return int(data.get("totalElements") or 0)


def region_prefixes(region_code, log=log):
    """Непустые префиксы ИНН региона: RR00..RR99 -> только те, где кто-то есть.

    Дробим до пятизначных там, где организаций больше, чем позволяет забрать
    ограничение offset — иначе хвост списка просто недостижим."""
    region_code = f"{int(region_code):02d}"
    found = []
    for tail in range(100):
        prefix = f"{region_code}{tail:02d}"
        total = count_for(prefix)
        time.sleep(PAUSE)
        if not total:
            continue
        if total <= MAX_OFFSET:
            found.append((prefix, total))
            continue
        log(f"    {prefix}: {total} — больше предела выдачи, дроблю на пятизначные")
        for digit in range(10):
            sub = f"{prefix}{digit}"
            sub_total = count_for(sub)
            time.sleep(PAUSE)
            if sub_total:
                found.append((sub, sub_total))
    total_all = sum(count for _, count in found)
    log(f"  префиксов с организациями: {len(found)} | организаций всего: {total_all}")
    return found


def iter_organizations(prefix, log=log):
    """Все записи по префиксу с учётом предела offset."""
    page = 0
    while page * PAGE_SIZE < MAX_OFFSET:
        data = _get({"query": prefix, "page": page, "size": PAGE_SIZE})
        rows = (data or {}).get("content") or []
        if not rows:
            return
        yield from rows
        page += 1
        time.sleep(PAUSE)


def record_to_lead(record, industry_key="", industry_cfg=None):
    """Запись ГИР БО -> лид в общем формате Фазы 1."""
    bfo = record.get("bfo") or {}
    gain = bfo.get("gainSum")
    revenue = int(gain) * 1000 if isinstance(gain, (int, float)) else None
    inn = _digits(record.get("inn"))
    cfg = industry_cfg or {}
    address = ", ".join(part for part in (
        record.get("index"), record.get("region"), record.get("city"),
        record.get("settlement"), record.get("street"), record.get("house")) if part)
    return {
        "name": (record.get("shortName") or record.get("fullName") or "").strip(),
        "niche": cfg.get("label", ""),
        "website": "", "phone": "", "email": "",
        "contact_person": "",
        "source": "ГИР БО ФНС (перечисление региона по ИНН)",
        "pain": cfg.get("pain", ""), "offer": cfg.get("offer", ""),
        "status": "", "next_step": "",
        "_inn": inn,
        "_ogrn": _digits(record.get("ogrn")),
        "_address": address,
        "_region": (record.get("region") or "").title(),
        "_okved": str(record.get("okved2") or ""),
        "_okved_descr": "",
        "_revenue": revenue,
        "_revenue_year": str(bfo.get("period") or ""),
        "_revenue_src": "girbo",
        "_revenue_source_name": "ГИР БО ФНС, строка 2110",
        "_revenue_source_url": CARD.format(record.get("id") or inn),
        "_industry": industry_key,
    }


def _region_keyword(region_code):
    """Опознавательное слово региона: «16» -> «татарстан».

    Берём название из справочника Checko и оставляем самое длинное слово без
    типовых «республика/область/край» — по нему и сверяем адрес из ГИР БО."""
    try:
        from source_checko import region_name
    except ImportError:
        return ""
    title = str(region_name(region_code) or "").lower().replace("ё", "е")
    words = [word for word in re.split(r"[^а-яa-z]+", title)
             if word and word not in ("республика", "область", "край", "автономный",
                                      "округ", "город", "федерального", "значения")]
    return max(words, key=len) if words else ""


def _in_region(record, inn, region_code, region_word):
    """Компания действительно из нужного региона.

    Две независимые проверки, потому что каждая по отдельности врёт:
      * поиск ГИР БО матчит префикс как ПОДСТРОКУ ИНН — по запросу «1650» приезжают
        компании других регионов, где эти цифры стоят в середине; первые две цифры
        ИНН — код региона регистрации, они это отсекают;
      * ИНН остаётся прежним при переезде, поэтому сверяем ещё и фактический адрес
        из выдачи — если название региона в нём есть и оно чужое, компания не наша.
    """
    if not inn.startswith(region_code):
        return False
    if not region_word:
        return True
    actual = (record.get("region") or "").lower().replace("ё", "е")
    return not actual or region_word in actual


def industry_of(okved, industry_map):
    """Отрасль по ОКВЭД — ПРЕФИКСНО.

    Здесь можно то, чего не позволяет API источников: у нас код уже на руках,
    поэтому 19.20.1 спокойно относится к отрасли с кодом 19.20, а 29.10.4 — к 29.10.
    Именно из-за точного матча на стороне API крупные предприятия и терялись."""
    code = str(okved or "").strip()
    if not code:
        return ""
    best_key, best_len = "", 0
    for key, cfg in industry_map.items():
        for prefix in cfg.get("okved", ()):
            prefix = str(prefix).strip()
            # Иерархия ОКВЭД вложена посимвольно: 29.1 > 29.10 > 29.10.4, поэтому
            # обычного startswith достаточно. Условие «prefix + точка» здесь не
            # годится: между 29.1 и 29.10.4 точки на этом месте нет.
            if len(prefix) >= 2 and code.startswith(prefix) and len(prefix) > best_len:
                best_key, best_len = key, len(prefix)
    return best_key


def harvest(region="16", min_revenue=1_000_000_000, year="", industries=None,
            limit=0, out_path=None, log=log):
    """Компании региона с выручкой >= порога. Контакты не трогаются."""
    from source_rusprofile import INDUSTRY

    wanted = [key for key in (industries or []) if key]
    unknown = [key for key in wanted if key not in INDUSTRY]
    if unknown:
        raise GirboSourceError(f"неизвестные отрасли: {', '.join(unknown)}")
    industry_map = {key: INDUSTRY[key] for key in wanted} if wanted else INDUSTRY
    year = str(year or "").strip()

    log(f"=== ГИР БО: регион {region}, порог {min_revenue / 1e9:g} млрд"
        + (f", отчётность за {year}" if year else "")
        + (f", отрасли: {', '.join(wanted)}" if wanted else ", все отрасли") + " ===")
    log("  ищу префиксы ИНН региона (по одному запросу на префикс)...")
    prefixes = region_prefixes(region, log=log)

    region_code = f"{int(region):02d}"
    region_word = _region_keyword(region_code)
    by_inn, scanned, skipped_year, skipped_industry, skipped_region = {}, 0, 0, 0, 0
    for prefix, total in prefixes:
        for record in iter_organizations(prefix, log=log):
            scanned += 1
            inn = _digits(record.get("inn"))
            if not inn or inn in by_inn:
                continue
            if not _in_region(record, inn, region_code, region_word):
                skipped_region += 1
                continue
            bfo = record.get("bfo") or {}
            gain = bfo.get("gainSum")
            if not isinstance(gain, (int, float)) or gain * 1000 < min_revenue:
                continue
            if year and str(bfo.get("period") or "") != year:
                skipped_year += 1
                continue
            key = industry_of(record.get("okved2"), industry_map)
            if wanted and not key:
                skipped_industry += 1
                continue
            by_inn[inn] = record_to_lead(record, key, INDUSTRY.get(key))
        log(f"    {prefix}: просмотрено {scanned}, отобрано {len(by_inn)}")

    leads = sorted(by_inn.values(), key=lambda lead: -(lead.get("_revenue") or 0))
    log(f"=== Просмотрено {scanned} организаций | прошли порог {len(leads)}"
        + (f" | отсеяно по году: {skipped_year}" if year else "")
        + (f" | вне заданных отраслей: {skipped_industry}" if wanted else "")
        + f" | чужой регион: {skipped_region} ===")

    if limit and len(leads) > int(limit):
        log(f"=== Лимит {int(limit)}: оставлены крупнейшие по выручке ===")
        leads = leads[:int(limit)]

    if out_path:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(leads, handle, ensure_ascii=False, indent=1)
        log(f"=== Сохранено: {out_path} ({len(leads)} лидов) ===")
    return leads


def _cli(argv):
    parser = argparse.ArgumentParser(
        description="Лиды из ГИР БО ФНС: регион по префиксам ИНН + выручка")
    parser.add_argument("--region", default="16", help="код региона (16 — Татарстан)")
    parser.add_argument("--min-revenue", type=float, default=1e9)
    parser.add_argument("--year", default="", help="год отчётности, например 2025")
    parser.add_argument("--industries", default="",
                        help="ключи INDUSTRY через запятую; пусто — все отрасли")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", default=None)
    parser.add_argument("--contacts", action="store_true",
                        help="добрать сайт/телефон/почту через OfData /company")
    parser.add_argument("--contacts-cap", type=int, default=0)
    parser.add_argument("--xlsx", default=None)
    args = parser.parse_args(argv)

    industries = [part.strip() for part in args.industries.split(",") if part.strip()]
    leads = harvest(region=args.region, min_revenue=args.min_revenue, year=args.year,
                    industries=industries, limit=args.limit, out_path=args.out)
    if args.contacts and leads:
        from source_ofdata import OfDataClient, ofdata_contacts_pass

        ofdata_contacts_pass(OfDataClient(), leads, cap=args.contacts_cap)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                json.dump(leads, handle, ensure_ascii=False, indent=1)
    if args.xlsx and leads:
        from build_excel import build

        label = ", ".join(industries) if industries else f"регион {args.region}"
        log(f"=== Excel: {build(leads, label, args.xlsx)} ===")

    for lead in leads[:20]:
        log(f"  {(lead.get('_revenue') or 0) / 1e9:8.2f} млрд  {lead.get('_revenue_year')}  "
            f"{lead.get('_inn'):<12} {lead.get('_okved'):<10} {lead.get('name', '')[:44]}")
    log(f"\nИТОГО: {len(leads)} лидов")
    return 0 if leads else 1


if __name__ == "__main__":
    try:
        sys.exit(_cli(sys.argv[1:]))
    except GirboSourceError as error:
        raise SystemExit(f"ГИР БО: {error}") from error
