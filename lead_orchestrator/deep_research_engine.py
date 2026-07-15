# -*- coding: utf-8 -*-
r"""
deep_research_engine — НАСТОЯЩИЙ глубокий ресёрч-движок для пресейл-пайплайна.

Заменяет фейково-глубокий deep_research (который ходил только в 3 структурных API и
не открывал ни одной веб-страницы). Собран из трёх паттернов:

  1) АРХИТЕКТУРА supervisor + параллельные коллекторы по ИСТОЧНИКАМ
     (как langchain-ai/open_deep_research): {официальная база, сайт компании,
     ЕИС/zakupki по ИНН, суды+СМИ, hh.ru, TAdviser} — каждый коллектор узко «вытащи всё
     поимённо из этого источника», запуск конкурентно через asyncio.Semaphore,
     затем синтез находок.

  2) ПЕТЛЯ ЦЕЛЕВОГО ДОБОРА (как dzhng/deep-research): после первого прохода
     completeness_critic считает НЕЗАПОЛНЕННЫЕ ячейки целевых таблиц
     (leadership / branches / departments.contact / соцсети / ИТ-контакт / ИТ-ландшафт) и
     генерирует follow-up запросы РОВНО под эти пробелы, повторяя релевантные
     коллекторы, пока пробелы не закрыты или не исчерпан depth-бюджет.

  3) РЕАЛЬНЫЙ КРАУЛИНГ Crawl4AI (PRIMARY, self-host, под on-prem/152-ФЗ):
     BestFirstCrawlingStrategy по ключевым словам. Грейсфул-фолбэк: если crawl4ai
     не установлен/упал — HTTP-фетч главной + поиск подстраниц через DuckDuckGo +
     фиксированный набор путей. ЕИС — отдельный best-effort неблокирующий коллектор
     (антибот: даём ссылки на выдачу по ИНН + поисковую выдачу; полноценный парсинг
     карточек — опционально за FIRECRAWL_API_KEY).

Дисциплина данных: КАЖДАЯ собранная строка несёт source URL. Запрещены и выдуманные
факты, и уверенные отрицания без реально открытой страницы. Движок НИКОГДА не роняет
пайплайн — при любом сбое деградирует до официальной базы.

Бюджет/конкуренция (env-переопределяемо):
  DR_BREADTH=4  DR_DEPTH=2  DR_MAXPAGES=25  DR_LLM_CONCURRENCY=2  DR_CRAWL_CONCURRENCY=4
  DR_EXTRACT_MODEL=sonnet   DR_USE_LLM=1   DR_PAGE_CHARS=9000   DR_MAX_DOMAINS=3
  FIRECRAWL_API_KEY=...     (опц.) — парсинг карточек ЕИС за ключом

Мульти-домен: у компании (особенно госструктуры) часто 2-3 сайта — свой + страница на
ведомственном портале. discover_domains подтверждает до DR_MAX_DOMAINS доменов (доп. —
строго по ИНН на странице), сайт-коллектор краулит все (основной — полный кап страниц,
дополнительные — половинный). Критик полноты также следит за «экосистемой» — вертикалью
принятия решений (учредитель, курирующее ведомство, сестринские структуры, комиссии).

Зависимости: stdlib + (опц.) crawl4ai + claude-agent-sdk (для LLM-экстракта sonnet).
Чистая логика (regex_findings, completeness_critic, merge_findings, consolidate)
работает БЕЗ сети и БЕЗ LLM — это страховка и предмет дымового теста.
"""
import asyncio
import gzip
import io
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlparse
import warnings

# Глушим косметический RequestsDependencyWarning (chardet 7.x вне диапазона requests) ДО импорта requests.
warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------- бюджет ----
DR_BREADTH = int(os.environ.get("DR_BREADTH", "4"))            # запросов добора за раунд
DR_DEPTH = int(os.environ.get("DR_DEPTH", "2"))               # раундов целевого добора
DR_MAX_PAGES = int(os.environ.get("DR_MAXPAGES", "25"))       # кап страниц на сайт-краул
DR_LLM_CONCURRENCY = int(os.environ.get("DR_LLM_CONCURRENCY", "2"))   # параллельных sonnet CLI
DR_CRAWL_CONCURRENCY = int(os.environ.get("DR_CRAWL_CONCURRENCY", "4"))  # параллельных HTTP-фетчей
EXTRACT_MODEL = os.environ.get("DR_EXTRACT_MODEL", "sonnet")  # дешёвая модель для экстракта/критика
# Провайдер LLM-экстракта: 'claude' (по умолч., claude-agent-sdk) или 'kimi' (OpenAI-совместимый
# шлюз, тот же, что у писателя). 'kimi' нужен, когда Claude недоступен (прод в РФ / Docker) —
# тогда ВЕСЬ пайплайн работает без Anthropic. Имя модели Kimi берётся из KIMI_MODEL_NAME.
DR_LLM_PROVIDER = (os.environ.get("DR_LLM_PROVIDER", "claude") or "claude").strip().lower()
DR_USE_LLM = os.environ.get("DR_USE_LLM", "1") not in ("0", "false", "no", "")
DR_PAGE_CHARS = int(os.environ.get("DR_PAGE_CHARS", "9000"))  # кап текста страницы для LLM
DR_MAX_DOMAINS = int(os.environ.get("DR_MAX_DOMAINS", "3"))   # подтверждённых сайтов на компанию

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Ключевые слова релевантности (для Crawl4AI scorer и поиска подстраниц).
KEYWORDS = ["контакт", "руководств", "филиал", "ДРСУ", "о компании", "реквизит",
            "закуп", "тендер", "структура", "сотрудник", "директор", "отдел",
            "проектн", "институт", "ваканс",
            # ИТ-ландшафт (страницы TAdviser: «Проекты», «ИТ-системы», «Внедрения»)
            "внедрен", "автоматизац", "цифров", "ит-систем", "информационные технологии",
            "ERP", "CRM", "SAP", "1С", "ECM", "MES", "интегратор"]

# Пути-кандидаты для HTTP-фолбэка (для не-Tilda сайтов; Tilda всё держит на главной).
FALLBACK_PATHS = ("", "/contacts", "/kontakty", "/kontakty-i-rekvizity", "/contact",
                  "/rukovodstvo", "/about", "/o-kompanii", "/o-predpriyatii",
                  "/filialy", "/struktura", "/zakupki", "/tendery", "/rekvizity",
                  "/company", "/o-nas")

# Агрегаторы/реестры — НЕ собственный сайт компании (для discover_domain).
AGGREGATORS = ("checko.ru", "list-org", "rusprofile", "audit-it", "zachestnyibiznes",
               "spark-interfax", "b2b.house", "tbank.ru", "tinkoff", "sbis.ru",
               "testfirm", "rbc.ru", "sudact", "kad.arbitr", "zakupki.gov",
               "clearspending", "hh.ru", "find-org", "ruscatalog", "czn-",
               "bigorg", "ogrn", "vbankcenter", "synapsenet", "cmm.", "tadviser",
               # реальные нарушители из логов прогонов (карточки-каталоги за анти-ботом)
               "focus.kontur", "kontragent", "vbr.ru", "cataloxy", "tilbagevise",
               "e-disclosure", "cbr.ru", "saby.ru")


def _digits(s):
    return re.sub(r"\D", "", str(s or ""))


# is_own_site: настоящий латинский домен-сайт (не соцсеть/агрегатор). Из harvest_inn_site.
try:
    from harvest_inn_site import is_own_site
except Exception:
    def is_own_site(site):  # минимальный фолбэк, если модуль недоступен
        if not site:
            return False
        h = urlparse(site if site.startswith("http") else "http://" + site).netloc.lower()
        return bool(re.match(r"^[a-z0-9.\-]+\.[a-z]{2,}$", h)) and not any(
            b in h for b in ("vk.com", "t.me", "instagram", "2gis", "facebook"))


# ============================================================================
# СЕМАФОРЫ (ленивые, привязка к текущему loop — безопасно при нескольких loop'ах)
# ============================================================================
_SEMS = {}


def _sem(name, n):
    try:
        loop = asyncio.get_event_loop()
        key = (name, id(loop))
    except Exception:
        key = (name, 0)
    s = _SEMS.get(key)
    if s is None:
        s = asyncio.Semaphore(max(1, n))
        _SEMS[key] = s
    return s


# ============================================================================
# НИЗКОУРОВНЕВОЕ: HTTP-фетч, html->текст, поиск (DuckDuckGo HTML)
# ============================================================================
def _host_headers(url):
    """Заголовки под конкретный хост. TAdviser отдаёт 403 на ЛЮБОЙ русский Accept-Language
    (проверено: 'ru', 'ru,en;q=0.8', 'ru-RU,...' -> 403; 'en-US,en;q=0.9' -> 200)."""
    host = urlparse(url).netloc.lower()
    lang = "en-US,en;q=0.9" if "tadviser.ru" in host else "ru,en;q=0.8"
    return {"User-Agent": UA, "Accept-Language": lang, "Accept-Encoding": "gzip"}


def _fetch_sync(url, timeout=12):
    """GET -> (final_url, text) или (url, '') при ошибке. Прозрачно жмёт gzip."""
    try:
        req = urllib.request.Request(url, headers=_host_headers(url))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            ct = r.headers.get("Content-Type", "")
            enc = "utf-8"
            m = re.search(r"charset=([\w\-]+)", ct)
            if m:
                enc = m.group(1)
            return r.geturl(), raw.decode(enc, "replace")
    except Exception:
        return url, ""


_LINK_KEEP = re.compile(
    r"(?:https?:)?//(?:t\.me|vk\.com|ok\.ru|(?:www\.)?youtube\.com|(?:www\.)?instagram\.com|"
    r"dzen\.ru|rutube\.ru)/[\w.\-/@+]+", re.I)


def _html_to_text(html):
    """HTML -> компактный текст. ВАЖНО: соцсети/почты/телефоны живут в href/mailto/tel —
    их надо вытащить ДО срезки тегов, иначе теряются (как у текущего агента)."""
    if not html:
        return ""
    html = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    # сохранить «интересные» ссылки из атрибутов перед удалением тегов
    keep = set(m.group(0) for m in _LINK_KEEP.finditer(html))
    keep |= set("mailto:" + m for m in re.findall(r'mailto:([^"\'>\s)]+)', html, re.I))
    keep |= set("tel:" + m for m in re.findall(r'tel:([^"\'>\s)]+)', html, re.I))
    html = re.sub(r"<(br|/p|/div|/li|/tr|/h[1-6])[^>]*>", "\n", html, flags=re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    html = (html.replace("&nbsp;", " ").replace("&laquo;", "«").replace("&raquo;", "»")
                .replace("&mdash;", "—").replace("&ndash;", "–").replace("&amp;", "&")
                .replace("&quot;", '"').replace("&#34;", '"').replace("&#39;", "'"))
    html = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))) if int(m.group(1)) < 1114111 else " ", html)
    html = re.sub(r"&[a-z]+;", " ", html)
    html = re.sub(r"[ \t\xa0]+", " ", html)
    html = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", html)
    out = html.strip()
    if keep:
        out += "\n\nСсылки и контакты страницы: " + " ".join(sorted(keep))
    return out


async def fetch_page(url):
    """url -> {url, markdown, source} или None. Не падает."""
    final, html = await asyncio.to_thread(_fetch_sync, url)
    txt = _html_to_text(html)
    if not txt:
        return None
    return {"url": final or url, "markdown": txt, "source": "http"}


# Поисковики и собственные домены движка — НИКОГДА не результат и не «сайт компании».
_SEARCH_ENGINE_HOSTS = ("duckduckgo.com", "google.", "bing.com", "yandex.",
                        "search.marginalia", "startpage.com", "ecosia.org", "duck.com",
                        "search.brave.com", "mojeek.com", "microsoft.com", "msn.com")


def _not_search_engine(url):
    host = urlparse(url).netloc.lower()
    return bool(host) and not any(h in host for h in _SEARCH_ENGINE_HOSTS)


def _clean_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def _ddg_parse(html, n=8):
    out = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        mm = re.search(r"uddg=([^&]+)", m.group(1))
        url = urllib.parse.unquote(mm.group(1)) if mm else m.group(1)
        out.append({"url": url, "title": _clean_html(m.group(2)), "snippet": ""})
    snips = [_clean_html(s) for s in re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)]
    for i, sn in enumerate(snips):
        if i < len(out):
            out[i]["snippet"] = sn
    return out


def _brave_parse(html, n=8):
    """Brave SERP: результаты — <a href="URL">…title…</a> + следом блок .content (сниппет).
    Сниппеты Brave часто содержат контактных лиц закупок из извещений — это ценно."""
    out, seen = [], set()
    for m in re.finditer(
            r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+href="https?://|\Z)',
            html, re.S):
        url = m.group(1)
        if url in seen or not _not_search_engine(url):
            continue
        seen.add(url)
        tm = re.search(r'title="([^"]+)"', m.group(2))
        title = _clean_html(tm.group(1) if tm else m.group(2))[:200]
        sm = re.search(r'class="[^"]*content[^"]*"[^>]*>(.*?)</div>', m.group(3)[:2000], re.S)
        out.append({"url": url, "title": title,
                    "snippet": _clean_html(sm.group(1))[:450] if sm else ""})
        if len(out) >= n:
            break
    return out


def _bing_parse(html, n=8):
    out = []
    for m in re.finditer(r'<li class="b_algo">(.*?)</li>', html, re.S):
        blk = m.group(1)
        a = re.search(r'<h2>.*?<a[^>]+href="(https?://[^"]+)"', blk, re.S)
        if not a:
            continue
        h2 = re.search(r'<h2>(.*?)</h2>', blk, re.S)
        sn = re.search(r'<p[^>]*>(.*?)</p>', blk, re.S)
        out.append({"url": a.group(1), "title": _clean_html(h2.group(1))[:200] if h2 else "",
                    "snippet": _clean_html(sn.group(1))[:450] if sn else ""})
        if len(out) >= n:
            break
    return out


# Бэкенды в порядке предпочтения. Ротация старта + кулдаун забаненных + глобальный троттл.
_BACKENDS = (
    ("brave", "https://search.brave.com/search?", lambda q: {"q": q}, _brave_parse),
    ("ddg", "https://html.duckduckgo.com/html/?", lambda q: {"q": q, "kl": "ru-ru"}, _ddg_parse),
    ("bing", "https://www.bing.com/search?", lambda q: {"q": q, "setlang": "ru", "count": "20"}, _bing_parse),
)
_SEARCH_CACHE = {}
_SEARCH_ROT = [0]
_SEARCH_LAST = [0.0]
_BACKEND_COOLDOWN = {}
_SEARCH_MIN_INTERVAL = float(os.environ.get("DR_SEARCH_INTERVAL", "1.6"))


def _search_fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "ru,en;q=0.8", "Accept-Encoding": "gzip",
        "Referer": "https://www.google.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        return raw.decode("utf-8", "replace")


def _web_search_sync(query, n=8):
    """Мульти-бэкенд поиск -> [{url,title,snippet}]. Ротация старта, кулдаун 90 с при 429/403,
    кэш в рамках прогона. Best-effort: [] если все бэкенды недоступны (пайплайн не падает)."""
    if query in _SEARCH_CACHE:
        return _SEARCH_CACHE[query][:n]
    rot = _SEARCH_ROT[0] % len(_BACKENDS)
    _SEARCH_ROT[0] += 1
    order = _BACKENDS[rot:] + _BACKENDS[:rot]
    now = time.monotonic()
    for name, base, params, parser in order:
        if _BACKEND_COOLDOWN.get(name, 0) > now:
            continue
        try:
            html = _search_fetch(base + urllib.parse.urlencode(params(query)))
        except urllib.error.HTTPError as e:
            if e.code in (429, 403, 503):
                _BACKEND_COOLDOWN[name] = time.monotonic() + 90  # забанен -> остынь
            continue
        except Exception:
            continue
        try:
            res = [x for x in parser(html, n)
                   if x.get("url", "").startswith("http") and _not_search_engine(x["url"])]
        except Exception:
            res = []
        if res:
            _SEARCH_CACHE[query] = res
            return res[:n]
    _SEARCH_CACHE[query] = []  # негативный кэш — не долбить повторно за прогон
    return []


async def web_search(query, n=8):
    """Глобальный троттл поиска: сериализация + min-interval (HTML-поисковики банят веер)."""
    async with _sem("search", int(os.environ.get("DR_SEARCH_CONCURRENCY", "1"))):
        wait = _SEARCH_MIN_INTERVAL - (time.monotonic() - _SEARCH_LAST[0])
        if wait > 0:
            await asyncio.sleep(wait)
        res = await asyncio.to_thread(_web_search_sync, query, n)
        _SEARCH_LAST[0] = time.monotonic()
        return res


# ============================================================================
# РЕГЭКСП-ЭКСТРАКТ (LLM-независимая страховка; ядро дымового теста)
# ============================================================================
# Филиал/подразделение: «<Имя> ДРСУ/филиал … Директор: ФИО … Телефон/Диспетчер: …»
_BRANCH_RE = re.compile(
    r"([А-ЯЁ][А-Яа-яёA-Za-z\-\s«»\"]{2,45}?(?:ДРСУ|ДРСУч|филиал|участок|ДЭП|ДУ))[\s«»\":\-]*"
    r"(?:Директор|Руководитель|Начальник)[:\s]*"
    r"([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})\s*"
    r"((?:(?:Телефон|Тел|Диспетчер|Факс)[.:\s]*\(?\d[\d\s\-\(\)]{5,}[\s,;]*)+)",
    re.I)
_PHONE_RE = re.compile(r"\(?\+?\d{1,2}?\)?[\s\-]?\(?\d{3,5}\)?[\s\-]?\d[\d\s\-]{4,}\d")
_PLUS7_RE = re.compile(r"(?:\+7|8)\s*\(?\d{3,4}\)?[\s\-]?\d[\d\s\-]{4,}\d")
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_SOCIAL_PATS = (
    ("Telegram", re.compile(r"(?:https?://)?t\.me/[\w/+]+", re.I)),
    ("ВКонтакте", re.compile(r"(?:https?://)?vk\.com/[\w.\-/]+", re.I)),
    ("Одноклассники", re.compile(r"(?:https?://)?ok\.ru/[\w.\-/]+", re.I)),
    ("YouTube", re.compile(r"(?:https?://)?(?:www\.)?youtube\.com/[\w.\-/@]+", re.I)),
    ("Instagram", re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/[\w.\-/]+", re.I)),
    ("Дзен", re.compile(r"(?:https?://)?dzen\.ru/[\w.\-/]+", re.I)),
    ("Rutube", re.compile(r"(?:https?://)?rutube\.ru/[\w.\-/]+", re.I)),
)
_TENDER_KW = ("zakup", "tender", "torg", "postav", "kontrakt", "contract", "purchase")
_GENERAL_LP = ("info", "office", "mail", "priem", "secretar", "kanc", "general",
               "company", "reception", "post", "obsh", "adm")


def _norm_phones(blob):
    phones = []
    for m in _PHONE_RE.finditer(blob):
        p = re.sub(r"\s+", " ", m.group(0)).strip(" ,;-")
        if len(_digits(p)) >= 5:
            phones.append(p)
    # dedup, сохраняя порядок
    seen, out = set(), []
    for p in phones:
        k = _digits(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def _email_role(email, source_url=""):
    lp = email.split("@", 1)[0].lower()
    src = (source_url or "").lower()
    if any(k in lp for k in _TENDER_KW) or "zakup" in src or "tender" in src or "zakupki.gov" in src:
        return "tender"
    if any(lp.startswith(k) or k == lp for k in _GENERAL_LP):
        return "general"
    return "personal"


def _tadviser_project_system(url):
    """«…/Проект:Северсталь_(Citeck_ECOS)» -> «Citeck ECOS». Заголовок статьи-проекта TAdviser
    по соглашению = «<Компания> (<ИТ-система>)» — детерминированная страховка без LLM."""
    title = urllib.parse.unquote(url or "").split("/index.php/", 1)[-1]
    if not title.startswith("Проект:"):
        return ""
    m = re.search(r"\(([^()]{2,80})\)\s*\d*$", title.replace("_", " ").strip())
    return m.group(1).strip() if m else ""


def regex_findings(pages):
    """pages:[{url,markdown,source}] -> частичные находки БЕЗ LLM (с source URL у строк)."""
    branches, social, emails, phones, it_landscape = [], [], [], [], []
    seen_b, seen_s, seen_e, seen_p = set(), set(), set(), set()

    for pg in pages or []:
        url = pg.get("url", "")
        text = pg.get("markdown", "") or ""
        flat = re.sub(r"\s+", " ", text)

        # ИТ-система из заголовка статьи-проекта TAdviser
        if "tadviser" in (pg.get("source", "") + url):
            sysname = _tadviser_project_system(url)
            if sysname:
                it_landscape.append({"system": sysname, "vendor": "", "year": "",
                                     "note": "статья-проект TAdviser", "source": url})

        # филиалы
        for m in _BRANCH_RE.finditer(flat):
            branch = re.sub(r"\s+", " ", m.group(1)).strip(" «»\":-")
            # срезать ведущий ALLCAPS-заголовок («ФИЛИАЛЫ»/«НАШИ ФИЛИАЛЫ») перед именем
            branch = re.sub(r"^(?:[А-ЯЁ]{3,}\s+)+(?=[А-ЯЁ][а-яё])", "", branch).strip(" «»\":-")
            director = re.sub(r"\s+", " ", m.group(2)).strip()
            ph = _norm_phones(m.group(3))
            key = re.sub(r"[^а-яёa-z]", "", branch.lower())
            if key and key not in seen_b:
                seen_b.add(key)
                branches.append({"branch": branch, "director": director,
                                 "phone": "; ".join(ph), "source": url})

        # соцсети
        for label, pat in _SOCIAL_PATS:
            for hit in pat.findall(text):
                u = hit if hit.startswith("http") else "https://" + hit
                u = u.rstrip("/.,)")
                k = u.lower()
                if k not in seen_s and not k.endswith(("t.me", "vk.com")):
                    seen_s.add(k)
                    social.append({"kind": label, "url": u, "source": url})

        # email
        for e in _EMAIL_RE.findall(text):
            e = e.strip(".,;:").lower()
            if e in seen_e or any(x in e for x in (".png", ".jpg", ".svg", "@2x", "example.", "sentry")):
                continue
            seen_e.add(e)
            emails.append({"email": e, "role": _email_role(e, url), "source": url})

        # телефоны +7/8 (общие контакты)
        for m in _PLUS7_RE.finditer(text):
            p = re.sub(r"\s+", " ", m.group(0)).strip()
            k = _digits(p)
            if len(k) >= 10 and k not in seen_p:
                seen_p.add(k)
                phones.append({"phone": p, "source": url})

    return {
        "leadership": [],
        "branches": branches,
        "departments": [],
        "ecosystem": [],
        "it_landscape": it_landscape,
        "contacts": {"phones": phones, "emails": emails, "social": social,
                     "address": "", "schedule": ""},
        "procurement": {"summary": "", "contacts": [], "source": ""},
        "project_institute": {},
        "courts": [],
        "media": [],
    }


# ============================================================================
# СЛИЯНИЕ НАХОДОК
# ============================================================================
def _merge_list(dst, src, key):
    seen = {key(x) for x in dst}
    for x in src or []:
        k = key(x)
        if k and k not in seen:
            seen.add(k)
            dst.append(x)
        elif k:
            # обогащаем существующую строку недостающими полями
            for d in dst:
                if key(d) == k:
                    for f, v in x.items():
                        if v and not d.get(f):
                            d[f] = v
                    break


def merge_findings(parts):
    """Слить список частичных находок в один документ (dedup по ключам, союз контактов)."""
    out = {"leadership": [], "branches": [], "departments": [], "ecosystem": [],
           "it_landscape": [],
           "contacts": {"phones": [], "emails": [], "social": [], "address": "", "schedule": ""},
           "procurement": {"summary": "", "contacts": [], "source": ""},
           "project_institute": {}, "courts": [], "media": []}
    for p in parts:
        if not isinstance(p, dict):
            continue
        _merge_list(out["leadership"], p.get("leadership"),
                    lambda x: re.sub(r"[^а-яёa-z]", "", (str(x.get("position", "")) + str(x.get("fio", ""))).lower())[:60])
        _merge_list(out["branches"], p.get("branches"),
                    lambda x: re.sub(r"[^а-яёa-z]", "", str(x.get("branch", "")).lower()))
        _merge_list(out["departments"], p.get("departments"),
                    lambda x: re.sub(r"[^а-яёa-z]", "", str(x.get("block", "")).lower())[:40])
        _merge_list(out["ecosystem"], p.get("ecosystem"),
                    lambda x: re.sub(r"[^а-яёa-z0-9]", "", str(x.get("entity", "")).lower())[:50])
        _merge_list(out["it_landscape"], p.get("it_landscape"),
                    lambda x: re.sub(r"[^а-яёa-z0-9]", "",
                                     (str(x.get("system", "")) + str(x.get("vendor", ""))).lower())[:50])
        c = p.get("contacts") or {}
        _merge_list(out["contacts"]["phones"], c.get("phones"), lambda x: _digits(x.get("phone")))
        _merge_list(out["contacts"]["emails"], c.get("emails"), lambda x: (x.get("email") or "").lower())
        _merge_list(out["contacts"]["social"], c.get("social"), lambda x: (x.get("url") or "").lower())
        if c.get("address") and not out["contacts"]["address"]:
            out["contacts"]["address"] = c["address"]
        if c.get("schedule") and not out["contacts"]["schedule"]:
            out["contacts"]["schedule"] = c["schedule"]
        proc = p.get("procurement") or {}
        if proc.get("summary") and not out["procurement"]["summary"]:
            out["procurement"]["summary"] = proc["summary"]
            out["procurement"]["source"] = proc.get("source", "")
        _merge_list(out["procurement"]["contacts"], proc.get("contacts"),
                    lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True))
        pi = p.get("project_institute") or {}
        if pi.get("name") and not out["project_institute"].get("name"):
            out["project_institute"] = pi
        _merge_list(out["courts"], p.get("courts"), lambda x: (x.get("summary") or "")[:80])
        _merge_list(out["media"], p.get("media"), lambda x: (x.get("summary") or "")[:80])
    return out


# ============================================================================
# КРИТИК ПОЛНОТЫ + ГЕНЕРАЦИЯ FOLLOW-UP (детерминированный детектор пробелов)
# ============================================================================
PLACEHOLDER_RE = re.compile(
    r"(не\s+подтвержд|не\s+выявл|не\s+раскрыт|не\s+найден|не\s+установл|"
    r"через\s+при[её]мн|^\s*[—\-]\s*$|^\s*n/?a\s*$|неизвестн|отсутств|уточн)", re.I)


def _is_blank(v):
    if v is None:
        return True
    s = str(v).strip()
    return (not s) or bool(PLACEHOLDER_RE.search(s))


def _looks_tender_email(em):
    return em.get("role") == "tender" or "zakup" in (em.get("source", "").lower())


def completeness_critic(findings, name="", inn="", domain=""):
    """Считает пустые/«не подтверждено» в целевых таблицах и генерит follow-up под пробелы."""
    branches = findings.get("branches") or []
    leadership = findings.get("leadership") or []
    departments = findings.get("departments") or []
    contacts = findings.get("contacts") or {}
    proc = findings.get("procurement") or {}
    gaps, followups = [], []

    # 1) филиалы: каждая строка должна иметь директора И телефон
    b_total = len(branches)
    b_filled = 0
    for b in branches:
        if _is_blank(b.get("director")) or _is_blank(b.get("phone")):
            bn = (b.get("branch") or "филиал").strip()
            gaps.append(f"филиал «{bn}»: нет ФИО директора и/или телефона")
            followups.append({"goal": "branch",
                              "query": f"{name} {bn} директор телефон", "branch": bn})
        else:
            b_filled += 1

    # 2) тендерный/закупочный контакт (email/тел/ФИО)
    tender_emails = [e for e in (contacts.get("emails") or []) if _looks_tender_email(e)]
    has_tender = bool(proc.get("contacts")) or bool(tender_emails)
    if not has_tender:
        gaps.append("тендерный/закупочный контакт (email/тел/ФИО) не найден")
        followups.append({"goal": "tender",
                          "query": f"{name} закупки тендерный отдел контактное лицо email телефон"})
        if inn:
            followups.append({"goal": "tender",
                              "query": f"{inn} извещение закупка контактное лицо email"})

    # 3) контакт ИТ/цифровизации
    has_it = any(
        (("ит" in (d.get("block", "").lower()) or "цифр" in (d.get("block", "").lower())
          or "it" in (d.get("block", "").lower())) and not _is_blank(d.get("contact")))
        for d in departments)
    if not has_it:
        gaps.append("контакт ИТ/цифровизации не выявлен (критичный недостающий контакт)")
        followups.append({"goal": "it",
                          "query": f"{name} директор по ИТ цифровизация информационные технологии начальник"})

    # 3b) ИТ-ландшафт: внедрённые системы/вендоры (TAdviser) — база под оффер LLM/RAG
    it_landscape = findings.get("it_landscape") or []
    has_it_landscape = bool(it_landscape)
    if not has_it_landscape:
        gaps.append("ИТ-ландшафт (внедрённые системы, вендоры, годы) не выявлен")
        followups.append({"goal": "it_landscape",
                          "query": f"site:tadviser.ru {_core_name(name)}"})
        followups.append({"goal": "it_landscape",
                          "query": f"{name} tadviser внедрение ERP CRM ИТ-система проект"})

    # 4) соцсети
    has_social = bool(contacts.get("social"))
    if not has_social:
        gaps.append("официальные соцсети (Telegram/ВКонтакте) не найдены")
        followups.append({"goal": "social",
                          "query": f"{name} официальная группа ВКонтакте Telegram канал"})

    # 5) руководство сверх первого лица
    if len(leadership) < 2:
        gaps.append("руководство центрального аппарата (замы/гл. инженер) публично не раскрыто")
        followups.append({"goal": "leadership",
                          "query": f"{name} заместитель генерального директора главный инженер руководство"})

    # 6) экосистема/вертикаль принятия решений: учредитель/собственник, курирующее
    #    ведомство, сестринские структуры, комиссии/советы, холдинг (критично для гос)
    ecosystem = findings.get("ecosystem") or []
    has_ecosystem = bool(ecosystem)
    if not has_ecosystem:
        gaps.append("вертикаль/экосистема (учредитель, курирующее ведомство, сестринские структуры) не выявлена")
        followups.append({"goal": "ecosystem",
                          "query": f"{name} учредитель кому принадлежит подведомственность"})
        followups.append({"goal": "ecosystem",
                          "query": f"{name} курирующее министерство ведомство холдинг группа компаний"})

    counts = {"branches_total": b_total, "branches_filled": b_filled,
              "leadership": len(leadership), "departments": len(departments),
              "has_tender": has_tender, "has_it": has_it, "has_social": has_social,
              "has_it_landscape": has_it_landscape, "it_systems": len(it_landscape),
              "has_ecosystem": has_ecosystem, "ecosystem": len(ecosystem),
              "emails": len(contacts.get("emails") or []),
              "social": len(contacts.get("social") or []), "gaps": len(gaps)}
    return {"gaps": gaps, "counts": counts, "followups": followups}


# ============================================================================
# LLM-ЭКСТРАКТ (sonnet) — превращает сырые страницы в структурный JSON находок
# ============================================================================
_EXTRACT_SYSTEM = (
    "Ты — извлекатель структурированных фактов из веб-страниц российской компании "
    "для B2B-пресейла. На входе — текст реально загруженных страниц, КАЖДАЯ помечена "
    "строкой [URL: ...]. Извлекай ТОЛЬКО то, что дословно присутствует в тексте: ФИО, "
    "должности, телефоны, e-mail, адреса, соцсети, филиалы и их директоров, контактных "
    "лиц закупок, проектный институт, суды/СМИ, ИТ-ЛАНДШАФТ (внедрённые ИТ-системы: "
    "название системы, вендор/интегратор, год внедрения — на страницах TAdviser это "
    "таблицы проектов), а также ЭКОСИСТЕМУ — связи вертикали "
    "принятия решений: учредитель/собственник, курирующее ведомство/министерство, "
    "головные/материнские и сестринские организации, комиссии/советы, членство в "
    "группах/холдингах (relation — тип связи, person — ключевое лицо, если названо). "
    "НИЧЕГО не выдумывай и не достраивай по "
    "догадке. У КАЖДОЙ строки поле source — URL страницы, откуда факт взят. Если факта "
    "нет в тексте — НЕ создавай строку. Верни СТРОГО JSON без прозы и без ```-ограждений, "
    "по схеме:\n"
    '{"leadership":[{"position":str,"fio":str,"source":str}],'
    '"branches":[{"branch":str,"director":str,"phone":str,"source":str}],'
    '"departments":[{"block":str,"contact":str,"relevance":str,"source":str}],'
    '"ecosystem":[{"entity":str,"relation":str,"person":str,"note":str,"source":str}],'
    '"it_landscape":[{"system":str,"vendor":str,"year":str,"note":str,"source":str}],'
    '"contacts":{"phones":[{"phone":str,"source":str}],'
    '"emails":[{"email":str,"role":"tender|general|personal","source":str}],'
    '"social":[{"kind":str,"url":str,"source":str}],"address":str,"schedule":str},'
    '"procurement":{"summary":str,"contacts":[{"fio":str,"email":str,"phone":str,"source":str}],"source":str},'
    '"project_institute":{"name":str,"address":str,"note":str,"source":str},'
    '"courts":[{"summary":str,"source":str}],"media":[{"summary":str,"source":str}]}'
)


def _extract_json(text):
    if not text:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    cand = fenced.group(1) if fenced else text
    try:
        return json.loads(cand)
    except Exception:
        pass
    m = re.search(r"\{.*\}", cand, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def _keyword_windows(text, keywords=KEYWORDS, window=700, max_chars=DR_PAGE_CHARS):
    """Для больших страниц — оставить только окна вокруг ключевых слов (экономия токенов)."""
    if len(text) <= max_chars:
        return text
    low = text.lower()
    spans = []
    for kw in keywords:
        start = 0
        kwl = kw.lower()
        while True:
            i = low.find(kwl, start)
            if i < 0:
                break
            spans.append((max(0, i - window), min(len(text), i + len(kw) + window)))
            start = i + len(kw)
    if not spans:
        return text[:max_chars]
    spans.sort()
    merged = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out, total = [], 0
    for s, e in merged:
        chunk = text[s:e]
        out.append(chunk)
        total += len(chunk)
        if total >= max_chars:
            break
    return " … ".join(out)


def _pages_blob(pages, max_total=24000):
    parts, total = [], 0
    for pg in pages or []:
        body = _keyword_windows(pg.get("markdown", "") or "")
        block = f"[URL: {pg.get('url', '')}]\n{body}"
        if total + len(block) > max_total:
            block = block[: max(0, max_total - total)]
        parts.append(block)
        total += len(block)
        if total >= max_total:
            break
    return "\n\n----\n\n".join(parts)


async def _kimi_extract(system, prompt):
    """LLM-экстракт через Kimi (OpenAI-совместимый шлюз, как у писателя). '' при сбое/без ключа.
    Переиспользует env-хелперы writer_kimi (ключ/endpoint/модель) — единый источник настроек Kimi."""
    try:
        import writer_kimi as WK
        from openai import AsyncOpenAI
    except Exception as e:                          # noqa: BLE001
        _note(f"Kimi недоступен для LLM-экстракта: {e}")
        return ""
    key = WK.kimi_key()
    if not key:
        _note("нет ключа Kimi (KIMI_API_KEY/GPLLM_API_KEY) для LLM-экстракта")
        return ""
    model = WK.kimi_model("kimi")
    timeout = float(os.environ.get("DR_LLM_TIMEOUT", "300"))
    max_tokens = int(os.environ.get("DR_KIMI_MAX_TOKENS", "8000"))
    client = AsyncOpenAI(base_url=WK.kimi_base_url(), api_key=key, timeout=timeout, max_retries=0)
    try:
        async with _sem("llm", DR_LLM_CONCURRENCY):
            for attempt in (1, 2):                  # 2-я попытка — без response_format (шлюз мог не принять)
                kwargs = {
                    "model": model,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": prompt}],
                    "max_tokens": max_tokens, "temperature": 0.2,
                }
                if attempt == 1:
                    kwargs["response_format"] = {"type": "json_object"}
                try:
                    r = await client.chat.completions.create(**kwargs)
                    return r.choices[0].message.content or ""
                except Exception as e:              # noqa: BLE001 — сеть/шлюз/лимиты
                    if attempt == 1:
                        continue
                    _note(f"Kimi LLM-экстракт не удался: {str(e)[:80]}")
                    return ""
    finally:
        await client.close()
    return ""


async def llm_extract(pages, focus="", model=EXTRACT_MODEL):
    """Сырые страницы -> структурный JSON находок. Провайдер — DR_LLM_PROVIDER
    ('claude' через claude-agent-sdk, либо 'kimi'). {} при недоступности/сбое."""
    if not DR_USE_LLM or not pages:
        return {}
    blob = _pages_blob(pages)
    if not blob.strip():
        return {}
    prompt = (f"Источник: {focus}. Извлеки факты из загруженных страниц ниже в JSON по схеме "
              f"из системного промпта. Особое внимание: филиалы+их директора+телефоны, "
              f"контактные лица закупок, соцсети, ИТ/цифровизация, ИТ-ландшафт "
              f"(системы/вендоры/годы внедрения — особенно на страницах tadviser.ru), проектный институт, "
              f"вертикаль/экосистема (учредитель, ведомство, сестринские структуры, комиссии).\n\n{blob}")
    # Kimi-путь: тот же промпт, но через OpenAI-совместимый шлюз — весь пайплайн без Anthropic.
    if DR_LLM_PROVIDER == "kimi":
        return _extract_json(await _kimi_extract(_EXTRACT_SYSTEM, prompt))
    try:
        from claude_agent_sdk import (query, ClaudeAgentOptions, AssistantMessage,
                                      TextBlock, ResultMessage)
    except Exception as e:
        _note(f"claude-agent-sdk недоступен для LLM-экстракта: {e}")
        return {}
    opts = ClaudeAgentOptions(
        model=model, system_prompt=_EXTRACT_SYSTEM, max_turns=1,
        allowed_tools=[], disallowed_tools=["Bash", "Edit", "Write", "NotebookEdit",
                                            "WebSearch", "WebFetch"],
        permission_mode="bypassPermissions", setting_sources=[])
    text = ""
    try:
        async with _sem("llm", DR_LLM_CONCURRENCY):
            async for m in query(prompt=prompt, options=opts):
                if isinstance(m, AssistantMessage):
                    for b in m.content:
                        if isinstance(b, TextBlock):
                            text += b.text
                elif isinstance(m, ResultMessage):
                    if getattr(m, "result", None):
                        text = m.result
    except Exception as e:
        _note(f"LLM-экстракт ({focus}) не удался: {str(e)[:80]}")
        return {}
    return _extract_json(text)


# ============================================================================
# ЗАМЕТКИ (для грейсфул-деградации — печатаем и складываем в документ)
# ============================================================================
_NOTES = []


def _note(msg):
    line = f"[deep_research] {msg}"
    print(line)
    _NOTES.append(msg)


# ============================================================================
# КОЛЛЕКТОРЫ
# ============================================================================
def official_lookup(name, inn):
    """Обёртка над CRA.research_company (структурные API) -> (payload, markdown)."""
    import company_research_agent as CRA  # ленивый импорт — рвём цикл и держим логику standalone
    payload = CRA.research_company(name, inn, "")
    md = CRA._render_official(payload)
    return payload, md


def _root_url(u):
    p = urlparse(u if u.startswith("http") else "http://" + u)
    return f"{p.scheme}://{p.netloc}" if p.netloc else ""


_OPF_WORDS = ("акционерное", "общество", "компания", "группа", "завод", "комбинат",
              "предприятие", "ооо", "оао", "зао", "пао", "нао", "гуп", "муп", "фгуп")

# «Родовые» слова названий: сами по себе НЕ доказывают принадлежность страницы компании
# («центр» — подстрока «Центрального банка», на этом движок однажды принял cbr.ru за сайт
# «Центра информационных технологий»). Матчатся только в составе ПОЛНОЙ фразы названия.
_GENERIC_NAME_WORDS = frozenset((
    "центр", "информационных", "информационные", "технологий", "технологии",
    "технологический", "республики", "республика", "российской", "россии",
    "российское", "государственное", "государственный", "национальный", "научно",
    "производственное", "производственный", "объединение", "управление", "служба",
    "агентство", "институт", "корпорация", "холдинг", "строительство", "развития"))


def _name_tokens(name):
    """Значимые токены названия: слова ≥5 букв без организационно-правовых форм."""
    return [t for t in re.split(r"[^а-яёa-z0-9]+", name.lower())
            if len(t) >= 5 and t not in _OPF_WORDS]


def _phrase_in(toks, text):
    """Полная фраза названия (токены подряд, по границе слова) присутствует в тексте."""
    return re.search(r"\b" + r"\s+".join(re.escape(t) for t in toks), text) is not None


def _text_belongs(txt, name, inn):
    """Текст страницы принадлежит ИМЕННО этой компании? Сигналы (по убыванию силы):
    ИНН на странице; ОТЛИЧИТЕЛЬНЫЙ токен названия по границе слова; для названий
    целиком из родовых слов — только ПОЛНАЯ фраза названия."""
    txt_l = (txt or "").lower()
    if inn and len(_digits(inn)) >= 10 and _digits(inn) in _digits(txt):
        return True
    toks = _name_tokens(name)
    distinctive = [t for t in toks if t not in _GENERIC_NAME_WORDS]
    if distinctive:
        return any(re.search(r"\b" + re.escape(t), txt_l) for t in distinctive)
    if toks:  # имя целиком «родовое» («Центр информационных технологий») — нужна вся фраза
        return _phrase_in(toks, txt_l)
    return False


async def _domain_matches(domain, name, inn):
    """Проверка, что найденный поиском домен — ДЕЙСТВИТЕЛЬНО этой компании (анти-garbage:
    rate-limited SERP может вернуть чужой сайт). Сигнал: ИНН на сайте ИЛИ токен названия."""
    page = await fetch_page(domain)
    if not page:
        return False
    return _text_belongs(page.get("markdown") or "", name, inn)


def _host_key(url):
    return urlparse(url).netloc.lower().removeprefix("www.")


async def _domain_matches_strict(root, name, inn):
    """Строгая проверка ДОПОЛНИТЕЛЬНОГО домена: токена названия мало (им «болеют» и СМИ,
    и каталоги) — требуем ИНН на главной либо на типовой странице контактов/реквизитов."""
    if not _digits(inn):
        return False
    for path in ("", "/contacts", "/kontakty", "/rekvizity", "/about"):
        page = await fetch_page(root + path)
        if page and _digits(inn) in _digits(page.get("markdown") or ""):
            return True
    return False


async def _serp_phrase_fallback(root, name, serp_hit):
    """Кандидат не отдаётся обычному HTTP (портал за анти-ботом, напр. *.tatarstan.ru) —
    принять по СНИППЕТУ выдачи, если в нём ПОЛНАЯ фраза названия из ≥2 слов (одиночный
    токен матчит и новостные сайты — не доказательство). Краул потом сделает Crawl4AI,
    которому анти-бот не мешает.
    Защита от каталогов не из блок-листа: карточка-агрегатор живёт на ГЛУБОКОМ пути
    с query (`/entity?query=...`), настоящий сайт/портал — на корне или /page.htm;
    глубокие URL и URL с параметрами не принимаем."""
    hit_url = urlparse(serp_hit.get("url", ""))
    if hit_url.query or len([s for s in hit_url.path.split("/") if s]) > 1:
        return False
    toks = _name_tokens(name)
    if len(toks) < 2:
        return False
    blob = f"{serp_hit.get('title', '')} {serp_hit.get('snippet', '')}".lower()
    if not _phrase_in(toks, blob):
        return False
    return await fetch_page(root) is None


async def discover_domains(name, inn, card, contacts, hint="", limit=None):
    """До DR_MAX_DOMAINS ПОДТВЕРЖДЁННЫХ сайтов компании (у госструктур их часто 2-3:
    свой сайт + страница на ведомственном портале — на них живут РАЗНЫЕ данные).
    Первый домен — по прежним правилам (hint ФАЗЫ 1 -> Checko/Dadata -> валидированный
    поиск: недоступный берём как есть, доступный без ИНН/названия отклоняем);
    ДОПОЛНИТЕЛЬНЫЕ — строго: ИНН на странице либо полная фраза названия в SERP-сниппете
    (порталы за анти-ботом), чтобы не притащить СМИ/каталог."""
    limit = limit or DR_MAX_DOMAINS
    domains = []

    def _seen(root):
        return _host_key(root) in {_host_key(d) for d in domains}

    def _accept(root, why):
        _note(why)
        domains.append(root)

    # 1) известные сайты (лид ФАЗЫ 1 / Checko / Dadata) — с валидацией принадлежности
    for src, cand in (("ФАЗА 1", hint),
                      ("contacts", (contacts or {}).get("website")),
                      ("card", (card or {}).get("website"))):
        cand = (cand or "").strip()
        if not (cand and is_own_site(cand) and _not_search_engine(cand)):
            continue
        root = _root_url(cand)
        if _seen(root):
            continue
        page = await fetch_page(root)
        if not page:
            _accept(root, f"известный домен {root} ({src}) недоступен для проверки — беру как есть")
        elif _text_belongs(page.get("markdown") or "", name, inn):
            _accept(root, f"домен из известных данных подтверждён ({src}): {root}")
        else:
            _note(f"известный домен {root} ({src}) ОТКЛОНЁН — на сайте нет ни ИНН, ни названия «{name}»; ищу правильный")
        if len(domains) >= limit:
            return domains
    # 2) поиск (best-effort) с валидацией — чтобы зарейтлимиченный SERP не подсунул чужой
    #    сайт; дополнительные (сверх первого) домены — строго ИНН либо SERP-фраза (анти-бот)
    for q in (f"{name} ИНН {inn} официальный сайт".strip(), f"{name} официальный сайт"):
        for r in await web_search(q, 6):
            u = r["url"]
            host = urlparse(u).netloc.lower()
            if not (is_own_site(u) and _not_search_engine(u)
                    and not any(a in host for a in AGGREGATORS)):
                continue
            root = _root_url(u)
            if _seen(root):
                continue
            if not domains:
                if await _domain_matches(root, name, inn):
                    _accept(root, f"домен найден и подтверждён поиском: {root}")
                elif await _serp_phrase_fallback(root, name, r):
                    _accept(root, f"домен принят по фразе названия в SERP (страница не отдаётся HTTP): {root}")
                else:
                    _note(f"кандидат {root} отклонён (не подтвердил принадлежность компании)")
            elif await _domain_matches_strict(root, name, inn):
                _accept(root, f"дополнительный домен подтверждён по ИНН: {root}")
            elif await _serp_phrase_fallback(root, name, r):
                _accept(root, f"дополнительный домен принят по фразе названия в SERP (анти-бот): {root}")
            if len(domains) >= limit:
                return domains
    if not domains:
        _note(f"официальный сайт «{name}» не определён — сайт-коллектор ограничен")
    return domains


async def discover_domain(name, inn, card, contacts, hint=""):
    """Совместимость: первый (основной) подтверждённый домен либо ''."""
    ds = await discover_domains(name, inn, card, contacts, hint=hint, limit=1)
    return ds[0] if ds else ""


class SiteCrawler:
    """Краул разделов сайта: Crawl4AI BestFirst (PRIMARY) -> HTTP-фолбэк."""

    def __init__(self, max_pages=DR_MAX_PAGES, max_depth=2):
        self.max_pages = max_pages
        self.max_depth = max_depth

    async def crawl_sections(self, domain):
        if not domain:
            return []
        if not domain.startswith("http"):
            domain = "http://" + domain
        pages = []
        if os.environ.get("DR_USE_CRAWL4AI", "1") not in ("0", "false", "no"):
            try:
                pages = await self._crawl4ai(domain)
                if pages:
                    _note(f"Crawl4AI: собрано {len(pages)} страниц с {urlparse(domain).netloc}")
            except Exception as e:
                _note(f"Crawl4AI недоступен/упал ({str(e)[:70]}) — HTTP-фолбэк")
                pages = []
        if not pages:
            pages = await self._http_fallback(domain)
            _note(f"HTTP-фолбэк: собрано {len(pages)} страниц с {urlparse(domain).netloc}")
        # гарантируем главную (для Tilda-сайтов весь контент именно там)
        root = domain.rstrip("/")
        if not any(urlparse(p["url"]).path in ("", "/") for p in pages):
            r = await fetch_page(root)
            if r:
                pages.insert(0, r)
        return pages[: self.max_pages]

    async def _crawl4ai(self, domain):
        from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, BrowserConfig
        from crawl4ai.deep_crawling import BestFirstCrawlingStrategy
        from crawl4ai.deep_crawling.scorers import KeywordRelevanceScorer
        scorer = KeywordRelevanceScorer(keywords=KEYWORDS, weight=0.8)
        strategy = BestFirstCrawlingStrategy(
            max_depth=self.max_depth, max_pages=self.max_pages,
            include_external=False, url_scorer=scorer)
        cfg = CrawlerRunConfig(deep_crawl_strategy=strategy, stream=True, verbose=False)
        out = []
        async with AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False)) as c:
            result = await c.arun(domain, config=cfg)
            if hasattr(result, "__aiter__"):
                async for r in result:
                    self._absorb(r, out)
            else:
                for r in (result if isinstance(result, list) else [result]):
                    self._absorb(r, out)
        return out

    @staticmethod
    def _absorb(r, out):
        if not getattr(r, "success", True):
            return
        md = getattr(r, "markdown", "") or ""
        if hasattr(md, "raw_markdown"):
            md = md.raw_markdown or getattr(md, "fit_markdown", "") or ""
        md = str(md)
        if md.strip():
            out.append({"url": getattr(r, "url", ""), "markdown": md, "source": "site:crawl4ai"})

    async def _http_fallback(self, domain):
        host = urlparse(domain).netloc
        root = domain.rstrip("/")
        pages, seen = [], set()
        r = await fetch_page(root)
        if r:
            pages.append(r)
            seen.add(root)
        # подстраницы через поисковик (для не-Tilda — реально индексируемые разделы)
        discovered = []
        for q in (f"site:{host} филиалы руководство контакты",
                  f"{host} руководство директор отдел закупки",
                  f"{host} проектный институт структура"):
            for res in await web_search(q, 5):
                u = res["url"]
                if host in urlparse(u).netloc and u.rstrip("/") not in seen:
                    seen.add(u.rstrip("/"))
                    discovered.append(u)
        # типовые пути
        for p in FALLBACK_PATHS:
            u = root + p
            if u.rstrip("/") not in seen:
                seen.add(u.rstrip("/"))
                discovered.append(u)
        # фетчим с капом конкуренции
        sem = _sem("crawl", DR_CRAWL_CONCURRENCY)

        async def _one(u):
            async with sem:
                return await fetch_page(u)

        got = await asyncio.gather(*[_one(u) for u in discovered[: self.max_pages]])
        pages += [g for g in got if g]
        return pages


async def collect_site(domains):
    """Краул ВСЕХ подтверждённых доменов: основной — полный кап страниц,
    дополнительные (ведомственный портал и т.п.) — половинный."""
    domains = [d for d in (domains or []) if d]
    pages = []
    for i, d in enumerate(domains):
        cap = DR_MAX_PAGES if i == 0 else max(6, DR_MAX_PAGES // 2)
        pages += await SiteCrawler(max_pages=cap).crawl_sections(d)
    notes = [] if domains else ["сайт компании не определён"]
    return {"source": "site", "pages": pages, "notes": notes, "links": list(domains)}


async def eis_by_inn(inn):
    """ЕИС/zakupki по ИНН — BEST-EFFORT, НЕблокирующий. Никогда не роняет пайплайн.
    Антибот: даём ссылки на выдачу по ИНН + поисковую выдачу; карточки парсим
    только если задан FIRECRAWL_API_KEY (опц.)."""
    inn = _digits(inn)
    res = {"source": "eis", "pages": [], "notes": [], "links": []}
    if not inn:
        res["notes"].append("ИНН не задан — ЕИС пропущен")
        return res
    search_44_223 = ("https://zakupki.gov.ru/epz/order/extendedsearch/results.html?"
                     + urllib.parse.urlencode({"searchString": inn, "morphology": "on",
                                               "fz44": "on", "fz223": "on"}))
    org_card = ("https://zakupki.gov.ru/epz/organization/search/results.html?"
                + urllib.parse.urlencode({"searchString": inn}))
    res["links"] = [search_44_223, org_card]

    # A) опциональный Firecrawl за ключом — единственный надёжный способ снять антибот-карточку
    fc = os.environ.get("FIRECRAWL_API_KEY")
    if fc:
        try:
            page = await asyncio.to_thread(_firecrawl_scrape, search_44_223, fc)
            if page:
                res["pages"].append(page)
                _note("ЕИС: карточка снята через Firecrawl")
        except Exception as e:
            res["notes"].append(f"Firecrawl ЕИС не сработал: {str(e)[:60]}")

    # B) поисковая выдача (сниппеты часто содержат контактных лиц закупок из извещений)
    for q in (f"{inn} zakupki.gov.ru контактное лицо",
              f"{inn} извещение закупка контактное лицо email телефон",
              f"{inn} clearspending госзакупки"):
        for r in await web_search(q, 5):
            res["pages"].append({"url": r["url"],
                                 "markdown": f"{r['title']}\n{r['snippet']}", "source": "eis:search"})

    res["notes"].append(
        "ЕИС zakupki.gov.ru имеет антибот-защиту: карточки извещений напрямую HTTP не "
        "снимаются. Даны ссылки на выдачу по ИНН (44/223-ФЗ) и карточку организации; "
        "контактные лица закупок взяты из поисковой выдачи извещений. Полное извлечение — "
        "за FIRECRAWL_API_KEY/browser-use (опц.).")
    return res


def _firecrawl_scrape(url, key):
    """Опц.: снять страницу через Firecrawl API. None при сбое."""
    body = json.dumps({"url": url, "formats": ["markdown"], "onlyMainContent": True}).encode()
    req = urllib.request.Request("https://api.firecrawl.dev/v1/scrape", data=body,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    md = (((data or {}).get("data") or {}).get("markdown")) or ""
    return {"url": url, "markdown": md, "source": "eis:firecrawl"} if md else None


async def collect_courts_media(name, inn):
    """Суды (арбитраж/kad.arbitr/sudact) + СМИ за последние годы. Сниппеты + топ-страницы."""
    pages, links = [], []
    queries = [f"{name} арбитражный суд дело",
               f"{name} прокуратура ФАС нарушение контракт",
               f"{name} новости суд иск {inn}".strip()]
    fetch_urls = []
    for q in queries:
        for r in await web_search(q, 5):
            pages.append({"url": r["url"], "markdown": f"{r['title']}\n{r['snippet']}",
                          "source": "courts_media:search"})
            host = urlparse(r["url"]).netloc.lower()
            if any(k in host for k in ("kad.arbitr", "sudact", "pravo.ru", "info", "ru")) \
                    and len(fetch_urls) < 3 and "zakupki.gov" not in host:
                fetch_urls.append(r["url"])
    sem = _sem("crawl", DR_CRAWL_CONCURRENCY)

    async def _one(u):
        async with sem:
            return await fetch_page(u)

    for g in await asyncio.gather(*[_one(u) for u in fetch_urls]):
        if g:
            pages.append({**g, "source": "courts_media:page"})
    return {"source": "courts_media", "pages": pages, "notes": [], "links": links}


TADVISER_HOST = "https://www.tadviser.ru"
_TADVISER_LINK_RE = re.compile(r'href="(/index\.php/(?:%D0%9A%D0%BE%D0%BC%D0%BF%D0%B0%D0%BD%D0%B8%D1%8F'
                               r'|%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82|Компания|Проект)[^"#]*)"', re.I)
_OPF_PREFIX_RE = re.compile(r"\b(ПАО|ОАО|ЗАО|АО|ООО|НАО|ГУП|МУП|ФГУП|ФГБУ|ФКУ|ГБУ|МБУ|АНО|ПК|НПО|НПП)\b\.?",
                            re.I)


# Несуществующая статья TAdviser отдаёт HTTP 200 и ПЕЧАТАЕТ запрошенное название в шапке —
# _text_belongs на ней проходит. Отличаем заглушку по тексту и объёму (реальная карточка
# компании — десятки КБ; заглушка — ~5 КБ голой навигации).
_TADVISER_STUB_RE = re.compile(r"недоступна\s+для\s+прос|стать[яи]\s+не\s+найдена", re.I)
_TADVISER_MIN_CHARS = 5500  # замер: заглушка 5009-5022, реальная статья-проект 7318+, карточка 51486


def _tadviser_is_stub(txt):
    t = txt or ""
    return bool(_TADVISER_STUB_RE.search(t)) or len(t) < _TADVISER_MIN_CHARS


def _core_name(name):
    """«ПАО "Алтай-Кокс"» -> «Алтай-Кокс»: без ОПФ и кавычек (заголовки статей TAdviser)."""
    s = re.sub(r"[«»\"'`]", " ", str(name or ""))
    s = _OPF_PREFIX_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip(" -–—,")


def _tadviser_url(title):
    return TADVISER_HOST + "/index.php/" + urllib.parse.quote(title.replace(" ", "_"))


def _tadviser_title_matches(link, core):
    """Заголовок статьи содержит ВСЕ значимые токены названия (поиск TAdviser нечёткий:
    по «Алтай-Кокс» он выдаёт «Республика Алтай», «Алтай-Кабель» и т.п.)."""
    title = urllib.parse.unquote(link).split("/index.php/", 1)[-1]
    title = title.split(":", 1)[-1].replace("_", " ").lower()
    toks = [t for t in re.split(r"[^а-яёa-z0-9]+", core.lower()) if len(t) >= 4]
    return bool(toks) and all(t in title for t in toks)


async def collect_tadviser(name, inn):
    """TAdviser (tadviser.ru) — ИТ-ландшафт компании: внедрённые системы, вендоры/интеграторы,
    ИТ-проекты по годам, ИТ-руководители. Ключевой источник под оффер LLM/RAG/ИИ-агентов.
    Путь: прямая статья «Компания:<имя>» -> внутренний поиск вики -> веб-поиск site:tadviser.ru.
    BEST-EFFORT: никогда не роняет пайплайн."""
    core = _core_name(name)
    if not core:
        return {"source": "tadviser", "pages": [], "notes": ["TAdviser: пустое название"], "links": []}
    pages, links, notes = [], [], []
    sem = _sem("crawl", DR_CRAWL_CONCURRENCY)

    async def _get(u, src):
        async with sem:
            g = await fetch_page(u)
        if not g:
            return False
        txt = g.get("markdown") or ""
        if _tadviser_is_stub(txt) or not _text_belongs(txt, name, inn):
            return False
        pages.append({**g, "source": src})
        links.append(g.get("url") or u)
        return True

    # 1) прямая статья компании
    got = await _get(_tadviser_url("Компания:" + core), "tadviser:company")

    # 2) внутренний поиск вики (нечёткий -> фильтруем заголовки по всем токенам названия)
    cand_company, cand_project = [], []
    search_url = TADVISER_HOST + "/index.php?fulltext=1&search=" + urllib.parse.quote(core)
    _, html = await asyncio.to_thread(_fetch_sync, search_url, 15)
    for href in dict.fromkeys(_TADVISER_LINK_RE.findall(html or "")):
        if not _tadviser_title_matches(href, core):
            continue
        u = TADVISER_HOST + href
        if u in links:
            continue
        (cand_company if "%D0%9A" in href or "Компания" in href else cand_project).append(u)

    if not got:
        for u in cand_company[:2]:
            got = await _get(u, "tadviser:company") or got
    # 3) статьи-проекты («<Компания> (<ИТ-система>)») — это и есть перечень внедрений
    for u in cand_project[:3]:
        await _get(u, "tadviser:project")

    # 4) фолбэк: веб-поиск по домену. ТОЛЬКО как источник URL — сниппеты в находки не идут.
    #    Заголовок статьи обязан содержать все токены названия: _text_belongs здесь бессилен
    #    (он пропускает по ЛЮБОМУ отличительному токену, а на 700-КБ обзоре TAdviser найдётся
    #    и «алтай», и «промресурс» — проверено, обе страницы были чужими).
    if not pages:
        for r in (await web_search(f"site:tadviser.ru {core}", 5))[:3]:
            path = urlparse(r["url"]).path
            if "tadviser.ru" in urlparse(r["url"]).netloc.lower() \
                    and _tadviser_title_matches(path, core):
                await _get(r["url"], "tadviser:page")

    if not pages:
        notes.append("TAdviser: статья о компании не найдена (ИТ-ландшафт из tadviser.ru не собран)")
    else:
        _note(f"TAdviser: собрано страниц {len(pages)}")
    return {"source": "tadviser", "pages": pages, "notes": notes, "links": links}


async def collect_hh(name, inn):
    """hh.ru: профиль работодателя -> численность/стек/оргструктура (сниппеты + страница)."""
    pages = []
    employer_url = ""
    for q in (f"{name} hh.ru работодатель вакансии", f"{name} вакансии численность"):
        for r in await web_search(q, 5):
            pages.append({"url": r["url"], "markdown": f"{r['title']}\n{r['snippet']}",
                          "source": "hh:search"})
            if "hh.ru/employer" in r["url"] and not employer_url:
                employer_url = r["url"]
    if employer_url:
        g = await fetch_page(employer_url)
        if g:
            pages.append({**g, "source": "hh:page"})
    return {"source": "hh", "pages": pages, "notes": [], "links": [employer_url] if employer_url else []}


# ============================================================================
# ДОБОР ПОД ПРОБЕЛЫ (петля dzhng/deep-research, адаптированная на целевой добор)
# ============================================================================
async def run_followups(followups, name, inn, domains):
    """Под каждый follow-up: таргетированный поиск + фетч топ-страниц. -> pages[]
    domains — список подтверждённых сайтов (или строка): их страницы дочитываются приоритетно."""
    pages = []
    sem = _sem("crawl", DR_CRAWL_CONCURRENCY)
    if isinstance(domains, str):
        domains = [domains] if domains else []
    hosts = [_host_key(d) for d in (domains or []) if d]

    async def _search_and_fetch(fu):
        local = []
        results = await web_search(fu["query"], 5)
        for r in results:
            local.append({"url": r["url"], "markdown": f"{r['title']}\n{r['snippet']}",
                          "source": f"followup:{fu['goal']}"})
        # приоритетно дочитываем страницы собственных сайтов, если они всплыли
        own = [r["url"] for r in results
               if hosts and any(h in urlparse(r["url"]).netloc.lower() for h in hosts)]
        for u in own[:2]:
            async with sem:
                g = await fetch_page(u)
            if g:
                local.append({**g, "source": f"followup:{fu['goal']}:page"})
        return local

    batches = await asyncio.gather(*[_search_and_fetch(fu) for fu in followups])
    for b in batches:
        pages.extend(b)
    return pages


# ============================================================================
# SUPERVISOR + КОНСОЛИДАЦИЯ
# ============================================================================
async def supervisor(name, inn, breadth=DR_BREADTH, depth=DR_DEPTH, domain_hint=""):
    # 0) официальная база (структурные API) — блокирующая, в поток
    try:
        payload, official_md = await asyncio.to_thread(official_lookup, name, inn)
    except Exception as e:
        _note(f"официальная база недоступна: {str(e)[:80]}")
        payload, official_md = {"card": {}, "contacts": {}, "inn": _digits(inn)}, ""
    card = payload.get("card") or {}
    contacts0 = payload.get("contacts") or {}
    inn = inn or payload.get("inn") or ""

    # 1) домены (hint из ФАЗЫ 1 -> Checko/Dadata -> поиск; до DR_MAX_DOMAINS сайтов)
    domains = await discover_domains(name, inn, card, contacts0, hint=domain_hint)
    domain = domains[0] if domains else ""

    # 2) параллельные коллекторы (по ИСТОЧНИКАМ)
    raws = await asyncio.gather(
        collect_site(domains), eis_by_inn(inn),
        collect_courts_media(name, inn), collect_hh(name, inn),
        collect_tadviser(name, inn),
        return_exceptions=True)
    raws = [r for r in raws if isinstance(r, dict)]
    all_pages = [pg for r in raws for pg in r.get("pages", [])]
    src_links = [l for r in raws for l in r.get("links", []) if l]
    collector_notes = [n for r in raws for n in r.get("notes", [])]

    # 3) синтез: регэксп (страховка) + per-source LLM-экстракт (sonnet), затем merge
    parts = [regex_findings(all_pages)]
    extracts = await asyncio.gather(
        *[llm_extract(r["pages"], r["source"]) for r in raws if r.get("pages")])
    parts += [e for e in extracts if e]
    findings = merge_findings(parts)

    # карточный адрес как фолбэк
    if not findings["contacts"]["address"] and card.get("address"):
        findings["contacts"]["address"] = card["address"]

    # 4) ПЕТЛЯ ЦЕЛЕВОГО ДОБОРА под незаполненные ячейки целевых таблиц
    rounds = 0
    opened_urls = {pg["url"] for pg in all_pages if pg.get("url")}
    while rounds < depth:
        crit = completeness_critic(findings, name, inn, domain)
        if not crit["followups"]:
            break
        fu = crit["followups"][:breadth]
        _note(f"добор раунд {rounds + 1}: пробелов {len(crit['gaps'])}, "
              f"follow-up {len(fu)} ({', '.join(sorted({f['goal'] for f in fu}))})")
        new_pages = await run_followups(fu, name, inn, domains)
        new_pages = [p for p in new_pages if p.get("url") not in opened_urls or p.get("source", "").endswith("page")]
        if not new_pages:
            break
        opened_urls |= {p["url"] for p in new_pages if p.get("url")}
        more = [regex_findings(new_pages), await llm_extract(new_pages, f"followup{rounds + 1}")]
        findings = merge_findings([findings] + [m for m in more if m])
        rounds += 1

    crit = completeness_critic(findings, name, inn, domain)
    return consolidate(name, inn, domains, official_md, findings, crit,
                       sorted(opened_urls), src_links, collector_notes, rounds)


def _fmt_branches(branches):
    if not branches:
        return "Филиалы/подразделения на открытых страницах не обнаружены.\n"
    out = ["| Филиал | Директор | Телефон | Источник |", "|---|---|---|---|"]
    for b in branches:
        out.append(f"| {b.get('branch', '')} | {b.get('director', '') or '—'} | "
                   f"{b.get('phone', '') or '—'} | {b.get('source', '')} |")
    return "\n".join(out) + "\n"


def consolidate(name, inn, domain, official_md, findings, crit,
                opened_urls, src_links, collector_notes, rounds):
    """Богатый ИСТОЧНИКОВАННЫЙ findings-документ: markdown + структурный JSON.
    domain — строка ИЛИ список подтверждённых доменов (мульти-домен)."""
    domains = list(domain) if isinstance(domain, (list, tuple)) else ([domain] if domain else [])
    c = findings.get("contacts") or {}
    counts = crit["counts"]
    L = []
    L.append(f"# Находки deep_research: {name}" + (f" (ИНН {inn})" if inn else ""))
    L.append(f"Движок: deep_research_engine (supervisor + целевой добор + краул). "
             f"Раундов добора: {rounds}. Сайт(ы): {', '.join(domains) or 'не определён'}.")
    L.append("")
    L.append("## Реально открытые источники (страницы/выдача загружены движком)")
    if opened_urls:
        for u in opened_urls[:40]:
            L.append(f"- {u}")
    else:
        L.append("- (веб-страницы открыть не удалось — см. примечания)")
    L.append("")

    if official_md:
        L.append("## Официальная база (структурные API: ГИР БО / Dadata / Checko)")
        L.append(official_md)
        L.append("")

    L.append("## Руководство (из открытых страниц)")
    if findings.get("leadership"):
        for r in findings["leadership"]:
            L.append(f"- {r.get('position', '')} — {r.get('fio', '')} "
                     f"[источник: {r.get('source', '')}]")
    else:
        L.append("- Сверх первого лица (ЕГРЮЛ) публично не раскрыто на открытых страницах.")
    L.append("")

    L.append(f"## Филиалы / обособленные подразделения "
             f"(заполнено {counts['branches_filled']}/{counts['branches_total']})")
    L.append(_fmt_branches(findings.get("branches")))

    L.append("## Профильные отделы и закупки")
    proc = findings.get("procurement") or {}
    if proc.get("summary"):
        L.append(f"- Закупки: {proc['summary']} [источник: {proc.get('source', '')}]")
    tender = [e for e in (c.get("emails") or []) if _looks_tender_email(e)]
    if tender:
        L.append("- Контакты закупок (e-mail из извещений/страниц): "
                 + "; ".join(f"{e['email']} [{e['source']}]" for e in tender[:8]))
    for pc in (proc.get("contacts") or [])[:8]:
        L.append(f"- Контактное лицо закупок: {pc.get('fio', '')} {pc.get('email', '')} "
                 f"{pc.get('phone', '')} [источник: {pc.get('source', '')}]")
    if findings.get("departments"):
        for d in findings["departments"]:
            L.append(f"- {d.get('block', '')}: {d.get('contact', '') or '—'} — "
                     f"{d.get('relevance', '')} [источник: {d.get('source', '')}]")
    L.append("")

    itl = findings.get("it_landscape") or []
    L.append("## ИТ-ландшафт: внедрённые системы, вендоры, годы (TAdviser и открытые источники)")
    if itl:
        L.append("| Система | Вендор / интегратор | Год | Примечание | Источник |")
        L.append("|---|---|---|---|---|")
        for x in itl[:20]:
            L.append(f"| {x.get('system', '')} | {x.get('vendor', '') or '—'} | "
                     f"{x.get('year', '') or '—'} | {x.get('note', '') or '—'} | {x.get('source', '')} |")
    else:
        L.append("- Внедрённые ИТ-системы на открытых страницах (вкл. tadviser.ru) не обнаружены.")
    L.append("")

    pi = findings.get("project_institute") or {}
    if pi.get("name"):
        L.append("## Проектный институт / профильное подразделение")
        L.append(f"- {pi.get('name', '')} {pi.get('address', '')} {pi.get('note', '')} "
                 f"[источник: {pi.get('source', '')}]")
        L.append("")

    eco = findings.get("ecosystem") or []
    if eco:
        L.append("## Экосистема и вертикаль принятия решений "
                 "(учредитель / ведомство / сестринские структуры / комиссии)")
        for x in eco[:12]:
            L.append(f"- {x.get('entity', '')} — {x.get('relation', '')}"
                     + (f" (ключевое лицо: {x.get('person', '')})" if x.get("person") else "")
                     + (f": {x.get('note', '')}" if x.get("note") else "")
                     + f" [источник: {x.get('source', '')}]")
        L.append("")

    L.append("## Официальные контакты")
    if c.get("phones"):
        L.append("- Телефоны: " + "; ".join(f"{p['phone']} [{p['source']}]" for p in c["phones"][:8]))
    if c.get("emails"):
        gen = [e for e in c["emails"] if e.get("role") != "tender"]
        L.append("- E-mail: " + "; ".join(f"{e['email']} ({e.get('role', '')}) [{e['source']}]"
                                           for e in (c["emails"])[:10]))
    if c.get("social"):
        L.append("- Соцсети: " + "; ".join(f"{s['kind']} {s['url']} [{s['source']}]"
                                           for s in c["social"]))
    if c.get("address"):
        L.append(f"- Адрес: {c['address']}")
    if c.get("schedule"):
        L.append(f"- График: {c['schedule']}")
    L.append("")

    if findings.get("courts"):
        L.append("## Суды / арбитраж")
        for x in findings["courts"][:8]:
            L.append(f"- {x.get('summary', '')} [источник: {x.get('source', '')}]")
        L.append("")
    if findings.get("media"):
        L.append("## СМИ")
        for x in findings["media"][:8]:
            L.append(f"- {x.get('summary', '')} [источник: {x.get('source', '')}]")
        L.append("")

    L.append("## ЕИС (госзакупки) по ИНН")
    for u in src_links:
        if "zakupki.gov" in u:
            L.append(f"- Выдача по ИНН: {u}")
    L.append("")

    # отчёт о полноте + якоря честности для писателя
    L.append("## Полнота целевых таблиц (для писателя документов)")
    L.append(f"- Филиалы: заполнено {counts['branches_filled']}/{counts['branches_total']} "
             f"(директор+телефон).")
    L.append(f"- Тендерный контакт: {'ЕСТЬ' if counts['has_tender'] else 'НЕ найден'}.")
    L.append(f"- ИТ/цифровизация: {'ЕСТЬ' if counts['has_it'] else 'НЕ выявлен (критичный пробел)'}.")
    L.append(f"- ИТ-ландшафт (TAdviser): "
             f"{'ЕСТЬ, систем ' + str(counts.get('it_systems', 0)) if counts.get('has_it_landscape') else 'НЕ выявлен'}.")
    L.append(f"- Соцсети: {'ЕСТЬ' if counts['has_social'] else 'НЕ найдены'}.")
    L.append(f"- Руководство сверх первого лица: строк {counts['leadership']}.")
    L.append(f"- Экосистема/вертикаль (учредитель/ведомство/сёстры-структуры): "
             f"{'ЕСТЬ, строк ' + str(counts.get('ecosystem', 0)) if counts.get('has_ecosystem') else 'НЕ выявлена'}.")
    if crit["gaps"]:
        L.append("- ОСТАВШИЕСЯ ПРОБЕЛЫ (после исчерпания бюджета добора):")
        for g in crit["gaps"]:
            L.append(f"   • {g}")
    L.append("ДИСЦИПЛИНА: ячейку можно оставить «не подтверждено» ТОЛЬКО потому, что "
             "соответствующий источник из списка выше реально открыт и факта там нет. "
             "Любое отрицание — со ссылкой на открытую страницу/выдачу. Выдумывать ФИО/тел/email запрещено.")
    if collector_notes or _NOTES:
        L.append("")
        L.append("## Примечания движка (грейсфул-деградация)")
        for n in dict.fromkeys(collector_notes + _NOTES[-12:]):
            L.append(f"- {n}")
    L.append("")
    L.append("## Структурные находки (JSON; у каждой строки source URL)")
    L.append("```json")
    L.append(json.dumps(findings, ensure_ascii=False, indent=1))
    L.append("```")
    return "\n".join(L)


# ============================================================================
# ПУБЛИЧНЫЙ ВХОД (зовётся тулом deep_research в company_research_agent.py)
# ============================================================================
def _domain_hint_from_aspects(aspects):
    """Вытащить подсказку-домен из aspects (ФАЗА 1 кладёт туда website лида)."""
    a = aspects or ""
    m = re.search(r"https?://[^\s,;]+", a)
    if m and is_own_site(m.group(0)):
        return m.group(0)
    m = re.search(r"\b([a-z0-9][a-z0-9\-]+\.(?:ru|рф|com|su|org|net)(?:/[^\s,;]*)?)", a.lower())
    if m and is_own_site(m.group(1)):
        return m.group(1)
    return ""


async def deep_research(company_name, inn="", aspects=""):
    """Запустить supervisor; вернуть богатый источникованный текст находок.
    НИКОГДА не роняет пайплайн: при сбое supervisor — официальная база."""
    _NOTES.clear()
    hint = _domain_hint_from_aspects(aspects)
    try:
        return await supervisor(company_name, _digits(inn), domain_hint=hint)
    except Exception as e:
        _note(f"supervisor аварийно завершился: {str(e)[:120]}")
        try:
            payload, md = await asyncio.to_thread(official_lookup, company_name, inn)
            return (md or f"# {company_name}") + (
                f"\n\n[deep_research_engine: сбой supervisor ({str(e)[:80]}); "
                f"вернулась только официальная база. Веб-сбор не выполнен.]")
        except Exception as e2:
            return (f"# {company_name}\n[deep_research_engine недоступен: {str(e)[:80]} / "
                    f"{str(e2)[:60]}]. Используй WebSearch/WebFetch вручную.")


# ============================================================================
# CLI: дымовой тест логики (без сети) + опц. реальный прогон по домену/компании
# ============================================================================
async def _amain():
    import argparse
    ap = argparse.ArgumentParser(description="deep_research_engine — движок и его тесты")
    ap.add_argument("--company", help="реальный прогон supervisor: имя компании")
    ap.add_argument("--inn", default="", help="ИНН для --company")
    ap.add_argument("--site", default="", help="подсказка-домен (как website из ФАЗЫ 1, минует поиск)")
    ap.add_argument("--crawl", help="только SiteCrawler.crawl_sections(<домен>)")
    a = ap.parse_args()
    if a.crawl:
        pages = await SiteCrawler().crawl_sections(a.crawl)
        print(f"\n=== crawl_sections({a.crawl}) -> {len(pages)} страниц ===")
        for p in pages:
            print(f"  {p['source']:16} {len(p['markdown']):6} симв  {p['url']}")
        f = regex_findings(pages)
        print(f"\nрегэксп: филиалов {len(f['branches'])}, соцсетей {len(f['contacts']['social'])}, "
              f"email {len(f['contacts']['emails'])}")
        for b in f["branches"]:
            print(f"  • {b['branch']:20} | {b['director']:30} | {b['phone']}")
        for s in f["contacts"]["social"]:
            print(f"  соцсеть: {s['kind']} {s['url']}")
        return
    if a.company:
        aspects = f"сайт: {a.site}" if a.site else ""
        text = await deep_research(a.company, a.inn, aspects)
        print(text)
        return
    print("Укажи --crawl <домен> или --company <имя> [--inn] [--site <домен>]. "
          "Дымовой тест логики: py test_deep_research.py")


if __name__ == "__main__":
    asyncio.run(_amain())
