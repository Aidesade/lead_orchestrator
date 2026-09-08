# -*- coding: utf-8 -*-
r"""
Источник лидов RusProfile — расширенный поиск с фильтром по ОКВЭД + ВЫРУЧКЕ.

Штатный браузер Фазы 1 — Playwright с cookie авторизованного аккаунта.
undetected_chromedriver сохранён как явный fallback через
``RUSPROFILE_BROWSER=uc``. Выдача рендерится Vue через внутренний API:

  POST https://www.rusprofile.ru/ajax/search/advanced?cacheKey=<rand>
  заголовок X-Csrf-Token = cookie __Host-csrf-token (ротируется per-load)
  тело: {"action":"search_advanced","query":"","state_1..5":bool,"okved_strict":true,
         "okved":["41.20",...],"finance_revenue_from":"1000000000","page":1}
  ответ: data.items[] c {inn,name,region,address,ceo_name,main_okved_id,okved_descr,
         finance_revenue,authorized_capital,reg_date,okpo,link}, data.total_count,
         data.pagination{per_page_limit:50, page_count<=20}.

Зовём API ИЗНУТРИ страницы (Playwright page.evaluate + синхронный XHR) — same-origin, cookie
Cloudflare и CSRF уже на месте. Фильтр по выручке и ОКВЭД делает сервер -> сразу
крупный бизнес нужной отрасли с ИНН и выручкой. RusProfile не гарантирует порядок
по выручке, поэтому доступные страницы дополнительно сортируются на клиенте.

CLI:
  py source_rusprofile.py --industries construction,energy,processing --min-revenue 1e9 \
        --per-industry 40 --out D:\лиды\_rp.json
  py source_rusprofile.py --okved 27.10,24.10 --min-revenue 1e9 --max-pages 10
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time

import undetected_chromedriver as uc

from browser_util import chrome_major  # version_main под реально установленный Chrome
from okved2_codes import (  # раздел C — отрасль manufacturing, раздел D — energy
    OKVED2_SECTION_C,
    OKVED2_SECTION_D,
)

uc.Chrome.__del__ = lambda self: None

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

ADV_URL = "https://www.rusprofile.ru/search-advanced"
MIN_REVENUE_FLOOR = 1_000_000_000
MAX_SEARCH_PAGES = 20
# Порог среднесписочной численности (ССЧ — показатель RusProfile по данным ФНС):
# компания годится от `LEAD_MIN_STAFF` человек (дефолт 50) и только за STAFF_YEAR.
# Нет показателя за этот год — отказ (fail-closed), ровно как с выручкой за 2025.
STAFF_YEAR = 2025
DEFAULT_MIN_STAFF = 50


def lead_min_staff():
    """Порог ССЧ (`LEAD_MIN_STAFF`, дефолт 50; `0` — фильтр выключен).

    Невалидное значение -> ValueError, а не молчаливый дефолт: опечатка в пороге
    меняет ВЕСЬ отбор и обязана остановить прогон."""
    raw = str(os.environ.get("LEAD_MIN_STAFF", "") or "").strip()
    if not raw:
        return DEFAULT_MIN_STAFF
    try:
        value = int(float(raw.replace(" ", "")))
    except ValueError as exc:
        raise ValueError(
            "LEAD_MIN_STAFF должен быть целым числом сотрудников (напр. 50)") from exc
    if value < 0:
        raise ValueError("LEAD_MIN_STAFF не может быть отрицательным")
    return value


def staff_verdict(lead, min_staff, year=STAFF_YEAR):
    """«ok» — ССЧ за нужный год не ниже порога; «below» — ниже порога;
    «unknown» — показателя за нужный год нет (в том числе только за более
    ранний: «был штат в 2023» ≠ «есть штат в 2025»)."""
    lead = lead or {}
    count = integer_value(lead.get("_staff_count"))
    staff_year = integer_value(lead.get("_staff_year"))
    if count is None or staff_year != year:
        return "unknown"
    return "ok" if count >= min_staff else "below"


def staff_reserve(per_industry, min_staff):
    """Сколько компаний сверх N брать из выдачи под отсев по карточке (0 без гейта).

    Дефолт — 20% от N, не меньше 3. `LEAD_STAFF_RESERVE` задаёт число явно: нужен
    для добора, когда верх выдачи уже занят компаниями, которые карточка отвергла
    (без показателя за STAFF_YEAR): они не в CRM, снова попадут в выборку и снова
    отсеются, так что резерв должен покрыть и их."""
    if not min_staff:
        return 0
    raw = str(os.environ.get("LEAD_STAFF_RESERVE", "") or "").strip()
    if not raw:
        return max(3, (int(per_industry) + 4) // 5)   # ceil(20% от N), не меньше 3
    try:
        return max(0, int(raw))
    except ValueError as exc:
        raise ValueError(
            "LEAD_STAFF_RESERVE должен быть целым числом компаний") from exc


def staff_gate(leads, per_industry, min_staff, log=None):
    """Окончательный гейт ССЧ по карточке и обрезка резерва до N на отрасль.

    Выдача advanced-search показателя численности НЕ несёт (проверено вживую
    2026-09-04: ключей `sshr` в items нет вовсе), а серверный `sshr_from` —
    предфильтр без года. Поэтому `harvest` берёт из выдачи резерв сверх N,
    карточка дописывает `_staff_count`/`_staff_year`, а здесь неподходящие
    отсеиваются fail-closed (ниже порога или нет показателя за STAFF_YEAR) и
    каждая отрасль режется до N по убыванию выручки — ДО общего отбора, чтобы
    резерв одной отрасли не добивал другую. `min_staff` = 0 — только обрезка."""
    log = log or (lambda *_a, **_k: None)
    per_industry = max(1, int(per_industry))
    kept, dropped = [], collections.Counter()
    for lead in leads:
        verdict = staff_verdict(lead, min_staff) if min_staff else "ok"
        if verdict == "ok":
            kept.append(lead)
        else:
            dropped[verdict] += 1
    if min_staff:
        log(f"[ССЧ] гейт >={min_staff} за {STAFF_YEAR} по карточке: прошло "
            f"{len(kept)} из {len(leads)}"
            + (f" | ниже порога {dropped['below']}" if dropped["below"] else "")
            + (f" | нет показателя за {STAFF_YEAR}: {dropped['unknown']}"
               if dropped["unknown"] else ""))
    by_ind = collections.defaultdict(list)
    for lead in kept:
        by_ind[lead.get("_industry")].append(lead)
    out = []
    for ind, rows in by_ind.items():
        rows.sort(key=lambda lead: lead.get("_revenue") or 0, reverse=True)
        if min_staff and len(rows) < per_industry:
            log(f"[ССЧ] {ind}: после гейта {len(rows)} из {per_industry} — "
                "резерв выдачи отсев не покрыл")
        out.extend(rows[:per_industry])
    return out

# ОКВЭД-2 коды по отраслям (RusProfile использует текущий ОКВЭД-2014).
# Боль/оффер — те же, что в build_bigleads + ОПК.
INDUSTRY = {
    "energy": {
        "label": "Производство и распределение электроэнергии, газа и воды",
        # Все 49 кодов класса 35: точный матч main_okved_id, а ТЭЦ и сети сидят на
        # видах 35.11.1 / 35.12.1 / 35.30.3 (партии по Башкортостану 2026-08).
        "okved": sorted(OKVED2_SECTION_D),
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
    # ⚠️ advanced-search с okved_strict матчит ТОЧНЫЙ main_okved_id: «06.10» не
    # ловит «06.10.1», а по группе «06.1» во всём ПФО находится одна компания
    # (проверено вживую 2026-09-04). Поэтому здесь перечислены ВСЕ уровни, включая
    # виды; секции выше уровня групп (mining, agriculture, trade…) тем же XHR
    # находят почти ничего — не «чинить» их добавлением групп, только видами.
    "oilgas": {
        "label": "Нефтегаз (добыча нефти и газа, нефтесервис, нефтепереработка, "
                 "трубопроводы, газораспределение)",
        "okved": [
            "06.10", "06.10.1", "06.10.2", "06.10.3",             # нефть и попутный газ
            "06.20", "06.20.1", "06.20.2",                        # природный газ, конденсат
            "09.10", "09.10.1", "09.10.2", "09.10.3", "09.10.4", "09.10.9",  # нефтесервис
            "19.20", "19.20.1", "19.20.2", "19.20.9",             # нефтепереработка
            "49.50", "49.50.1", "49.50.11", "49.50.12",           # трубопроводы: нефть
            "49.50.2", "49.50.21", "49.50.22",                    # трубопроводы: газ
            "35.21", "35.22", "35.23",                            # газ: производство, ГРО, сбыт
        ],
        "pain": "Простои и аварии на промысле и НПЗ, промбезопасность, геология, "
                "энергоёмкость, регламентная документация",
        "offer": "Предиктивное ТОиР скважин и установок, CV-контроль ОТ и ПБ, "
                 "ИИ-анализ геоданных, RAG по регламентам и ПБ",
    },
    "manufacturing": {
        "label": "Обрабатывающие производства (раздел C ОКВЭД целиком)",
        # Все 910 кодов раздела C ниже групп (классы, подклассы, виды): по группам
        # NN.N строгий матч находил 39 компаний на весь ПФО, по классам NN.NN — 575,
        # со всеми уровнями — 1281 (замер 2026-09-07). Пересекается с `processing`;
        # в одном прогоне дубли снимает by_inn по порядку отраслей, между прогонами —
        # индекс CRM. ⚠️ Объём по ПФО больше окна выдачи (1000): виден верх по выручке.
        "okved": sorted(OKVED2_SECTION_C),
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
        # ждать прохождения Cloudflare-challenge ПО ФАКТУ появления CSRF-cookie (до 30с),
        # а не слепые 6 секунд: на медленной машине challenge не успевал — и весь сбор
        # молча возвращал 0 компаний.
        end = time.time() + 30
        while True:
            time.sleep(3)
            try:
                if any(c.get("name") == "__Host-csrf-token" for c in (self._d.get_cookies() or [])):
                    break
            except Exception:
                pass
            if time.time() >= end:
                break
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

    def _post_retry(self, body, log=log):
        """_post с одним повтором: при сбое/success=false перезагрузить страницу поиска
        (новый Cloudflare-проход и свежий CSRF) и попробовать ещё раз. None = не удалось."""
        last = ""
        for i in range(2):
            try:
                r = self._post(body)
                if r.get("success"):
                    return r
                last = "success=false"
            except Exception as e:
                last = str(e).splitlines()[0][:70]
            if i == 0:
                log(f"  [warn] стр.{body.get('page')}: {last} — обновляю страницу и повторяю")
                try:
                    self._d.get(ADV_URL)
                    time.sleep(6)
                except Exception:
                    pass
        log(f"  [warn] стр.{body.get('page')}: {last} — стоп")
        return None

    def search(self, okved, revenue_from, max_pages=20, pause=1.0, log=log,
               staff_from=None, staff_to=None):
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
        if staff_from is not None:
            base["sshr_from"] = str(int(staff_from))
        if staff_to is not None:
            base["sshr_to"] = str(int(staff_to))
        if (staff_from is not None and staff_to is not None
                and int(staff_from) > int(staff_to)):
            raise ValueError("staff_from не может быть больше staff_to")
        out, total = [], None
        for page in range(1, max_pages + 1):
            body = dict(base, page=page)
            r = self._post_retry(body, log)
            if r is None:
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

def revenue_value(raw):
    """Числовая выручка RusProfile; неизвестное значение сортируется последним."""
    rev = raw
    try:
        if isinstance(rev, str):
            rev = rev.replace("\xa0", "").replace(" ", "").replace(",", ".")
        rev = int(float(rev)) if rev not in (None, "") else None
    except (TypeError, ValueError):
        rev = None
    return rev


def displayed_revenue_value(raw):
    """«2,5 млрд руб.» / «2 500 000 000 ₽» -> рубли из карточки.

    Значение карточки связано с явно распознанным там же отчётным годом, поэтому
    строгий collector перепроверяет им порог, а не полагается только на выдачу.
    """
    text = str(raw or "").lower().replace("\xa0", " ")
    match = re.search(r"\d[\d\s]*(?:[,.]\d+)?", text)
    if not match:
        return None
    number = match.group(0).strip().replace(" ", "").replace(",", ".")
    try:
        value = float(number)
    except ValueError:
        return None
    multiplier = 1
    if "трлн" in text:
        multiplier = 1_000_000_000_000
    elif "млрд" in text:
        multiplier = 1_000_000_000
    elif "млн" in text:
        multiplier = 1_000_000
    elif "тыс" in text:
        multiplier = 1_000
    return int(value * multiplier)


def integer_value(raw):
    """Целое из числового поля API; пустое/маркер возвращает None."""
    try:
        text = str(raw).replace("\xa0", "").replace(" ", "")
        return int(float(text)) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def item_to_lead(it, cfg, industry):
    rev = revenue_value(it.get("finance_revenue"))
    okved = str(it.get("main_okved_id") or "").strip()
    # Advanced-search иногда отдаёт frontend-маркер вроде ``!~.~1.01`` вместо
    # 10.11. Не пропускаем его в JSON; правильный код добирается с уже открытой
    # карточки Playwright без дополнительного GET.
    if not re.fullmatch(r"\d{2}(?:\.\d{1,2}){1,2}", okved):
        okved = ""
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
        "_revenue_year": integer_value(it.get("finance_year")),
        "_staff_count": integer_value(it.get("sshr")),
        "_staff_year": integer_value(it.get("sshr_year")),
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
    # Алиас — только целым словом: голой подстрокой «мск» сидит внутри «пер-мск-ий»,
    # и «Пермский» превращался в фильтр по Москве (пермские компании молча выпадали
    # из отраслевых сборов по ПФО до 2026-09-07). Дефис — граница: «ханты-мансийск».
    for key, pats in REGION_ALIASES.items():
        if re.search(r"(?<![а-яёa-z])" + re.escape(key) + r"(?![а-яёa-z])", q):
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


# Маркеры отрицания в свободном тексте региона: "НЕ Москва" -> искать всё, кроме Москвы.
REGION_NEG_PREFIXES = ("не ", "!", "кроме ", "исключить ", "except ", "not ")


def parse_region_query(query):
    """Свободный текст региона -> (include, exclude) списки подстрок для матча по выдаче.

    Поддерживает ОТРИЦАНИЕ — регион с приставкой исключается, а не включается:
        'НЕ Москва'       -> exclude=['москва']            (вся РФ, кроме Москвы)
        '!Москва' / '-Москва' -> exclude=['москва']
        'кроме СПб'       -> exclude=['санкт-петербург']
    Можно перечислять через запятую и смешивать включение/исключение:
        'Урал, НЕ Москва' -> include=['урал'], exclude=['москва']

    Аббревиатуры (ХМАО/ЯНАО/СПб/МСК) разворачиваются через region_patterns().
    Возвращает (inc|None, exc|None); None означает «этой части фильтра нет».
    NB: города фед. значения в исключении не задевают одноимённую ОБЛАСТЬ —
    'НЕ Москва' убирает Москву, но оставляет «Московскую область»
    (см. region_excluded())."""
    q = (query or "").strip()
    if not q:
        return None, None
    inc, exc = [], []
    for raw in q.split(","):
        tok = raw.strip()
        if not tok:
            continue
        neg = False
        low = tok.lower()
        for pref in REGION_NEG_PREFIXES:
            if low.startswith(pref):
                neg = True
                tok = tok[len(pref):].strip()
                break
        if not neg and tok.startswith("-"):   # форма '-Москва'
            neg = True
            tok = tok[1:].strip()
        pats = region_patterns(tok)
        if not pats:
            continue
        (exc if neg else inc).extend(pats)
    return (inc or None), (exc or None)


def region_code_filter():
    """Код субъекта РФ для СЕРВЕРНОГО фильтра выдачи (`RUSPROFILE_REGION_CODE`).

    Пусто — прежнее поведение: листаем страну и режем регион у себя. Код задан —
    RusProfile отдаёт только этот субъект, и в те же 20 страниц влезает весь регион,
    а не его случайный срез из общероссийского топа. Клиентский фильтр при этом
    НЕ отключается: код — ускоритель выдачи, а решение о регионе остаётся за
    `region_included()`, иначе опечатка в коде тихо впустила бы чужой субъект.
    Список через запятую (`16,02,63`) — округ одним запросом, как
    `STATE_LEAD_REGION_CODE` в госрежиме."""
    return str(os.environ.get("RUSPROFILE_REGION_CODE", "") or "").strip()


def _harvest_with_session(session, industries, min_revenue, per_industry, region,
                          out_path, exclude_regions, max_pages, min_staff=0,
                          exclude=None):
    region_codes = [c.strip() for c in region_code_filter().split(",") if c.strip()] or None
    inc, exc = parse_region_query(region)  # 'НЕ Москва' -> inc=None, exc=['москва']
    if exclude_regions:                    # явные исключения (обратная совместимость)
        exc = (exc or []) + list(exclude_regions)
    exclude_regions = exc
    has_filter = bool(inc or exclude_regions)
    by_inn = {}
    skipped_excl = 0
    for ind in industries:
        cfg = INDUSTRY[ind]
        log(f"\n=== {ind}: {cfg['label']}"
            + (f" | регион: {region}" if has_filter else "") + " ===")
        # Живой ответ RusProfile не упорядочен по finance_revenue. Берём все
        # доступные страницы (API ограничивает их двадцатью), затем сортируем.
        # ⚠️ Двадцать страниц — это 1000 записей и жёсткий потолок САМОГО RusProfile:
        # запрос 50 страниц всё равно останавливается на 20-й (замерено 2026-08-24).
        # Поэтому при региональном сборе решает не глубина листания, а серверный
        # фильтр по коду субъекта: тот же construction по Башкортостану дал 25 из
        # тысячи по стране против 54 по коду «02» — вдвое больше и без мусора.
        # Серверный предфильтр ССЧ (`sshr_from`, без года): в items показателя
        # нет, окончательно решает карточка — см. staff_gate().
        items = session.search(cfg["okved"], min_revenue, max_pages=max_pages,
                               region_codes=region_codes, staff_from=min_staff or None)
        items = sorted(
            (it for it in items if isinstance(it, dict)),
            key=lambda it: revenue_value(it.get("finance_revenue")) or -1,
            reverse=True,
        )
        kept = skipped_staff = skipped_known = 0
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
            # Сервер уже получил finance_revenue_from; перепроверка не даёт
            # пропустить пустую/некорректную выручку или регрессию API.
            if lead["_revenue"] is None or lead["_revenue"] < min_revenue:
                continue
            # Порог ССЧ по выдаче — только если она вообще несёт показатель
            # (живой XHR его не отдаёт, 2026-09-04); иначе решает карточка,
            # а отказывать вслепую значило бы отсеять всех.
            if min_staff and "sshr" in it and staff_verdict(lead, min_staff) != "ok":
                skipped_staff += 1
                continue
            # Уже заведённые (индекс CRM) — мимо, и ДО карточки: «собрать N» при
            # выгрузке в CRM значит N новых, а не N минус дубли, которые CRM
            # потом всё равно отбросит, зато окно выдачи и карточки уже потрачены.
            if exclude is not None and exclude.contains(lead):
                skipped_known += 1
                continue
            by_inn[inn] = lead
            kept += 1
            if kept >= per_industry:
                break
        log(f"  -> отобрано {kept} по убыванию выручки (порог >={min_revenue/1e9:g} млрд"
            + (f", ССЧ >={min_staff} за {STAFF_YEAR}" if min_staff else "")
            + (f", регион «{region}»" if has_filter else "") + ")"
            + (f"; по ССЧ отсеяно {skipped_staff}" if skipped_staff else "")
            + (f"; уже в CRM {skipped_known}" if skipped_known else ""))
        if out_path:
            _save(list(by_inn.values()), out_path)
    res = list(by_inn.values())
    if exclude_regions:
        log(f"\n[исключение регионов] отсеяно {skipped_excl} компаний "
            f"(паттерны: {', '.join(exclude_regions)})")
    if out_path:
        _save(res, out_path)
    return res


def _browser_session_class():
    browser = (os.environ.get("RUSPROFILE_BROWSER") or "playwright").strip().lower()
    if browser in ("playwright", "pw"):
        from rusprofile_playwright import RusProfilePlaywrightSession
        return RusProfilePlaywrightSession
    if browser in ("uc", "chrome", "selenium"):
        return RusProfileSession
    raise ValueError(
        f"неизвестный RUSPROFILE_BROWSER={browser!r}; допустимо: playwright, uc")


def harvest(industries, min_revenue=1e9, per_industry=40, region=None,
            headless=False, out_path=None, exclude_regions=None, offscreen=False,
            session=None, max_pages=None, min_staff=None, exclude=None):
    """Собрать лиды; переданный session удобен для одной Playwright-context Фазы 1.

    ``min_staff`` (дефолт — ``LEAD_MIN_STAFF``, 50) — нижняя граница ССЧ за
    ``STAFF_YEAR``; 0 выключает фильтр. ``exclude`` — объект с ``contains(lead)``
    (индекс лидов CRM `crm_push.ExistingLeads`): такие компании не идут в счёт
    ``per_industry`` и не открываются карточкой."""
    min_revenue = max(float(min_revenue), float(MIN_REVENUE_FLOOR))
    per_industry = max(1, int(per_industry))
    min_staff = lead_min_staff() if min_staff is None else max(0, int(min_staff))
    if max_pages is None:
        max_pages = os.environ.get("RUSPROFILE_MAX_PAGES", str(MAX_SEARCH_PAGES))
    try:
        max_pages = max(1, min(MAX_SEARCH_PAGES, int(max_pages)))
    except (TypeError, ValueError) as exc:
        raise ValueError("RUSPROFILE_MAX_PAGES должен быть целым числом 1..20") from exc

    args = (
        industries, min_revenue, per_industry, region, out_path,
        exclude_regions, max_pages, min_staff, exclude,
    )
    if session is not None:
        return _harvest_with_session(session, *args)

    session_cls = _browser_session_class()
    with session_cls(headless=headless, offscreen=offscreen) as owned_session:
        return _harvest_with_session(owned_session, *args)


class StateLeadExhausted(RuntimeError):
    """Фильтр исчерпан раньше, чем найдено запрошенное число новых лидов."""

    def __init__(self, requested, found, stats):
        self.requested = int(requested)
        self.found = int(found)
        self.stats = dict(stats)
        super().__init__(
            f"по строгим критериям найдено {self.found}/{self.requested}; "
            f"источник исчерпан в заданном лимите страниц")


class StateOwnershipUnavailable(RuntimeError):
    """Официальный источник госучастия системно недоступен."""


def _industry_for_okved(code):
    """Наиболее специфичная существующая отрасль для downstream-материалов."""
    code = str(code or "").strip()
    priority = {"opk": 3, "processing": 2, "manufacturing": 1}
    matches = []
    for key, cfg in INDUSTRY.items():
        for prefix in cfg.get("okved") or ():
            if code == prefix or code.startswith(prefix + "."):
                matches.append((len(prefix), priority.get(key, 0), key))
    return max(matches)[2] if matches else "services"


def _merge_state_contacts(lead, contacts):
    """Перенести факты карточки в канонические поля лида."""
    contacts = contacts or {}
    for key in (
        "_ceo_post", "_ceo_fio", "_ceo_inn", "_capital", "_main_okved_id",
        "_staff_count", "_staff_year", "_revenue_year", "_revenue_display",
    ):
        if contacts.get(key):
            lead[key] = contacts[key]
    if contacts.get("_ceo_fio"):
        lead["contact_person"] = contacts["_ceo_fio"]
    if contacts.get("website"):
        lead["website"] = contacts["website"]
    phones = list(contacts.get("phones") or [])
    if contacts.get("phone") and contacts["phone"] not in phones:
        phones.insert(0, contacts["phone"])
    if phones:
        lead["_phones"] = phones
        lead["phone"] = ", ".join(phones)[:90]
    emails = list(dict.fromkeys(
        str(value).strip().lower() for value in (contacts.get("emails") or [])
        if str(value).strip()))
    if emails:
        lead["_emails"] = emails
        lead["email"] = emails[0]
        lead["_email_src"] = "RusProfile/Playwright"
    if contacts.get("founders"):
        lead["_founders"] = contacts["founders"]


def harvest_state_owned(*args, **kwargs):
    """Ленивый фасад, чтобы основной источник оставался канонической точкой входа."""
    from state_lead_collection import harvest_state_owned as implementation
    return implementation(*args, **kwargs)


def _save(rows, path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--industries", default=None,
                    help="energy,water,construction,processing,opk (через запятую)")
    ap.add_argument("--okved", default=None, help="произвольные коды ОКВЭД через запятую")
    ap.add_argument("--min-revenue", type=float, default=1e9)
    ap.add_argument("--per-industry", type=int, default=40)
    ap.add_argument("--region", default=None,
                    help="регион: название/аббревиатура, КЛИЕНТСКИЙ фильтр "
                         "(напр. ХМАО, Татарстан, 'Свердловская область'). "
                         "Приставка отрицания = исключение: 'НЕ Москва' -> вся РФ кроме Москвы; "
                         "через запятую можно смешивать: 'Урал, НЕ Москва'")
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--out", default="D:/лиды/_rusprofile.json")
    a = ap.parse_args()
    t0 = time.time()
    if a.okved:
        codes = [c.strip() for c in a.okved.split(",") if c.strip()]
        max_pages = 20 if a.region else a.max_pages  # регион -> листаем по максимуму
        threshold = max(float(a.min_revenue), float(MIN_REVENUE_FLOOR))
        with _browser_session_class()(headless=a.headless) as s:
            items = s.search(codes, threshold, max_pages=max_pages)
        items = sorted(
            items,
            key=lambda it: revenue_value(it.get("finance_revenue")) or -1,
            reverse=True,
        )
        inc, exc = parse_region_query(a.region)  # 'НЕ Москва' -> exc=['москва']
        cfg = {"label": "ОКВЭД " + ",".join(codes), "pain": "", "offer": ""}
        leads, seen = [], set()
        for it in items:
            inn = (it.get("inn") or "").strip()
            if not inn or inn in seen or it.get("inactive"):
                continue
            reg = it.get("region") or ""
            if inc and not region_included(reg, inc):
                continue
            if region_excluded(reg, exc):
                continue
            lead = item_to_lead(it, cfg, "custom")
            if lead["_revenue"] is None or lead["_revenue"] < threshold:
                continue
            seen.add(inn); leads.append(lead)
        _save(leads, a.out)
        log(f"\nГОТОВО: {len(leads)} компаний -> {a.out} за {int(time.time()-t0)}с")
        return
    inds = [s.strip() for s in (a.industries or "construction,energy,processing").split(",") if s.strip()]
    rows = harvest(inds, min_revenue=a.min_revenue, per_industry=a.per_industry,
                   region=a.region, headless=a.headless, out_path=a.out)
    by = collections.Counter(l["_industry"] for l in rows)
    log(f"\nГОТОВО: {len(rows)} компаний | по отраслям: {dict(by)} | {int(time.time()-t0)}с -> {a.out}")


if __name__ == "__main__":
    main()
