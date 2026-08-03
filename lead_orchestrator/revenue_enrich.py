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
import time
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


def girbo_revenue_ex(inn, timeout=20, retries=3):
    """ИНН -> (status, data). status: 'ok' | 'none' | 'fail'.

    ⚠️ ЗАЧЕМ ОТДЕЛЬНЫЙ СТАТУС. Раньше и «отчётности нет», и «запрос не прошёл» давали
    одинаковый пустой ответ. Пока ГИР БО лишь СВЕРЯЛ выручку у полусотни уже отобранных
    лидов, это было безобидно. Но как только он становится ФИЛЬТРОМ над сотнями кандидатов
    (источник Checko), склейка превращается в тихую потерю лидов: компания с реальной
    выручкой, чей запрос затроттлили, молча выпадает как «выручки нет».
    Замерено на 60 ИНН: 8 потоков без пауз -> выручка нашлась у 8; 2 потока с паузой 0.25 ->
    у 48. То есть наивный темп терял 40 лидов из 60.

    'fail' обязан обрабатываться вызывающим (ретрай/отчёт), а НЕ приравниваться к 'none'.
    """
    inn = _digits(inn)
    if len(inn) not in (10, 12):
        return "none", {}
    url = SEARCH + "?" + urllib.parse.urlencode({"query": inn, "page": 0})
    data = None
    last = ""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (429, 500, 502, 503) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))   # БЭКОФФ: раньше ретрай шёл без паузы
                continue
            return "fail", {"error": last}
        except Exception as e:
            last = type(e).__name__
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            return "fail", {"error": last}
    return _parse_girbo(data, inn)


def _parse_girbo(data, inn):
    """Ответ ГИР БО -> ('ok', {...}) | ('none', {}). Сетевые сбои сюда не попадают."""
    for org in ((data or {}).get("content") or []):
        if _digits(_TAG.sub("", org.get("inn") or "")) != inn:
            continue
        bfo = org.get("bfo") or {}
        gain = bfo.get("gainSum")
        if gain is None:
            return "none", {}  # ЮЛ найдено, но отчётности нет (молодое/на спецрежиме)
        try:
            revenue = int(gain) * 1000  # ГИР БО хранит в тыс. руб
        except (TypeError, ValueError):
            return "none", {}
        oid = org.get("id")
        return "ok", {
            "revenue": revenue,
            "year": str(bfo.get("period") or ""),
            "org": _TAG.sub("", org.get("shortName") or ""),
            "src": "girbo",
            "source_url": CARD.format(oid) if oid else "",
            "source_name": "ГИР БО ФНС",
        }
    return "none", {}


def girbo_revenue(inn, timeout=20, retries=3):
    """ИНН -> выручка из ГИР БО или {}. Прежний контракт, для существующих вызовов.

    ⚠️ Склеивает 'нет отчётности' и 'запрос не прошёл' в один пустой ответ. Для сверки
    десятка лидов это допустимо; для ОТБОРА по выручке — нет (см. girbo_revenue_ex).
    """
    status, data = girbo_revenue_ex(inn, timeout=timeout, retries=retries)
    return data if status == "ok" else {}


def add_revenue(leads, workers=2, pause=0.25, log=print):
    """Проставить лидам _revenue / _revenue_year / _revenue_src из ГИР БО по _inn.

    ГИР БО (точная выручка стр. 2110) перетирает значение из Dadata-income.
    ИП и фирмы без отчётности остаются без выручки — это нормально и помечается.
    Возвращает {'revenue_hits': n, 'revenue_total': m, 'failed': k}.

    ⚠️ ТЕМП ЗАПРОСОВ. ГИР БО жёстко троттлит: замерено на 60 ИНН — 8 потоков без пауз дали
    выручку у 8 компаний и 20 отказов, 2 потока с паузой 0.25 — у 48 и НОЛЬ отказов. Отказ
    неотличим от «нет отчётности», поэтому лишний параллелизм не ускоряет, а МОЛЧА ТЕРЯЕТ
    лиды. Отсюда дефолт workers=2 (был 8). Не поднимать без замера.

    Отказы НЕ приравниваются к «выручки нет»: они ретраятся последовательно, а то, что не
    вылечилось, помечается _revenue_error и попадает в лог. Молчать об этом нельзя — на
    выручке строится отбор.
    """
    todo = [l for l in leads if l.get("_inn")]
    if not todo:
        log("=== Выручка (ГИР БО): нет лидов с ИНН — пропуск ===")
        return {"revenue_hits": 0, "revenue_total": 0, "failed": 0}
    log(f"=== Выручка (ГИР БО): запрашиваем по {len(todo)} ИНН "
        f"({workers} потока, пауза {pause}s) ===")

    def _fetch(l):
        time.sleep(pause)                       # темп: без него ГИР БО начинает отказывать
        return l, girbo_revenue_ex(l["_inn"])

    hit, failed = 0, []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(_fetch, todo))

    def _apply(l, res):
        l["_revenue"] = res["revenue"]
        l["_revenue_year"] = res["year"]
        l["_revenue_src"] = "girbo"
        l["_revenue_source_url"] = res.get("source_url", "")
        l["_revenue_source_name"] = res.get("source_name", "ГИР БО ФНС")

    for l, (status, res) in results:
        if status == "ok":
            _apply(l, res)
            hit += 1
        elif status == "fail":
            failed.append(l)
        elif l.get("_revenue") and not l.get("_revenue_src"):
            l["_revenue_src"] = "dadata"  # ранее подхвачено из Dadata income
            l.setdefault("_revenue_source_name", "Dadata (ФНС, открытые данные)")

    if failed:   # добор последовательно и медленно — троттлинг лечится темпом, а не силой
        log(f"    [!] отказов ГИР БО: {len(failed)} — повтор по одному")
        still = []
        for l in failed:
            time.sleep(0.5)
            status, res = girbo_revenue_ex(l["_inn"])
            if status == "ok":
                _apply(l, res)
                hit += 1
            elif status == "fail":
                l["_revenue_error"] = res.get("error", "girbo unavailable")
                still.append(l)
        if still:
            log(f"    [!!] ВЫРУЧКА НЕ ПОЛУЧЕНА у {len(still)} компаний (сеть/лимит ГИР БО). "
                f"Это НЕ «выручки нет» — их нельзя молча отсеивать: {', '.join(x['_inn'] for x in still[:8])}"
                + ("..." if len(still) > 8 else ""))
        failed = still
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
        f"(ИП и фирмы без отчётности — пусто, это норма)"
        + (f"; НЕ ПОЛУЧЕНА из-за сбоя у {len(failed)} — разбирать вручную" if failed else "")
        + " ===")
    return {"revenue_hits": hit, "revenue_total": len(todo), "failed": len(failed)}


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
