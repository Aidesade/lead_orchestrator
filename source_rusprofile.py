# -*- coding: utf-8 -*-
r"""
Источник лидов RusProfile — расширенный поиск с фильтром по ОКВЭД + ВЫРУЧКЕ.

Антибот RusProfile (Cloudflare) проходится undetected_chromedriver (проверено
2026-06-16; List-Org для сравнения — IP-бан, RusProfile же нас по IP не банит).
Выдача рендерится Vue через внутренний API:

  POST https://www.rusprofile.ru/ajax/search/advanced?cacheKey=<rand>
  заголовок X-Csrf-Token = cookie __Host-csrf-token (ротируется per-load)
  тело: {"action":"search_advanced","query":"","state_1..5":bool,"okved_strict":true,
         "okved":["41.20",...],"finance_revenue_from":"1000000000","page":1}
  ответ: data.items[] c {inn,name,region,address,ceo_name,main_okved_id,okved_descr,
         finance_revenue,authorized_capital,reg_date,okpo,link}, data.total_count,
         data.pagination{per_page_limit:50, page_count<=20}.

Зовём API ИЗНУТРИ страницы (execute_script + синхронный XHR) — same-origin, cookie
Cloudflare и CSRF уже на месте. Фильтр по выручке и ОКВЭД делает сервер -> сразу
крупный бизнес нужной отрасли с ИНН и выручкой.

CLI:
  py source_rusprofile.py --industries construction,energy,processing --min-revenue 1e9 \
        --per-industry 40 --out D:\лиды\_rp.json
  py source_rusprofile.py --okved 27.10,24.10 --min-revenue 1e9 --max-pages 10
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import undetected_chromedriver as uc

from browser_util import chrome_major  # version_main под реально установленный Chrome

uc.Chrome.__del__ = lambda self: None

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

ADV_URL = "https://www.rusprofile.ru/search-advanced"

# ОКВЭД-2 коды по отраслям (RusProfile использует текущий ОКВЭД-2014).
# Боль/оффер — те же, что в build_bigleads + ОПК.
INDUSTRY = {
    "energy": {
        "label": "Производство и распределение электроэнергии, газа и воды",
        "okved": ["35", "35.1", "35.11", "35.12", "35.13", "35.14",
                  "35.2", "35.21", "35.22", "35.23", "35.3", "35.30"],
        "pain": "Потери в сетях, аварийность оборудования, прогноз нагрузки, ручные обходы",
        "offer": "Прогноз нагрузки/аварий, ИИ-анализ обходов (дроны/CV), оптимизация режимов, аналитика сбыта",
    },
    "water": {
        "label": "Водоснабжение и водоотведение (ЖКХ)",
        "okved": ["36", "36.00", "37", "37.00", "38", "38.1", "38.2", "39"],
        "pain": "Потери воды, аварийность сетей, биллинг, ручной учёт",
        "offer": "ИИ-мониторинг сетей и утечек, прогноз аварий, оптимизация и биллинг-аналитика",
    },
    "construction": {
        "label": "Строительство",
        "okved": ["41", "41.1", "41.2", "41.20", "42", "42.1", "42.11", "42.12",
                  "42.13", "42.2", "42.21", "42.22", "42.9", "42.91", "42.99",
                  "43", "43.1", "43.2", "43.3", "43.9"],
        "pain": "Срыв сроков и смет, контроль работ на площадке, объёмы и закупки",
        "offer": "ИИ-контроль работ по фото/видео, авто-проверка смет и КС-2/3, прогноз сроков, аналитика тендеров",
    },
    "processing": {
        "label": "Промышленная переработка (нефтехимия, металлургия, химия, ЛПК, пищевая)",
        # ОКВЭД матчится ТОЧНО по main_okved_id -> перечисляем КОНКРЕТНЫЕ подкоды
        "okved": [
            "19.20",                                              # нефтепродукты
            "20.11", "20.13", "20.14", "20.15", "20.16", "20.17", "20.60",  # химия
            "21.10", "21.20", "22.11", "22.21",                   # фарма/резина/пластик
            "23.11", "23.51", "23.61", "23.99",                   # стекло/цемент/бетон
            "24.10", "24.20", "24.31", "24.32", "24.33", "24.34", # чёрная металлургия/трубы
            "24.41", "24.42", "24.43", "24.44", "24.45",          # цветная металлургия
            "25.11", "25.21", "25.40", "25.62",                   # металлоизделия
            "17.11", "17.12", "17.21", "17.22", "17.24", "17.29", # ЦБП
            "16.10", "16.21", "16.23", "16.24", "16.29",          # деревообработка
            "10.11", "10.41", "10.51", "10.61", "10.71", "10.81", "11.07",  # пищевая
            "28.92", "28.99",                                     # тяжёлое машиностроение
        ],
        "pain": "Незапланированные простои, контроль качества, энергоёмкость, планирование",
        "offer": "Предиктивное ТОиР, оптимизация режимов и энергопотребления, ИИ-контроль качества, спрос-планирование",
    },
    "opk": {
        "label": "ОПК / ВПК",
        "okved": ["25.40", "30.30", "30.11", "30.12", "30.40", "20.51",
                  "26.30", "26.51", "28.99"],
        "pain": "Гособоронзаказ: ручной КД/ТД-документооборот, контроль качества/дефектоскопия, импортозамещение ПО",
        "offer": "ИИ-контроль качества (CV-дефектоскопия), автоматизация КД/ТД и приёмки, предиктивное ТОиР",
    },
    # ── Разделы ОКВЭД-2 (добавлено 2026-06-23; okved = уровень групп NN.N,
    #    строгий матч префиксно ловит подгруппы/виды; источник кодов — рубрикатор RusProfile) ──
    "agriculture": {
        "label": "Сельское, лесное хозяйство, охота, рыболовство",
        "okved": ["01.1", "01.2", "01.3", "01.4", "01.5", "01.6", "01.7", "02.1", "02.2", "02.3", "02.4", "03.1", "03.2"],
        "pain": "Прогноз урожайности и потерь, контроль полей и техники, ручной учёт, логистика хранения",
        "offer": "ИИ-агроскаутинг (снимки/дроны), прогноз урожая и болезней, мониторинг техники, оптимизация хранения и логистики",
    },
    "mining": {
        "label": "Добыча полезных ископаемых (уголь, нефть, газ, руды)",
        "okved": ["05.1", "05.2", "06.1", "06.2", "07.1", "07.2", "08.1", "08.9", "09.1", "09.9"],
        "pain": "Аварийность и простои оборудования, безопасность, геологоразведка, энергоёмкость",
        "offer": "Предиктивное ТОиР, CV-контроль безопасности, ИИ-анализ геоданных, оптимизация добычи и логистики",
    },
    "manufacturing": {
        "label": "Обрабатывающие производства",
        "okved": ["10.1", "10.2", "10.3", "10.4", "10.5", "10.6", "10.7", "10.8", "10.9", "11.0", "12.0", "13.1", "13.2", "13.3", "13.9", "14.1", "14.2", "14.3", "15.1", "15.2", "16.1", "16.2", "17.1", "17.2", "18.1", "18.2", "19.1", "19.2", "19.3", "20.1", "20.2", "20.3", "20.4", "20.5", "20.6", "21.1", "21.2", "22.1", "22.2", "23.1", "23.2", "23.3", "23.4", "23.5", "23.6", "23.7", "23.9", "24.1", "24.2", "24.3", "24.4", "24.5", "25.1", "25.2", "25.3", "25.4", "25.5", "25.6", "25.7", "25.9", "26.1", "26.2", "26.3", "26.4", "26.5", "26.6", "26.7", "26.8", "27.1", "27.2", "27.3", "27.4", "27.5", "27.9", "28.1", "28.2", "28.3", "28.4", "28.9", "29.1", "29.2", "29.3", "30.1", "30.2", "30.3", "30.4", "30.9", "31.0", "32.1", "32.2", "32.3", "32.4", "32.5", "32.9", "33.1", "33.2"],
        "pain": "Незапланированные простои, контроль качества, планирование, энергозатраты",
        "offer": "Предиктивное ТОиР, CV-контроль качества, спрос-планирование, оптимизация режимов и энергопотребления",
    },
    "trade": {
        "label": "Торговля оптовая и розничная; ремонт автотранспорта",
        "okved": ["45.1", "45.2", "45.3", "45.4", "46.1", "46.2", "46.3", "46.4", "46.5", "46.6", "46.7", "46.9", "47.1", "47.2", "47.3", "47.4", "47.5", "47.6", "47.7", "47.8", "47.9"],
        "pain": "Прогноз спроса, неликвиды и out-of-stock, ценообразование, отток клиентов",
        "offer": "ИИ-прогноз спроса и автозаказ, динамическое ценообразование, рекомендации, аналитика чеков и оттока",
    },
    "transport": {
        "label": "Транспортировка и хранение",
        "okved": ["49.1", "49.2", "49.3", "49.4", "49.5", "50.1", "50.2", "50.3", "50.4", "51.1", "51.2", "52.1", "52.2", "53.1", "53.2"],
        "pain": "Пустые пробеги, простои, маршрутизация, состояние парка",
        "offer": "ИИ-маршрутизация и загрузка, предиктивное ТОиР парка, прогноз ETA, оптимизация склада",
    },
    "hospitality": {
        "label": "Гостиницы и общественное питание",
        "okved": ["55.1", "55.2", "55.3", "55.9", "56.1", "56.2", "56.3"],
        "pain": "Колебания загрузки, списания продуктов, персонал, отзывы",
        "offer": "Прогноз загрузки и закупок, динамические цены, ИИ-анализ отзывов, автоматизация бронирования",
    },
    "ict": {
        "label": "Информация и связь (ИТ, СМИ, телеком)",
        "okved": ["58.1", "58.2", "59.1", "59.2", "60.1", "60.2", "61.1", "61.2", "61.3", "61.9", "62.0", "63.1", "63.9"],
        "pain": "Нагрузка на поддержку и отток, ручные процессы, модерация контента",
        "offer": "ИИ-ассистенты поддержки, прогноз оттока и антифрод, генерация/модерация контента, AIOps",
    },
    "finance": {
        "label": "Финансовая и страховая деятельность",
        "okved": ["64.1", "64.2", "64.3", "64.9", "65.1", "65.2", "65.3", "66.1", "66.2", "66.3"],
        "pain": "Скоринг и фрод, ручной андеррайтинг, комплаенс, отток",
        "offer": "ИИ-скоринг и антифрод, авто-андеррайтинг, аналитика комплаенса, удержание клиентов",
    },
    "realestate": {
        "label": "Операции с недвижимым имуществом",
        "okved": ["68.1", "68.2", "68.3"],
        "pain": "Оценка и ценообразование, простой площадей, обслуживание, лидогенерация",
        "offer": "ИИ-оценка и прогноз цен, прогноз спроса/вакантности, предиктивное обслуживание, лидскоринг",
    },
    "science": {
        "label": "Профессиональная, научная и техническая деятельность",
        "okved": ["69.1", "69.2", "70.1", "70.2", "71.1", "71.2", "72.1", "72.2", "73.1", "73.2", "74.1", "74.2", "74.3", "74.9", "75.0"],
        "pain": "Рутина документов и расчётов, утилизация специалистов, подготовка материалов",
        "offer": "ИИ-обработка документов, ассистенты расчётов и проектирования, авто-отчёты, генерация материалов",
    },
    "admin": {
        "label": "Административная деятельность и доп. услуги",
        "okved": ["77.1", "77.2", "77.3", "77.4", "78.1", "78.2", "78.3", "79.1", "79.9", "80.1", "80.2", "80.3", "81.1", "81.2", "81.3", "82.1", "82.2", "82.3", "82.9"],
        "pain": "Планирование персонала, графики и маршруты, контроль качества услуг",
        "offer": "ИИ-планирование смен и маршрутов, CV-контроль, прогноз спроса, автоматизация заявок",
    },
    "government": {
        "label": "Госуправление и военная безопасность; соцобеспечение",
        "okved": ["84.1", "84.2", "84.3"],
        "pain": "Документооборот, обращения граждан, контроль, аналитика",
        "offer": "ИИ-обработка обращений и документов, аналитика, ассистенты; on-prem под 152-ФЗ",
    },
    "education": {
        "label": "Образование",
        "okved": ["85.1", "85.2", "85.3", "85.4"],
        "pain": "Проверка работ, персонализация, нагрузка преподавателей, отсев",
        "offer": "ИИ-проверка и тьютор, персональные траектории, генерация материалов, прогноз отсева",
    },
    "healthcare": {
        "label": "Здравоохранение и социальные услуги",
        "okved": ["86.1", "86.2", "86.9", "87.1", "87.2", "87.3", "87.9", "88.1", "88.9"],
        "pain": "Очереди и расписание, документооборот, поддержка диагностики, отток",
        "offer": "ИИ-планирование и триаж, CV-поддержка диагностики, авто-документация; on-prem под 152-ФЗ",
    },
    "culture": {
        "label": "Культура, спорт, досуг и развлечения",
        "okved": ["90.0", "91.0", "92.1", "92.2", "93.1", "93.2"],
        "pain": "Заполняемость, продвижение, контент, удержание аудитории",
        "offer": "Прогноз посещаемости, динамические цены, ИИ-контент и рекомендации, удержание",
    },
    "services": {
        "label": "Прочие виды услуг",
        "okved": ["94.1", "94.2", "94.9", "95.1", "95.2", "96.0"],
        "pain": "Загрузка мастеров, поток заявок, ценообразование, отзывы",
        "offer": "ИИ-распределение заявок, прогноз спроса, авто-коммуникации, аналитика отзывов",
    },
}

_XHR = r"""
var body=arguments[0];
var tok=(document.cookie.match(/__Host-csrf-token=([^;]+)/)||[])[1]||'';
var xhr=new XMLHttpRequest();
xhr.open('POST','/ajax/search/advanced?cacheKey='+Math.random(),false);
xhr.setRequestHeader('Content-Type','application/json');
xhr.setRequestHeader('X-Csrf-Token', decodeURIComponent(tok));
xhr.send(JSON.stringify(body));
return xhr.responseText;
"""


def log(m):
    print(m, flush=True)


class RusProfileSession:
    def __init__(self, headless=False, offscreen=False):
        self.headless = headless
        self.offscreen = offscreen        # headed, но окно ЗА экраном (антибот проходит, окна не видно)
        self._d = None

    def __enter__(self):
        opts = uc.ChromeOptions()
        opts.page_load_strategy = "eager"
        if self.headless:
            opts.add_argument("--headless=new")
        elif self.offscreen:
            opts.add_argument("--window-position=-32000,-32000")  # окно за пределами экрана: не видно
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1280,900")
        _kw = dict(options=opts, headless=self.headless, use_subprocess=True)
        _vm = chrome_major()              # прибиваем версию uc-драйвера к Chrome
        if _vm:
            _kw["version_main"] = _vm
        self._d = uc.Chrome(**_kw)
        if self.offscreen and not self.headless:
            try:
                self._d.minimize_window()  # подстраховка: и за экраном, и свёрнуто
            except Exception:
                pass
        self._d.set_page_load_timeout(45)
        self._d.get(ADV_URL)
        time.sleep(6)  # дать Cloudflare-challenge пройти и выставить CSRF-cookie
        return self

    def __exit__(self, *exc):
        if self._d is not None:
            try:
                self._d.quit()
            except Exception:
                pass
            self._d = None

    def _post(self, body):
        txt = self._d.execute_script(_XHR, body)
        return json.loads(txt)

    def search(self, okved, revenue_from, max_pages=20, pause=1.0, log=log):
        """okved: список кодов ОКВЭД-2. revenue_from: ₽. Возвращает items[].

        РЕГИОН сервер НЕ фильтрует. Проверено живым пробником API расш. поиска:
        ключ `region` -> success=false «Некорректные входные параметры»;
        `regions`/`region_id` сервер молча игнорирует (total_count не меняется).
        Поэтому регион фильтруется КЛИЕНТСКИ по полю `region` в выдаче (она
        отдаёт полное имя субъекта) — см. region_included()/harvest().
        """
        base = {
            "action": "search_advanced", "query": "",
            "state_1": True, "state_2": False, "state_3": False,
            "state_4": False, "state_5": False, "okved_strict": True,
            "okved": list(okved), "finance_revenue_from": str(int(revenue_from)),
        }
        out, total = [], None
        for page in range(1, max_pages + 1):
            body = dict(base, page=page)
            try:
                r = self._post(body)
            except Exception as e:
                log(f"  [warn] стр.{page}: {str(e).splitlines()[0][:70]}")
                break
            if not r.get("success"):
                log(f"  [warn] стр.{page}: success=false — стоп")
                break
            data = r.get("data") or {}
            items = data.get("items") or []
            if total is None:
                total = data.get("total_count")
                pg = (data.get("pagination") or {}).get("page_count", max_pages)
                max_pages = min(max_pages, pg or max_pages)
                log(f"  всего по фильтру: {total} (страниц до {max_pages})")
            if not items:
                break
            out.extend(items)
            log(f"  стр.{page}: +{len(items)} (итого {len(out)})")
            if page >= max_pages:
                break
            time.sleep(pause)
        return out


def item_to_lead(it, cfg, industry):
    rev = it.get("finance_revenue")
    try:
        rev = int(rev) if rev not in (None, "") else None
    except (TypeError, ValueError):
        rev = None
    okved = it.get("main_okved_id") or ""
    niche = cfg["label"]
    descr = it.get("okved_descr")
    return {
        "name": it.get("name") or it.get("raw_name") or "",
        "niche": f"{niche} (ОКВЭД {okved})" if okved else niche,
        "website": "", "phone": "", "email": "",
        "contact_person": it.get("ceo_name") or "",
        "source": "RusProfile (расш. поиск)",
        "pain": cfg["pain"], "offer": cfg["offer"], "status": "", "next_step": "",
        "_inn": (it.get("inn") or "").strip(),
        "_ogrn": it.get("ogrn") or "",
        "_address": it.get("address") or "",
        "_region": it.get("region") or "",
        "_okved_descr": descr or "",
        "_revenue": rev,
        "_revenue_src": "rusprofile",
        "_revenue_source_name": "RusProfile",
        "_revenue_source_url": "https://www.rusprofile.ru" + (it.get("link") or ""),
        "_rusprofile_url": "https://www.rusprofile.ru" + (it.get("link") or ""),
        "_industry": industry,
    }


# Аббревиатуры регионов -> подстроки полного имени субъекта в выдаче RusProfile
# (выдача отдаёт, напр., "Ханты-Мансийский автономный округ - Югра").
REGION_ALIASES = {
    "хмао": ["ханты-мансийск", "югра"],
    "югра": ["ханты-мансийск", "югра"],
    "ханты-мансийск": ["ханты-мансийск", "югра"],
    "ханты": ["ханты-мансийск", "югра"],
    "янао": ["ямало-ненецк"],
    "ямал": ["ямало-ненецк"],
    "ненецкий ао": ["ненецкий автономный"],
    "нао": ["ненецкий автономный"],
    "спб": ["санкт-петербург"],
    "питер": ["санкт-петербург"],
    "петербург": ["санкт-петербург"],
    "мск": ["москва"],
}


def region_patterns(query):
    """Свободный текст региона -> список подстрок (lower) для матча по выдаче.
    Аббревиатуры (ХМАО/ЯНАО/СПб) разворачиваются по REGION_ALIASES; иначе берём
    сам текст подстрокой — выдача содержит ПОЛНОЕ имя субъекта, а пользовательское
    «Татарстан»/«Ханты-Мансийск»/«Свердловск» является его подстрокой.
    Возвращает None, если регион не задан (= фильтра нет, вся РФ)."""
    q = (query or "").strip().lower()
    if not q:
        return None
    if q in REGION_ALIASES:
        return REGION_ALIASES[q]
    for key, pats in REGION_ALIASES.items():
        if key in q:
            return pats
    return [q]


def region_included(region, patterns):
    """True, если регион подходит под включающий фильтр (или фильтра нет).
    NB: «москва» матчит и город, и «Московскую область» — разграничь точнее,
    если важно (для области используй полное «московская область»)."""
    if not patterns:
        return True
    r = (region or "").lower()
    return any(p in r for p in patterns)


def region_excluded(region, patterns):
    """True, если регион попадает под исключение. patterns — список подстрок (lower).
    Города фед. значения отсекаются, но соответствующие ОБЛАСТИ сохраняются:
    'Москва' исключается, 'Московская область' — нет; 'Санкт-Петербург' исключается,
    'Ленинградская область' — нет."""
    if not patterns:
        return False
    r = (region or "").lower()
    if "московск" in r or "ленинградск" in r:  # области — оставляем
        return False
    return any(p in r for p in patterns)


def harvest(industries, min_revenue=1e9, per_industry=40, region=None,
            headless=False, out_path=None, exclude_regions=None, offscreen=False):
    inc = region_patterns(region)  # None, если регион не задан
    by_inn = {}
    skipped_excl = 0
    with RusProfileSession(headless=headless, offscreen=offscreen) as s:
        for ind in industries:
            cfg = INDUSTRY[ind]
            pages = max(1, -(-per_industry // 50))  # ceil(per_industry/50)
            if exclude_regions or inc:
                # регион фильтруется КЛИЕНТСКИ -> листаем по максимуму: нужная
                # выдача рассыпана по всем страницам (по региону не сортируется).
                pages = 20
            log(f"\n=== {ind}: {cfg['label']}"
                + (f" | регион: {region}" if inc else "") + " ===")
            items = s.search(cfg["okved"], min_revenue, max_pages=min(20, pages))
            kept = 0
            for it in items:
                inn = (it.get("inn") or "").strip()
                if not inn or inn in by_inn:
                    continue
                if it.get("inactive"):
                    continue
                reg = it.get("region") or ""
                if inc and not region_included(reg, inc):
                    continue
                if region_excluded(reg, exclude_regions):
                    skipped_excl += 1
                    continue
                lead = item_to_lead(it, cfg, ind)
                if lead["_revenue"] and lead["_revenue"] < min_revenue:
                    continue
                by_inn[inn] = lead
                kept += 1
                if kept >= per_industry:
                    break
            log(f"  -> отобрано {kept} (порог >{min_revenue/1e9:g} млрд"
                + (f", регион «{region}»" if inc else "") + ")")
            if out_path:
                _save(list(by_inn.values()), out_path)
    res = list(by_inn.values())
    if exclude_regions:
        log(f"\n[исключение регионов] отсеяно {skipped_excl} компаний "
            f"(паттерны: {', '.join(exclude_regions)})")
    if out_path:
        _save(res, out_path)
    return res


def _save(rows, path):
    json.dump(rows, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--industries", default=None,
                    help="energy,water,construction,processing,opk (через запятую)")
    ap.add_argument("--okved", default=None, help="произвольные коды ОКВЭД через запятую")
    ap.add_argument("--min-revenue", type=float, default=1e9)
    ap.add_argument("--per-industry", type=int, default=40)
    ap.add_argument("--region", default=None,
                    help="регион: название/аббревиатура, КЛИЕНТСКИЙ фильтр "
                         "(напр. ХМАО, Татарстан, 'Свердловская область')")
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--out", default="D:/лиды/_rusprofile.json")
    a = ap.parse_args()
    t0 = time.time()
    if a.okved:
        codes = [c.strip() for c in a.okved.split(",") if c.strip()]
        max_pages = 20 if a.region else a.max_pages  # регион -> листаем по максимуму
        with RusProfileSession(headless=a.headless) as s:
            items = s.search(codes, a.min_revenue, max_pages=max_pages)
        inc = region_patterns(a.region)
        cfg = {"label": "ОКВЭД " + ",".join(codes), "pain": "", "offer": ""}
        leads, seen = [], set()
        for it in items:
            inn = (it.get("inn") or "").strip()
            if not inn or inn in seen or it.get("inactive"):
                continue
            if inc and not region_included(it.get("region") or "", inc):
                continue
            seen.add(inn); leads.append(item_to_lead(it, cfg, "custom"))
        _save(leads, a.out)
        log(f"\nГОТОВО: {len(leads)} компаний -> {a.out} за {int(time.time()-t0)}с")
        return
    inds = [s.strip() for s in (a.industries or "construction,energy,processing").split(",") if s.strip()]
    rows = harvest(inds, min_revenue=a.min_revenue, per_industry=a.per_industry,
                   region=a.region, headless=a.headless, out_path=a.out)
    import collections
    by = collections.Counter(l["_industry"] for l in rows)
    log(f"\nГОТОВО: {len(rows)} компаний | по отраслям: {dict(by)} | {int(time.time()-t0)}с -> {a.out}")


if __name__ == "__main__":
    main()
