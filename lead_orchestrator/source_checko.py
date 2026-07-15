# -*- coding: utf-8 -*-
r"""
Источник лидов Checko API — замена RusProfile БЕЗ браузера и без антибота.

Drop-in для source_rusprofile.harvest(): та же сигнатура, та же форма лида, тот же
контракт «вернуть список лидов с выручкой >= порога». Отличие — под капотом нет ни
Chrome, ни undetected-chromedriver, ни Cloudflare: обычные HTTPS-запросы.

КОНТРАКТ API (сверен вживую 2026-07-14, не по документации):
  GET https://api.checko.ru/v2/search
      ?key=<K>&by=okved&obj=org&query=<ОКВЭД>&region=<код>&active=true&limit=100&page=N
  -> {"data": {"ЗапВсего": 426, "СтрВсего": 5, "СтрТекущ": 1, "Записи": [...]},
      "meta": {"status":"ok","today_request_count":N,"balance":0.0}}
  Запись: ИНН, ОГРН, КПП, НаимСокр, НаимПолн, ДатаРег, Статус, РегионКод, ЮрАдрес,
          ОКВЭД (ОПИСАНИЕ, не код!), Руковод[{ФИО, ИНН, НаимДолжн}], Учред{...}

ЧЕГО В /search НЕТ (и это определяет всю конструкцию):
  - ВЫРУЧКИ. Фильтровать по ней на стороне Checko НЕЛЬЗЯ. Поэтому: перечисляем
    кандидатов по ОКВЭД+регион -> добираем выручку из ГИР БО ФНС (revenue_enrich,
    бесплатно и без лимита) -> фильтруем ПОРОГОМ В КОДЕ. Это не «медленнее», это
    единственный честный способ: выручка — факт из отчётности, её берут запросом.
  - КОНТАКТОВ (тел/email/сайт). Их добирает checko_enrich.checko_contacts_pass()
    по ИНН через /company — как и раньше. Здесь мы их не трогаем.

⚠️ ОКВЭД МАТЧИТСЯ ТОЧНО, ПРЕФИКСЫ НЕ РАБОТАЮТ (проверено):
    query=42.11 -> 426 компаний | query=42.1 -> 0 | query=42 -> 0
  Это не ограничение Checko: по правилам регистрации основной ОКВЭД указывается
  не короче ЧЕТЫРЁХ знаков (NN.NN), кодов уровня «42» или «42.1» в реестре просто нет.
  Значит коды короче NN.NN бесполезны — модуль их ОТБРАСЫВАЕТ И ГРОМКО ОБ ЭТОМ ГОВОРИТ,
  а не молча возвращает пустоту. Отрасли, заданные уровнем NN.N (agriculture, mining,
  manufacturing, trade, transport, ict, finance, realestate, science, admin,
  government, hospitality), через Checko работать НЕ БУДУТ, пока их коды не разложены
  до NN.NN. Полностью готовы: processing (51/51), opk (9/9). Частично: construction
  (8/20), energy (8/12), water (2/8) — рабочих кодов хватает, короткие отбрасываются.

⚠️ ИП: obj=org возвращает ТОЛЬКО юрлица. Значит ИП в выборку не попадают в принципе,
  и ограничение ГИР БО («ИП бухотчётность не сдают -> выручки нет») здесь не срабатывает.

СТОИМОСТЬ В ЗАПРОСАХ: одна страница = 1 запрос Checko (100 записей). Бесплатный тариф —
100 запросов/сутки, и его НЕ ХВАТИТ: только перечисление отрасли по региону — единицы
запросов, но добор контактов через /company — это ещё по запросу на компанию.
Модуль считает израсходованное и печатает итог.

CLI:
    py source_checko.py --industries construction --min-revenue 1e9 --region Татарстан
    py source_checko.py --okved 42.11,41.20 --min-revenue 5e8 --region 16 --out leads.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Справочник подклассов ОКВЭД-2 — чтобы разворачивать коды отраслей уровня NN / NN.N в
# реальные NN.NN (Checko матчит точно, префиксы не работают). Фолбэк на пустой набор:
# без справочника поведение прежнее (короткие коды просто отбрасываются).
try:
    from okved2_codes import OKVED2_SUBCLASSES
except Exception:                            # noqa: BLE001
    OKVED2_SUBCLASSES = frozenset()

BASE = "https://api.checko.ru/v2"
PAGE_LIMIT = 100          # максимум, который отдаёт Checko за один запрос
MAX_PAGES_HARD = 50       # предохранитель: 50 стр = 5000 компаний на код = 50 запросов


# --- Коды субъектов РФ: Checko фильтрует регион ЧИСЛОВЫМ кодом, а CLI принимает текст ---
REGION_CODES = {
    "01": "Адыгея", "02": "Башкортостан", "03": "Бурятия", "04": "Алтай Республика",
    "05": "Дагестан", "06": "Ингушетия", "07": "Кабардино-Балкарская",
    "08": "Калмыкия", "09": "Карачаево-Черкесская", "10": "Карелия", "11": "Коми",
    "12": "Марий Эл", "13": "Мордовия", "14": "Саха Якутия",
    "15": "Северная Осетия Алания", "16": "Татарстан", "17": "Тыва",
    "18": "Удмуртская", "19": "Хакасия", "20": "Чеченская", "21": "Чувашская",
    "22": "Алтайский край", "23": "Краснодарский край", "24": "Красноярский край",
    "25": "Приморский край", "26": "Ставропольский край", "27": "Хабаровский край",
    "28": "Амурская область", "29": "Архангельская область", "30": "Астраханская область",
    "31": "Белгородская область", "32": "Брянская область", "33": "Владимирская область",
    "34": "Волгоградская область", "35": "Вологодская область", "36": "Воронежская область",
    "37": "Ивановская область", "38": "Иркутская область", "39": "Калининградская область",
    "40": "Калужская область", "41": "Камчатский край", "42": "Кемеровская область",
    "43": "Кировская область", "44": "Костромская область", "45": "Курганская область",
    "46": "Курская область", "47": "Ленинградская область", "48": "Липецкая область",
    "49": "Магаданская область", "50": "Московская область", "51": "Мурманская область",
    "52": "Нижегородская область", "53": "Новгородская область", "54": "Новосибирская область",
    "55": "Омская область", "56": "Оренбургская область", "57": "Орловская область",
    "58": "Пензенская область", "59": "Пермский край", "60": "Псковская область",
    "61": "Ростовская область", "62": "Рязанская область", "63": "Самарская область",
    "64": "Саратовская область", "65": "Сахалинская область", "66": "Свердловская область",
    "67": "Смоленская область", "68": "Тамбовская область", "69": "Тверская область",
    "70": "Томская область", "71": "Тульская область", "72": "Тюменская область",
    "73": "Ульяновская область", "74": "Челябинская область", "75": "Забайкальский край",
    "76": "Ярославская область", "77": "Москва", "78": "Санкт-Петербург",
    "79": "Еврейская автономная область", "83": "Ненецкий автономный округ",
    "86": "Ханты-Мансийский автономный округ Югра", "87": "Чукотский автономный округ",
    "89": "Ямало-Ненецкий автономный округ", "91": "Крым", "92": "Севастополь",
}


class CheckoSourceError(RuntimeError):
    pass


def log(m):
    print(m, flush=True)


def _digits(s):
    return re.sub(r"\D", "", str(s or ""))


def _okved_children(prefix):
    """NN / NN.N -> список реальных подклассов NN.NN из справочника ОКВЭД-2."""
    p = str(prefix).strip()
    if re.fullmatch(r"\d{2}", p):            # класс NN -> все NN.xx
        return sorted(c for c in OKVED2_SUBCLASSES if c[:2] == p)
    if re.fullmatch(r"\d{2}\.\d", p):        # группа NN.N -> NN.Nx
        return sorted(c for c in OKVED2_SUBCLASSES if c.startswith(p))
    return []


def usable_okved(codes):
    """Коды отрасли -> (пригодные_для_Checko, реально_бесполезные).

    NN.NN и глубже берём как есть. NN и NN.N РАЗВОРАЧИВАЕМ в реальные подклассы NN.NN по
    справочнику ОКВЭД-2 (okved2_codes) — Checko матчит основной ОКВЭД точно, префиксы не
    работают, а короче 4 знаков основной ОКВЭД в реестре не встречается. В coarse попадают
    только коды, у которых в справочнике нет подклассов (почти не бывает, либо нет справочника).
    """
    ok, coarse = [], []
    for c in codes:
        if len(_digits(c)) >= 4:
            ok.append(c)
            continue
        kids = _okved_children(c)
        (ok.extend(kids) if kids else coarse.append(c))
    return list(dict.fromkeys(ok)), coarse


def resolve_region(region):
    """Текст региона -> (include_codes, exclude_names).

    Checko умеет фильтровать регион на своей стороне ТОЛЬКО по числовому коду, поэтому:
      'Татарстан' / '16' / '16,02'  -> include -> фильтр отдаёт СЕРВЕР (дёшево);
      'НЕ Москва'                   -> exclude -> сервер не поможет, фильтруем КЛИЕНТСКИ,
                                       а значит перечисляем всю страну (дорого по запросам).
    """
    if not region:
        return None, None
    # Алиасы аббревиатур (ХМАО/ЯНАО/СПб/МСK…) — ЕДИНЫЙ источник с RusProfile-путём.
    # Ленивый импорт с фолбэком: в минимальном образе source_rusprofile может не встать.
    try:
        from source_rusprofile import region_patterns
    except Exception:                                   # noqa: BLE001
        region_patterns = lambda s: [str(s or "").strip().lower()]  # noqa: E731

    # Негатив разбираем ПО КАЖДОМУ элементу списка, а не по всей строке: иначе
    # «Дагестан, не Москва» трактовалось как ВКЛючить и Дагестан, И Москву (список
    # начинался не с «не», а внутри «не Москва» подстрокой матчилась Москва как include).
    _NEG = r"^\s*(НЕ|не|NOT|not|КРОМЕ|кроме|!|-)\s*"
    inc_codes, exc_names = [], []          # include -> коды (серверный фильтр); exclude -> имена (клиентский)
    for part in re.split(r"[,;]", str(region)):
        part = part.strip()
        if not part:
            continue
        neg = bool(re.match(_NEG, part))
        part = re.sub(_NEG, "", part).strip()
        if not part:
            continue
        if part.isdigit():
            hits = [part.zfill(2)]
        else:
            hits = []                       # аббревиатура -> подстроки полного имени субъекта
            for pat in (region_patterns(part) or []):
                p = pat.replace("ё", "е")
                for c, n in REGION_CODES.items():
                    nl = n.lower().replace("ё", "е")
                    if p in nl or nl in p:
                        hits.append(c)
            hits = list(dict.fromkeys(hits))            # дедуп с сохранением порядка
        if not hits:
            log(f"[!] регион «{part}» не опознан — фильтр по нему применён НЕ БУДЕТ")
            continue
        if neg:
            exc_names.extend(REGION_CODES.get(c, c) for c in hits)
        else:
            inc_codes.extend(hits)
    return (inc_codes or None), (exc_names or None)


def region_name(code):
    return REGION_CODES.get(str(code).zfill(2), str(code or ""))


class CheckoSearch:
    """Тонкий клиент /search. Свой, а не CheckoClient из checko_enrich: тот заточен под
    /company и не считает страницы. Ошибки/429/лимит обрабатываем здесь же."""

    def __init__(self, token=None, pause=0.25, retries=3):
        from checko_enrich import checko_keys        # единый источник ключей (+ запасные)
        self.keys = [token.strip()] if token else checko_keys()
        if not self.keys:
            raise CheckoSourceError("Не задан CHECKO_TOKEN (env или token=...)")
        self._ki = 0                                 # индекс текущего ключа (запоминаем рабочий)
        self.pause = pause
        self.retries = retries
        self.requests_used = 0

    @property
    def token(self):
        return self.keys[self._ki]

    def _get(self, params):
        tried = 0
        while tried < len(self.keys):
            url = f"{BASE}/search?" + urllib.parse.urlencode(dict(params, key=self.token))
            for attempt in range(self.retries):
                try:
                    req = urllib.request.Request(url, headers={"Accept": "application/json"})
                    with urllib.request.urlopen(req, timeout=25) as r:
                        self.requests_used += 1
                        time.sleep(self.pause)
                        return json.loads(r.read().decode("utf-8"))
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        return {}
                    if e.code == 429:               # троттл/лимит текущего ключа — подождать и повторить
                        time.sleep(2.0 * (attempt + 1)); continue
                    if e.code in (401, 403):        # ключ невалиден/исчерпан — на следующий
                        break
                    raise CheckoSourceError(f"Checko HTTP {e.code}: {e.read()[:200]}")
                except urllib.error.URLError:
                    time.sleep(1.0 * (attempt + 1))
            tried += 1                               # текущий ключ не сработал -> следующий
            if tried < len(self.keys):
                self._ki = (self._ki + 1) % len(self.keys)
                log(f"[checko] ключ исчерпан/невалиден — переключаюсь на запасной #{self._ki + 1}")
        raise CheckoSourceError(
            "Checko: все ключи исчерпаны/недоступны — лимит (100/сут на бесплатном тарифе) или "
            "неверный ключ. Добавь запасной ключ в CHECKO_TOKEN_ALT.")

    def by_okved(self, okved, region_code=None, max_pages=MAX_PAGES_HARD, active=True):
        """Все юрлица с ОСНОВНЫМ ОКВЭД=okved (опц. в регионе). Генератор записей."""
        params = {"by": "okved", "obj": "org", "query": okved, "limit": PAGE_LIMIT}
        if region_code:
            params["region"] = region_code
        if active:
            params["active"] = "true"

        page, total_pages = 1, 1
        while page <= min(total_pages, max_pages, MAX_PAGES_HARD):
            data = (self._get(dict(params, page=page)) or {}).get("data") or {}
            recs = data.get("Записи") or []
            if page == 1:
                total_pages = int(data.get("СтрВсего") or 0)
                found = int(data.get("ЗапВсего") or 0)
                if found == 0:
                    return
                if total_pages > max_pages:
                    log(f"    [!] ОКВЭД {okved}: найдено {found} (страниц {total_pages}), "
                        f"берём первые {max_pages} стр. — остальное НЕ просмотрено")
            for rec in recs:
                yield rec
            if not recs:
                return
            page += 1


def item_to_lead(rec, cfg, industry, okved):
    """Запись Checko -> лид в форме, которую ждёт весь пайплайн (см. source_rusprofile.item_to_lead).

    Выручку НЕ ставим — её проставит revenue_enrich.add_revenue() из ГИР БО, он же
    заполнит _revenue_src / _revenue_source_url. Контакты пустые — их добирает
    checko_enrich.checko_contacts_pass() по ИНН.
    """
    ruk = rec.get("Руковод")
    if isinstance(ruk, list):
        ruk = ruk[0] if ruk else {}
    ceo = (ruk or {}).get("ФИО", "") if isinstance(ruk, dict) else ""
    ogrn = str(rec.get("ОГРН") or "")
    label = cfg["label"]
    return {
        "name": rec.get("НаимСокр") or rec.get("НаимПолн") or "",
        "niche": f"{label} (ОКВЭД {okved})",
        "website": "", "phone": "", "email": "",
        "contact_person": ceo,
        "source": "Checko API (/search by okved)",
        "pain": cfg["pain"], "offer": cfg["offer"], "status": "", "next_step": "",
        "_inn": _digits(rec.get("ИНН")),
        "_ogrn": ogrn,
        "_address": rec.get("ЮрАдрес") or "",
        "_region": region_name(rec.get("РегионКод")),
        "_region_code": str(rec.get("РегионКод") or ""),
        "_okved": okved,
        "_okved_descr": rec.get("ОКВЭД") or "",
        "_status": rec.get("Статус") or "",
        "_revenue": None,          # <- ГИР БО
        "_industry": industry,
        # Источник-ссылка на карточку (пользователь требует первоисточник в выгрузках).
        "_checko_url": f"https://checko.ru/company/{ogrn}" if ogrn else "",
    }


def harvest(industries, min_revenue=1e9, per_industry=40, region=None,
            headless=False, out_path=None, exclude_regions=None, offscreen=False,
            max_candidates=3000, client=None):
    """Drop-in замена source_rusprofile.harvest().

    headless/offscreen приняты РАДИ СОВМЕСТИМОСТИ сигнатуры и игнорируются: браузера здесь нет.

    Порядок (важен для цены): сначала дёшево сузить (ОКВЭД + регион на стороне Checko),
    и только потом дорого проверять выручку по каждому ИНН в ГИР БО.
    """
    from revenue_enrich import add_revenue          # ГИР БО: бесплатно, без лимита
    try:
        from source_rusprofile import INDUSTRY      # конфиг отраслей — единственный источник
    except ImportError as e:                        # selenium/uc не стоят (минимальный образ)
        raise CheckoSourceError(
            "не импортируется source_rusprofile.INDUSTRY (конфиг отраслей): "
            f"{e}. Передай коды через --okved.") from e

    cli = client or CheckoSearch()
    inc_codes, exc_names = resolve_region(region)
    if exclude_regions:
        exc_names = (exc_names or []) + list(exclude_regions)

    if exc_names and not inc_codes:
        log(f"[!] регион задан ИСКЛЮЧЕНИЕМ ({', '.join(exc_names)}) — Checko так фильтровать "
            f"не умеет. Перечисляем всю страну и отсеиваем клиентски: запросов уйдёт МНОГО.")

    by_inn, coarse_all = {}, []
    for ind in industries:
        cfg = INDUSTRY[ind]
        codes, coarse = usable_okved(cfg["okved"])
        coarse_all += coarse
        if coarse:
            log(f"[!] {ind}: {len(coarse)} код(ов) короче NN.NN отброшены "
                f"({', '.join(coarse[:8])}{'...' if len(coarse) > 8 else ''}) — "
                f"в реестре таких основных ОКВЭД нет, Checko вернёт 0")
        if not codes:
            log(f"[!!] {ind}: НЕ ОСТАЛОСЬ ни одного пригодного кода — отрасль пропущена. "
                f"Разложи ОКВЭД до уровня NN.NN.")
            continue

        log(f"\n=== {ind}: {cfg['label']} | ОКВЭД {len(codes)} шт"
            + (f" | регион {', '.join(region_name(c) for c in inc_codes)}" if inc_codes else "")
            + " ===")
        before = len(by_inn)
        for okved in codes:
            for rc in (inc_codes or [None]):
                for rec in cli.by_okved(okved, region_code=rc):
                    inn = _digits(rec.get("ИНН"))
                    if not inn or inn in by_inn:
                        continue
                    if (rec.get("Статус") or "") != "Действует":
                        continue
                    lead = item_to_lead(rec, cfg, ind, okved)
                    if exc_names and any(
                            e.lower()[:12] in (lead["_region"] or "").lower()
                            for e in exc_names):
                        continue
                    by_inn[inn] = lead
                    if len(by_inn) >= max_candidates:
                        log(f"    [!] достигнут потолок кандидатов ({max_candidates}) — "
                            f"перечисление остановлено (подними max_candidates или сузь регион)")
                        break
                if len(by_inn) >= max_candidates:
                    break
            if len(by_inn) >= max_candidates:
                break
        log(f"    кандидатов по отрасли: +{len(by_inn) - before}")

    leads = list(by_inn.values())
    log(f"\n=== Кандидатов всего: {len(leads)} | запросов к Checko: {cli.requests_used} ===")
    if not leads:
        if coarse_all:
            raise CheckoSourceError(
                "Checko не вернул ни одной компании. Вероятная причина — коды ОКВЭД короче "
                "NN.NN (см. предупреждения выше): по ним в реестре никто не зарегистрирован.")
        return []

    # ВЫРУЧКА: единственный источник — ГИР БО ФНС (официально, бесплатно, без лимита).
    # Модели её не отдаём: это факт из отчётности (стр. 2110), а не предмет суждения.
    # Темп запросов задаёт add_revenue (2 потока с паузой) — ГИР БО троттлит, и отказ там
    # неотличим от «выручки нет»; выжимать параллелизм здесь значит терять лиды.
    add_revenue(leads, log=log)

    kept, no_rev, broken = [], [], []
    for l in leads:
        if l.get("_revenue_error"):
            broken.append(l)          # ЗАПРОС НЕ ПРОШЁЛ — это НЕ «выручки нет»
            continue
        if not l.get("_revenue"):
            no_rev.append(l)          # честно нет отчётности (молодое ЮЛ / спецрежим / ОПК)
            continue
        if l["_revenue"] >= min_revenue:
            kept.append(l)
    kept.sort(key=lambda l: -(l.get("_revenue") or 0))

    log(f"=== Порог >{min_revenue / 1e9:g} млрд: прошли {len(kept)} из {len(leads)} "
        f"(нет отчётности в ГИР БО: {len(no_rev)}) ===")
    if broken:
        # Молчать нельзя: это лиды с НЕИЗВЕСТНОЙ выручкой, а не отсеянные по порогу.
        log(f"[!!] У {len(broken)} компаний выручку получить НЕ УДАЛОСЬ (сбой ГИР БО). "
            f"Они НЕ отсеяны по порогу — они не проверены. Прогони источник ещё раз "
            f"или добери их точечно: py revenue_enrich.py <ИНН>")

    # квота на отрасль — как в RusProfile-версии
    out, per = [], {}
    for l in kept:
        i = l.get("_industry")
        if per.get(i, 0) >= per_industry:
            continue
        per[i] = per.get(i, 0) + 1
        out.append(l)

    if out_path:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        log(f"=== Сохранено: {out_path} ({len(out)} лидов) ===")
    return out


def _cli(argv):
    ap = argparse.ArgumentParser(description="Лиды из Checko API (без браузера) + выручка из ГИР БО")
    ap.add_argument("--industries", default="", help="ключи INDUSTRY через запятую")
    ap.add_argument("--okved", default="", help="конкретные коды NN.NN через запятую (вместо отрасли)")
    ap.add_argument("--min-revenue", type=float, default=1e9)
    ap.add_argument("--per-industry", type=int, default=40)
    ap.add_argument("--region", default=None, help="'Татарстан' | '16' | '16,02' | 'НЕ Москва'")
    ap.add_argument("--max-candidates", type=int, default=3000)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)

    if a.okved:
        from source_rusprofile import INDUSTRY
        codes = [c.strip() for c in a.okved.split(",") if c.strip()]
        ok, coarse = usable_okved(codes)
        if coarse:
            log(f"[!] отброшены короткие коды: {', '.join(coarse)}")
        if not ok:
            raise SystemExit("не осталось пригодных кодов (нужен уровень NN.NN)")
        # временная псевдо-отрасль на переданных кодах
        base = next(iter(INDUSTRY.values()))
        INDUSTRY["_adhoc"] = {"label": "Произвольные ОКВЭД", "okved": ok,
                              "pain": base.get("pain", ""), "offer": base.get("offer", "")}
        inds = ["_adhoc"]
    else:
        inds = [s.strip() for s in a.industries.split(",") if s.strip()]
        if not inds:
            raise SystemExit("укажи --industries или --okved")

    leads = harvest(inds, min_revenue=a.min_revenue, per_industry=a.per_industry,
                    region=a.region, out_path=a.out, max_candidates=a.max_candidates)
    for l in leads[:20]:
        rev = (l.get("_revenue") or 0) / 1e9
        log(f"  {rev:6.2f} млрд  {l['_inn']:<12} {l['name'][:48]:<50} {l['_region']}")
    log(f"\nИТОГО: {len(leads)} лидов")
    return 0 if leads else 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
