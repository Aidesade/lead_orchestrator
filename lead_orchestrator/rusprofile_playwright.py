# -*- coding: utf-8 -*-
"""Playwright-сессия RusProfile для Фазы 1.

Одна и та же авторизованная browser-context умеет:
  * вызвать внутренний advanced-search с серверным фильтром выручки;
  * открыть только выбранные карточки и извлечь контакты с cookie платного аккаунта.

Cookie-значения никогда не печатаются. Файл задаётся через
``RUSPROFILE_COOKIES_FILE`` либо берётся из ``rusprofile_session.COOKIES_FILE``.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from pathlib import Path

from rusprofile_session import (
    COOKIES_FILE,
    PAYWALL,
    TEST_CARD,
    _MASK,
    _REAL_MAIL,
    _REAL_TEL,
    RusProfileAuth,
)

ADV_URL = "https://www.rusprofile.ru/search-advanced"
MAX_SEARCH_PAGES = 20


def _canonical_profile_url(link, section):
    """Канонический URL разрешённого раздела на ожидаемом origin."""
    if section not in ("id", "founders"):
        raise ValueError("неподдерживаемый раздел RusProfile")
    raw = str(link or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme or parsed.netloc:
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443)
                or host not in ("rusprofile.ru", "www.rusprofile.ru")):
            raise RusProfilePlaywrightError("ссылка карточки ведёт вне rusprofile.ru")
    if (parsed.query or parsed.fragment
            or not re.fullmatch(rf"/{section}/\d+", parsed.path or "")):
        raise RusProfilePlaywrightError("неканоническая ссылка RusProfile")
    return "https://www.rusprofile.ru" + parsed.path


def canonical_card_url(link):
    """Только каноническая карточка /id/<number> на ожидаемом origin."""
    return _canonical_profile_url(link, "id")

_SEARCH_XHR = r"""
async arg => {
  const tok=(document.cookie.match(/__Host-csrf-token=([^;]+)/)||[])[1]||'';
  const controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(), Math.max(100, arg.timeout_ms));
  try {
    const response=await fetch('/ajax/search/advanced?cacheKey='+Math.random(), {
      method:'POST', credentials:'same-origin', signal:controller.signal,
      headers:{'Content-Type':'application/json','X-Csrf-Token':decodeURIComponent(tok)},
      body:JSON.stringify(arg.body)
    });
    return await response.text();
  } finally { clearTimeout(timer); }
}
"""


class RusProfilePlaywrightError(RuntimeError):
    """Cookie, browser, anti-bot or response error."""


class RusProfileDeadlineReached(RusProfilePlaywrightError):
    """Намеренная остановка по общему лимиту времени, а не сбой источника."""


class RusProfileCardSourceError(RusProfilePlaywrightError):
    """Карточка заменена антиботом или больше не соответствует ожидаемой схеме."""


def log(message):
    print(message, flush=True)


# --------------------------------------------------------- факты карточки ----
# Проверено на живой карточке 2026-08-06 при ЗАКРЫТЫХ контактах: блок «Руководитель»
# (должность, ФИО, дата назначения) читается без профессионального доступа, а телефоны
# и почта в это же время замаскированы. Поэтому разбор руководителя обязан идти ВЫШЕ
# пейвол-гарда _contacts_from_current — иначе ЛПР теряется вместе с телефонами.
_RE_MANAGER = re.compile(
    r"Руководитель\s*\r?\n\s*([^\r\n]+)\s*\r?\n\s*([^\r\n]+)", re.I)
# В подвале карточки: «Генеральный директор ООО "X" - Иванов Иван Иванович (ИНН 123456789012)».
# 12 цифр — ИНН физлица (у организаций 10), это и отличает ЛПР от самой компании.
_RE_PERSON_INN = re.compile(r"\(ИНН\s*(\d{12})\)")
_RE_CAPITAL = re.compile(r"Уставный капитал\s*\r?\n\s*([^\r\n]+)", re.I)
_RE_STAFF = re.compile(
    r"Среднесписочная численность\s*\r?\n\s*([\d\s]+)\s+сотрудник\w*\s+в\s+(\d{4})\s+год",
    re.I,
)
_RE_REVENUE_YEAR = re.compile(
    r"Основные показатели за\s+(\d{4})\s+год\w*:?\s*\r?\n\s*"
    r"Выручка\s*\r?\n\s*([^\r\n]+)",
    re.I,
)


def _masked(value):
    """Строка целиком или частично закрыта пейволом."""
    return _MASK in (value or "")


def card_facts(text):
    """Факты главной карточки, доступные БЕЗ платного доступа.

    Возвращает ключи только для того, что реально прочиталось: пустые и
    замаскированные значения не кладём, чтобы не затирать в лиде то, что уже
    нашли другие источники."""
    text = text or ""
    facts = {}
    manager = _RE_MANAGER.search(text)
    if manager:
        # порядок строк в блоке фиксирован: «Руководитель», должность, ФИО
        post, fio = manager.group(1).strip(), manager.group(2).strip()
        if not _masked(post) and not _masked(fio):
            facts["_ceo_post"] = post
            facts["_ceo_fio"] = fio
    person_inn = _RE_PERSON_INN.search(text)
    if person_inn:
        facts["_ceo_inn"] = person_inn.group(1)
    capital = _RE_CAPITAL.search(text)
    if capital and not _masked(capital.group(1)):
        facts["_capital"] = capital.group(1).strip()
    staff_values = {
        (int(re.sub(r"\s+", "", match.group(1))), int(match.group(2)))
        for match in _RE_STAFF.finditer(text)
        if not _masked(match.group(0))
    }
    if len(staff_values) == 1:
        facts["_staff_count"], facts["_staff_year"] = next(iter(staff_values))
    revenue_values = {
        (int(match.group(1)), " ".join(match.group(2).split()))
        for match in _RE_REVENUE_YEAR.finditer(text)
        if not _masked(match.group(0))
    }
    if len(revenue_values) == 1:
        facts["_revenue_year"], facts["_revenue_display"] = next(iter(revenue_values))
    return facts


def _name_above(lines, idx, depth=3):
    """ФИО (или название) учредителя — ближайшая непустая строка над «Период:».

    Смотрим на несколько строк вверх, а не ровно на одну: вёрстка вставляет между
    ними пустые строки."""
    for back in range(idx - 1, max(-1, idx - depth - 1), -1):
        if lines[back]:
            return lines[back]
    return ""


def parse_founders(text):
    """Страница ``/founders/<id>`` -> учредители.

    Формат блока (проверен на живой странице): строка с ФИО/названием, затем
    «Период: …», «Доля: …», «Руководитель: …», «Связи: …», «ИНН: …». Разделы
    «Актуальные (N)» и «Исторические (N)» идут подряд — исторических владельцев
    в outreach не используем, но факт их наличия сохраняем.

    ⚠️ Состав учредителей — платный раздел RusProfile. Без профессионального доступа
    ФИО приходят замаскированными (``░``). В этом случае возвращается ``locked=True``
    и ПУСТОЙ список — «учредители не раскрыты» и «учредителей нет» это разные вещи,
    и пайплайн не должен их путать."""
    lines = [ln.strip() for ln in (text or "").replace("\r", "").split("\n")]
    founders, current = [], None
    in_historic = locked = False
    for idx, line in enumerate(lines):
        if line.startswith("Исторические ("):
            in_historic = True
            continue
        if line.startswith("Актуальные ("):
            in_historic = False
            continue
        if line.startswith("Период:"):
            name = _name_above(lines, idx)
            if _masked(name):
                locked = True
                current = None                     # блок закрыт — его «Доля»/«ИНН» не наши
                continue
            if not name:
                continue
            current = {"fio": name, "period": line.split(":", 1)[1].strip(),
                       "historic": in_historic, "share": "", "inn": ""}
            founders.append(current)
            continue
        if current is None:
            continue
        for prefix, key in (("Доля:", "share"), ("ИНН:", "inn")):
            if line.startswith(prefix):
                value = line.split(":", 1)[1].strip()
                current[key] = "" if _masked(value) else value
    if _masked(text) and not founders:
        locked = True
    return {"founders": [f for f in founders if not f["historic"]],
            "historic": [f for f in founders if f["historic"]],
            "locked": locked}


def normalize_cookie_rows(rows, *, now=None):
    """Selenium JSON-cookie -> Playwright cookie; expired entries are skipped."""
    current = time.time() if now is None else float(now)
    normalized = []
    for raw in rows if isinstance(rows, list) else []:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        expiry = raw.get("expiry", raw.get("expires"))
        try:
            expiry = float(expiry) if expiry not in (None, "") else None
        except (TypeError, ValueError):
            expiry = None
        if expiry is not None and expiry <= current:
            continue
        item = {
            "name": str(raw["name"]),
            "value": str(raw.get("value") or ""),
            "domain": str(raw.get("domain") or ".rusprofile.ru"),
            "path": str(raw.get("path") or "/"),
            "httpOnly": bool(raw.get("httpOnly")),
            "secure": bool(raw.get("secure")),
        }
        if expiry is not None:
            item["expires"] = expiry
        same = str(raw.get("sameSite") or "").strip().lower()
        mapped = {
            "strict": "Strict",
            "lax": "Lax",
            "none": "None",
            "no_restriction": "None",
        }.get(same)
        if mapped:
            item["sameSite"] = mapped
        normalized.append(item)
    return normalized


def load_cookies(path=COOKIES_FILE):
    cookie_path = Path(path)
    if not cookie_path.is_file():
        raise RusProfilePlaywrightError(
            f"cookie-файл RusProfile не найден: {cookie_path}. "
            "Выполни один раз: py rusprofile_session.py --login")
    try:
        rows = json.loads(cookie_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RusProfilePlaywrightError(
            f"cookie-файл RusProfile не читается: {cookie_path}") from exc
    cookies = normalize_cookie_rows(rows)
    if not cookies:
        raise RusProfilePlaywrightError(
            f"в cookie-файле RusProfile нет действующих записей: {cookie_path}")
    return cookies


class RusProfilePlaywrightSession:
    """Синхронная Playwright-сессия поиска и карточек RusProfile."""

    def __init__(
        self,
        headless=False,
        offscreen=False,
        cookies_file=None,
        timeout_ms=None,
        deadline=None,
        log_fn=log,
    ):
        self.headless = bool(headless)
        self.offscreen = bool(offscreen)
        self.cookies_file = str(
            cookies_file or os.environ.get("RUSPROFILE_COOKIES_FILE") or COOKIES_FILE)
        self.timeout_ms = int(
            timeout_ms or os.environ.get("RUSPROFILE_TIMEOUT_MS") or 60_000)
        self.deadline = deadline
        self.log = log_fn
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.cookies_loaded = 0
        self.search_requests = 0
        self.last_contacts_locked = False

    def __enter__(self):
        self._remaining_ms(self.deadline, self.timeout_ms)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RusProfilePlaywrightError(
                "Playwright не установлен: py -m pip install playwright && "
                "py -m playwright install chromium") from exc

        cookies = load_cookies(self.cookies_file)
        try:
            self._pw = sync_playwright().start()
            args = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--window-size=1320,950",
            ]
            if self.offscreen and not self.headless:
                args.append("--window-position=-32000,-32000")
            self.browser = self._pw.chromium.launch(
                headless=self.headless, args=args,
                timeout=self._remaining_ms(self.deadline, self.timeout_ms))
            self._remaining_ms(self.deadline, self.timeout_ms)
            self.context = self.browser.new_context(viewport={"width": 1320, "height": 950})
            for cookie in cookies:
                try:
                    # Одна плохая старая cookie не должна выбить весь действующий набор.
                    self.context.add_cookies([cookie])
                    self.cookies_loaded += 1
                except Exception:
                    continue
            if not self.cookies_loaded:
                raise RusProfilePlaywrightError(
                    "Playwright не принял ни одной cookie RusProfile")
            self.page = self.context.new_page()
            self._goto(ADV_URL, settle_ms=7_000, deadline=self.deadline)
            self.log(
                f"[RusProfile/Playwright] cookie загружены: {self.cookies_loaded}; "
                f"файл {self.cookies_file}")
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_exc):
        for obj, method in (
            (self.context, "close"),
            (self.browser, "close"),
            (self._pw, "stop"),
        ):
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:
                    pass
        self.page = self.context = self.browser = self._pw = None

    @staticmethod
    def _remaining_ms(deadline, fallback):
        if deadline is None:
            return int(fallback)
        remaining = int((float(deadline) - time.monotonic()) * 1000)
        if remaining <= 0:
            raise RusProfileDeadlineReached("общий лимит времени RusProfile исчерпан")
        return min(int(fallback), remaining)

    def _goto(self, url, *, settle_ms=3_000, deadline=None):
        timeout_ms = self._remaining_ms(deadline, self.timeout_ms)
        self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        self.page.wait_for_timeout(self._remaining_ms(deadline, settle_ms))
        # Cloudflare иногда успевает завершить challenge уже после DOMContentLoaded.
        challenge_end = time.monotonic() + 20
        if deadline is not None:
            challenge_end = min(challenge_end, float(deadline))
        while time.monotonic() < challenge_end:
            title = (self.page.title() or "").lower()
            if "just a moment" not in title and "подождите" not in title:
                break
            self.page.wait_for_timeout(self._remaining_ms(deadline, 1_500))

    def _post(self, body, timeout_ms=None):
        timeout_ms = max(100, min(self.timeout_ms, int(timeout_ms or self.timeout_ms)))
        self.search_requests += 1
        raw = self.page.evaluate(_SEARCH_XHR, {"body": body, "timeout_ms": timeout_ms})
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RusProfilePlaywrightError(
                "RusProfile advanced-search вернул невалидный JSON") from exc
        if not isinstance(payload, dict):
            raise RusProfilePlaywrightError(
                "RusProfile advanced-search вернул неожиданный ответ")
        return payload

    def _post_retry(self, body, log_fn=None, deadline=None):
        logger = log_fn or self.log
        last = ""
        for attempt in range(2):
            timeout_ms = self._remaining_ms(deadline, self.timeout_ms)
            try:
                response = self._post(body, timeout_ms=timeout_ms)
                if response.get("success"):
                    return response
                last = str(response.get("message") or "success=false")[:120]
            except RusProfileDeadlineReached:
                raise
            except Exception as exc:
                last = str(exc).splitlines()[0][:120]
            if attempt == 0:
                logger(
                    f"  [warn] стр.{body.get('page')}: {last} — "
                    "обновляю Playwright-страницу и повторяю")
                self._goto(ADV_URL, settle_ms=6_000, deadline=deadline)
        raise RusProfilePlaywrightError(
            f"стр.{body.get('page')}: RusProfile advanced-search недоступен после ретрая: {last}")

    def search(self, okved, revenue_from, max_pages=MAX_SEARCH_PAGES, pause=0.35,
               log=log, staff_from=None, staff_to=None, deadline=None):
        """Advanced-search: ОКВЭД, выручка и опциональная численность."""
        if deadline is not None and time.monotonic() >= float(deadline):
            raise RusProfileDeadlineReached(
                "лимит времени истёк до открытия advanced-search")
        if "/search-advanced" not in (self.page.url or ""):
            self._goto(ADV_URL, settle_ms=5_000, deadline=deadline)
        base = {
            "action": "search_advanced",
            "query": "",
            "state_1": True,
            "state_2": False,
            "state_3": False,
            "state_4": False,
            "state_5": False,
            "okved_strict": True,
            "okved": list(okved),
            "finance_revenue_from": str(int(revenue_from)),
        }
        if staff_from is not None:
            base["sshr_from"] = str(int(staff_from))
        if staff_to is not None:
            base["sshr_to"] = str(int(staff_to))
        if (staff_from is not None and staff_to is not None
                and int(staff_from) > int(staff_to)):
            raise ValueError("staff_from не может быть больше staff_to")
        out = []
        page_count = max(1, min(int(max_pages), MAX_SEARCH_PAGES))
        total = available = None
        for page_no in range(1, page_count + 1):
            if deadline is not None and time.monotonic() >= float(deadline):
                raise RusProfileDeadlineReached(
                    f"лимит времени истёк перед стр.{page_no}")
            response = self._post_retry(
                dict(base, page=page_no), log, deadline=deadline)
            if deadline is not None and time.monotonic() >= float(deadline):
                raise RusProfileDeadlineReached(
                    f"лимит времени истёк на стр.{page_no}")
            if response is None:
                break
            data = response.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise RusProfilePlaywrightError(
                    f"RusProfile: неверная схема ответа поиска на стр.{page_no}")
            items = data["items"]
            pagination = data.get("pagination")
            if (not isinstance(pagination, dict)
                    or any(not isinstance(item, dict) for item in items)):
                raise RusProfilePlaywrightError(
                    f"RusProfile: неверная схема ответа поиска на стр.{page_no}")
            try:
                current_total = int(data["total_count"])
                current_available = int(pagination["page_count"])
            except (TypeError, ValueError):
                raise RusProfilePlaywrightError(
                    f"RusProfile: неверная схема ответа поиска на стр.{page_no}") from None
            except KeyError:
                raise RusProfilePlaywrightError(
                    f"RusProfile: неполная схема ответа поиска на стр.{page_no}") from None
            if current_total < 0 or current_available < 0:
                raise RusProfilePlaywrightError(
                    f"RusProfile: неверная схема ответа поиска на стр.{page_no}")
            if total is None:
                total, available = current_total, current_available
                page_count = min(page_count, max(1, available))
                log(f"  всего по фильтру: {total} (страниц до {page_count})")
            elif (current_total, current_available) != (total, available):
                raise RusProfilePlaywrightError(
                    f"RusProfile: изменилась схема пагинации на стр.{page_no}")
            if not items:
                if total is not None and len(out) < total:
                    raise RusProfilePlaywrightError(
                        f"RusProfile: преждевременно пустая стр.{page_no}; "
                        f"получено {len(out)} из заявленных {total}")
                break
            out.extend(items)
            log(f"  стр.{page_no}: +{len(items)} (итого {len(out)})")
            if page_no >= page_count:
                break
            delay = max(0.0, float(pause))
            if deadline is not None and delay >= float(deadline) - time.monotonic():
                raise RusProfileDeadlineReached(
                    f"лимит времени истёк перед паузой после стр.{page_no}")
            time.sleep(delay)
        if deadline is not None and time.monotonic() >= float(deadline):
            raise RusProfileDeadlineReached(
                "лимит времени истёк при завершении advanced-search")
        return out

    def _snapshot(self, link, *, deadline=None, section="id"):
        url = _canonical_profile_url(link, section)
        self._goto(url, settle_ms=3_000, deadline=deadline)
        html = self.page.content()
        try:
            text = self.page.locator("body").inner_text()
        except Exception:
            text = ""
        return url, html, text, html + "\n" + text

    def _contacts_from_current(self, combined):
        if PAYWALL in combined.lower() or _MASK in combined or not _REAL_TEL.search(combined):
            return {}
        result = self.page.evaluate(
            "() => {" + RusProfileAuth._JS_CONTACTS + "}") or {}
        block = result.get("block", "")
        phones = list(result.get("phones") or [])
        phones += re.findall(r"\+7[\s\d()\-]{8,16}", block)
        phones = [
            re.sub(r"\s+", " ", phone).strip()
            for phone in phones if re.search(r"\d", phone)
        ]
        emails = list(result.get("emails") or []) + _REAL_MAIL.findall(block)
        emails = [
            email for email in emails
            if not email.lower().endswith((".png", ".jpg", ".svg"))
        ]
        website = (result.get("website") or "").strip()
        if not website:
            match = re.search(
                r"Сайт[\s\S]{0,30}?([a-zA-Zа-яё0-9.\-]+\.(?:ru|рф|com|su|org))",
                block,
            )
            website = ("http://" + match.group(1)) if match else ""
        unique_phones = list(dict.fromkeys(phones))
        return {
            # phone — историческая одна строка, обрезанная под ширину таблиц Excel;
            # phones — полный список, он и нужен рассылке (звонок по второму номеру,
            # если по первому не дозвонились)
            "phone": ", ".join(unique_phones)[:90],
            "phones": unique_phones,
            "emails": list(dict.fromkeys(email.lower() for email in emails)),
            "website": website,
        }

    def contacts_unlocked(self):
        _url, _html, _text, combined = self._snapshot(TEST_CARD)
        return (
            PAYWALL not in combined.lower()
            and _MASK not in combined
            and bool(_REAL_TEL.search(combined))
        )

    def card_facts_by_url(self, link, *, expected_inn="", deadline=None):
        """Открыть карточку и извлечь показатели строгого отбора."""
        _url, _title, text, _html = self._snapshot(link, deadline=deadline)
        lowered = text.casefold()
        anti_bot = (
            "captcha", "just a moment", "cloudflare", "проверка браузера",
            "доступ ограничен", "подтвердите, что вы не робот",
        )
        if any(marker in lowered for marker in anti_bot):
            raise RusProfileCardSourceError("карточка RusProfile заменена CAPTCHA/anti-bot")
        expected_inn = re.sub(r"\D", "", str(expected_inn or ""))
        if expected_inn:
            shown_inns = {
                re.sub(r"\D", "", value)
                for value in re.findall(r"\bИНН\D{0,20}([\d\s-]{10,24})", text, re.I)
            }
            if expected_inn not in shown_inns:
                raise RusProfileCardSourceError(
                    "карточка RusProfile не содержит ожидаемый ИНН")
        facts = card_facts(text)
        if not facts.get("_revenue_display") or not facts.get("_revenue_year"):
            raise RusProfileCardSourceError(
                "схема карточки RusProfile не содержит выручку и её год")
        if facts.get("_staff_count") is None:
            raise RusProfileCardSourceError(
                "схема карточки RusProfile не содержит численность сотрудников")
        return facts

    def contacts_by_url(self, link, *, deadline=None):
        _url, _html, text, combined = self._snapshot(link, deadline=deadline)
        low = combined.lower()
        self.last_contacts_locked = PAYWALL in low or _MASK in combined
        contacts = self._contacts_from_current(combined)
        main_okved = re.search(
            r"Основной вид деятельности\s*\r?\n[^\r\n]*?"
            r"\((\d{2}(?:\.\d{1,2}){1,2})\)",
            text,
            re.I,
        )
        if main_okved:
            contacts["_main_okved_id"] = main_okved.group(1)
        # руководитель и его ИНН — БЕСПЛАТНАЯ часть карточки, поэтому добираются
        # даже когда _contacts_from_current вернул {} из-за пейвола
        contacts.update(card_facts(text))
        return contacts

    def founders_by_url(self, link):
        """Учредители компании — отдельная страница ``/founders/<id>``.

        На главной карточке структурированного состава учредителей нет: там о них
        говорится только прозой в саммари, поэтому нужен отдельный переход. Ссылку
        строим из URL карточки: ``/id/<n>`` -> ``/founders/<n>``."""
        url = str(link)
        match = re.search(r"/id/(\d+)", url)
        if not match:
            return {"founders": [], "historic": [], "locked": False,
                    "note": f"из ссылки {url[:60]} не выводится страница учредителей"}
        founders_url = f"https://www.rusprofile.ru/founders/{match.group(1)}"
        _url, _html, text, _combined = self._snapshot(
            founders_url, section="founders")
        return parse_founders(text)

    def company_by_url(self, link):
        """Совместимый с прежним --urls-file --playwright плоский JSON карточки."""
        url, _html, text, combined = self._snapshot(link)
        contacts = self._contacts_from_current(combined)

        def one(pattern):
            match = re.search(pattern, text, re.I)
            return match.group(1).strip() if match else ""

        title = self.page.title()
        title_inn = re.search(r"\(ИНН\s+(\d{10,12})\)", title, re.I)
        heading = (
            self.page.locator("h1").first.inner_text().strip()
            if self.page.locator("h1").count() else "")
        manager = _RE_MANAGER.search(text)
        revenue = re.search(
            r"Основные показатели[^\r\n]*\r?\n(?:[^\r\n]*\r?\n){0,3}?"
            r"Выручка\s*\r?\n\s*([^\r\n]+)",
            text,
            re.I,
        )
        return {
            "requested_url": url,
            "name": heading or (title.split(" - ")[0] if title else "").strip(),
            "inn": title_inn.group(1) if title_inn else one(r"ИНН\s+(\d{10,12})"),
            "revenue": revenue.group(1).strip() if revenue else "",
            "manager_role": manager.group(1).strip() if manager else "",
            "manager_name": manager.group(2).strip() if manager else "",
            "phones": [
                phone.strip() for phone in (contacts.get("phone") or "").split(",")
                if phone.strip()
            ],
            "emails": contacts.get("emails") or [],
            "website": contacts.get("website") or "",
        }

    def enrich_leads(self, leads, only_missing=True, log=log, checkpoint=None,
                     stop_when_locked=True, need_facts=False, with_founders=False):
        """Открыть ровно выбранные карточки и заполнить сайт/телефон/email.

        ``need_facts`` — карточку открывать и ради ЛПР (должность, ФИО, ИНН физлица),
        даже если контакты у лида уже есть: это разные разделы страницы.
        ``stop_when_locked`` — прежнее поведение «нет платного доступа, дальше нет
        смысла». Для рассылки его выключают: руководитель читается и без доступа,
        и ради него карточки стоит дочитать до конца.
        ``with_founders`` — дополнительно открыть страницу учредителей (ещё один
        переход на компанию; без профессионального доступа состав замаскирован)."""
        try:
            from checko_enrich import _best_email
        except Exception:
            _best_email = lambda emails: (
                emails[0] if emails else None, "общая", False)

        def needed_url(lead):
            url = lead.get("_rusprofile_url") or (
                lead.get("_revenue_source_url")
                if "/id/" in (lead.get("_revenue_source_url") or "") else "")
            if not url:
                return None
            if need_facts and not lead.get("_ceo_fio"):
                return url
            if only_missing and (
                    lead.get("website") or lead.get("phone") or lead.get("email")):
                return None
            return url

        def save_checkpoint():
            if checkpoint:
                try:
                    checkpoint()
                except Exception:
                    pass

        todo = [(lead, needed_url(lead)) for lead in leads]
        todo = [(lead, url) for lead, url in todo if url]
        log(f"[RusProfile/Playwright] карточек к парсингу: {len(todo)}")
        used = sites = phones = emails = failed = facts = 0
        locked = warned_locked = False
        for lead, url in todo:
            used += 1
            try:
                contacts = self.contacts_by_url(url)
            except Exception as exc:
                failed += 1
                log(
                    f"  [warn] карточка {used}/{len(todo)} не разобрана: "
                    f"{type(exc).__name__}")
                continue
            # Данные ЕГРЮЛ бесплатны и читаются даже при закрытых контактах —
            # переносим их ДО проверки замка, иначе ЛПР терялся бы вместе с телефоном
            if contacts.get("_ceo_fio") and not lead.get("_ceo_fio"):
                facts += 1
            for key in (
                "_ceo_post", "_ceo_fio", "_ceo_inn", "_capital",
                "_staff_count", "_staff_year", "_revenue_year", "_revenue_display",
            ):
                if contacts.get(key) and not lead.get(key):
                    lead[key] = contacts[key]
            if contacts.get("_ceo_fio") and not lead.get("contact_person"):
                lead["contact_person"] = contacts["_ceo_fio"]
            if with_founders:
                try:
                    found = self.founders_by_url(url)
                except Exception:                  # noqa: BLE001 — учредители не критичны
                    found = {}
                if found.get("founders"):
                    lead["_founders"] = found["founders"]
                elif found.get("locked"):
                    lead["_founders_locked"] = True
            if self.last_contacts_locked:
                locked = True
                if stop_when_locked:
                    log(
                        "[RusProfile/Playwright] контакты закрыты — cookie истекли "
                        "или нет профессионального доступа")
                    break
                if not warned_locked:
                    warned_locked = True
                    log(
                        "[RusProfile/Playwright] контакты закрыты (нет профессионального "
                        "доступа) — телефоны и почта не читаются, продолжаем ради "
                        "руководителя: он в бесплатной части карточки")
            main_okved = contacts.get("_main_okved_id")
            if main_okved:
                niche = re.sub(
                    r"\s+\(ОКВЭД[^)]*\)\s*$", "", lead.get("niche") or "")
                lead["niche"] = f"{niche} (ОКВЭД {main_okved})"
            if not contacts:
                continue
            if contacts.get("website") and not lead.get("website"):
                lead["website"] = contacts["website"]
                sites += 1
            if contacts.get("phone") and not lead.get("phone"):
                lead["phone"] = contacts["phone"]
                lead["_phones"] = contacts.get("phones") or []
                phones += 1
            if contacts.get("emails"):
                lead["_emails"] = contacts["emails"]
            if contacts.get("emails") and not lead.get("email"):
                best, kind, is_target = _best_email(contacts["emails"])
                if best:
                    lead["email"] = best
                    lead["_email_kind"] = kind
                    lead["_email_is_target"] = is_target
                    lead["_email_src"] = "RusProfile/Playwright"
                    emails += 1
            if used % 20 == 0:
                save_checkpoint()
                log(
                    f"  RusProfile/Playwright: {used}/{len(todo)} | "
                    f"сайт {sites} тел {phones} email {emails}")
        save_checkpoint()
        log(
            f"[RusProfile/Playwright] контакты: {used} карточек | "
            f"сайт {sites} | тел {phones} | email {emails} | ЛПР {facts}"
            + (f" | ошибок {failed}" if failed else ""))
        return {
            "used": used,
            "site": sites,
            "phone": phones,
            "email": emails,
            "facts": facts,
            "failed": failed,
            "locked": locked,
        }
