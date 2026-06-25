# -*- coding: utf-8 -*-
"""
Бесплатная ВЫРУЧКА («оборот») компании по ИНН из ОФИЦИАЛЬНОГО ГИР БО ФНС
(bo.nalog.gov.ru) — без ключа и без суточного лимита.

Один HTTP-запрос на ИНН: эндпоинт advanced-search уже возвращает последнюю
сданную бухотчётность в блоке ``bfo`` -> ``gainSum`` (выручка, строка 2110,
в ТЫС. руб) и ``period`` (год). Дополнительные drill-down запросы не нужны.

  GET https://bo.nalog.gov.ru/advanced-search/organizations/search?query=<ИНН>&page=0
  -> {"content":[{"inn":"...","shortName":"...","bfo":{"period":"2025","gainSum":16483582}}]}

Честные ограничения публичных данных (важно НЕ обещать лишнего):
 - выручку отдают только ЮЛ (ООО, АО…), сдающие бухотчётность в ГИР БО;
 - ИП бухотчётность НЕ сдают -> их выручки нет нигде в открытом доступе;
 - банки и страховые сдают отчётность в ЦБ, а не в ГИР БО -> здесь их нет;
 - это БУХГАЛТЕРСКАЯ выручка (метод начисления, стр. 2110), а НЕ оборот по
   расчётному счёту — последний составляет банковскую тайну и недоступен.

Fallback: ``data.finance.income`` из Dadata (доходы по данным ФНС) — если
карточку всё равно тянули в dadata_enrich, значение прокидывается бесплатно.

CLI-проверка:  py revenue_enrich.py 7734418612
"""
import concurrent.futures as cf
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# консоль Windows = cp1251 и роняет вывод на ₽/кириллице -> принудительно utf-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SEARCH = "https://bo.nalog.gov.ru/advanced-search/organizations/search"
CARD = "https://bo.nalog.gov.ru/organizations-card/{}"  # человекочитаемая карточка-источник

# Источники выручки для компаний с ЗАКРЫТОЙ отчётностью (ОПК / подсанкционные):
# в ГИР БО их нет, но цифры просачиваются в деловую прессу. Ключ — ИНН.
# Реестр расширяемый: добавляй сюда проверенные медиа-источники по ИНН.
MEDIA_FALLBACK = {
    "1656002652": {  # АО «Казанский вертолётный завод» (Вертолёты России / Ростех)
        "url": "https://m.business-gazeta.ru/news/668146",
        "name": "Business Gazeta",
        "note": "Отчётность закрыта (ОПК); выручка по данным деловой прессы",
    },
}

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": "https://bo.nalog.gov.ru/",
}
_TAG = re.compile(r"<[^>]+>")  # ГИР БО подсвечивает совпадение тегами <strong>


def _digits(s):
    return re.sub(r"\D", "", str(s or ""))


def girbo_revenue(inn, timeout=20, retries=2):
    """ИНН -> выручка из ГИР БО или {} (нет ЮЛ / ИП / нет отчётности / ошибка).

    Возвращает: {'revenue': <руб>, 'year': '2024', 'org': 'ООО ...', 'src': 'girbo'}.
    """
    inn = _digits(inn)
    if len(inn) not in (10, 12):
        return {}
    url = SEARCH + "?" + urllib.parse.urlencode({"query": inn, "page": 0})
    data = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries:
                continue
            return {}
        except Exception:
            if attempt < retries:
                continue
            return {}
    for org in (data.get("content") or []):
        if _digits(_TAG.sub("", org.get("inn") or "")) != inn:
            continue
        bfo = org.get("bfo") or {}
        gain = bfo.get("gainSum")
        if gain is None:
            return {}  # ЮЛ найдено, но отчётности нет (молодое/на спецрежиме)
        try:
            revenue = int(gain) * 1000  # ГИР БО хранит в тыс. руб
        except (TypeError, ValueError):
            return {}
        oid = org.get("id")
        return {
            "revenue": revenue,
            "year": str(bfo.get("period") or ""),
            "org": _TAG.sub("", org.get("shortName") or ""),
            "src": "girbo",
            "source_url": CARD.format(oid) if oid else "",
            "source_name": "ГИР БО ФНС",
        }
    return {}


def add_revenue(leads, workers=8, log=print):
    """Проставить лидам _revenue / _revenue_year / _revenue_src из ГИР БО по _inn.

    ГИР БО (точная выручка стр. 2110) перетирает значение из Dadata-income.
    ИП и фирмы без отчётности остаются без выручки — это нормально и помечается.
    Возвращает статистику {'revenue_hits': n, 'revenue_total': m}.
    """
    todo = [l for l in leads if l.get("_inn")]
    if not todo:
        log("=== Выручка (ГИР БО): нет лидов с ИНН — пропуск ===")
        return {"revenue_hits": 0, "revenue_total": 0}
    log(f"=== Выручка (ГИР БО): запрашиваем по {len(todo)} ИНН ===")
    hit = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda l: (l, girbo_revenue(l["_inn"])), todo))
    for l, res in results:
        if res.get("revenue") is not None:
            l["_revenue"] = res["revenue"]
            l["_revenue_year"] = res["year"]
            l["_revenue_src"] = "girbo"
            l["_revenue_source_url"] = res.get("source_url", "")
            l["_revenue_source_name"] = res.get("source_name", "ГИР БО ФНС")
            hit += 1
        elif l.get("_revenue") and not l.get("_revenue_src"):
            l["_revenue_src"] = "dadata"  # ранее подхвачено из Dadata income
            l.setdefault("_revenue_source_name", "Dadata (ФНС, открытые данные)")
    # компании с закрытой отчётностью -> занести медиа-источник по ИНН
    for l in leads:
        fb = MEDIA_FALLBACK.get(_digits(l.get("_inn")))
        if fb and not l.get("_revenue_source_url"):
            l["_revenue_source_url"] = fb["url"]
            l["_revenue_source_name"] = fb["name"]
            l.setdefault("_revenue_note", fb["note"])
    for l in leads:
        is_ip = (l.get("_type") == "INDIVIDUAL"
                 or (l.get("_opf") or "").upper().startswith("ИП"))
        if not l.get("_revenue") and is_ip:
            l.setdefault("_revenue_note",
                         "ИП — бухотчётность не сдаёт, выручка не публикуется")
    log(f"=== Выручка: найдена у {hit}/{len(todo)} "
        f"(ИП и фирмы без отчётности — пусто, это норма) ===")
    return {"revenue_hits": hit, "revenue_total": len(todo)}


if __name__ == "__main__":
    import sys
    inn = sys.argv[1] if len(sys.argv) > 1 else "7734418612"
    res = girbo_revenue(inn)
    if res.get("revenue") is not None:
        print(f"{res['org']}: выручка {res['revenue']:,} ₽ за {res['year']} "
              f"({res['src']})".replace(",", " "))
    else:
        print(f"ИНН {inn}: выручка в ГИР БО не найдена "
              f"(ИП / нет отчётности / банк / ошибка сети)")
