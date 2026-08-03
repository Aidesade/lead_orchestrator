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
from pathlib import Path

from rusprofile_session import (
    COOKIES_FILE,
    HOME,
    PAYWALL,
    TEST_CARD,
    _MASK,
    _REAL_MAIL,
    _REAL_TEL,
    RusProfileAuth,
)

ADV_URL = "https://www.rusprofile.ru/search-advanced"
MAX_SEARCH_PAGES = 20

_SEARCH_XHR = r"""
body => {
  const tok=(document.cookie.match(/__Host-csrf-token=([^;]+)/)||[])[1]||'';
  const xhr=new XMLHttpRequest();
  xhr.open('POST','/ajax/search/advanced?cacheKey='+Math.random(),false);
  xhr.setRequestHeader('Content-Type','application/json');
  xhr.setRequestHeader('X-Csrf-Token', decodeURIComponent(tok));
  xhr.send(JSON.stringify(body));
  return xhr.responseText;
}
"""


class RusProfilePlaywrightError(RuntimeError):
    """Cookie, browser, anti-bot or response error."""


def log(message):
    print(message, flush=True)


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
        log_fn=log,
    ):
        self.headless = bool(headless)
        self.offscreen = bool(offscreen)
        self.cookies_file = str(
            cookies_file or os.environ.get("RUSPROFILE_COOKIES_FILE") or COOKIES_FILE)
        self.timeout_ms = int(
            timeout_ms or os.environ.get("RUSPROFILE_TIMEOUT_MS") or 60_000)
        self.log = log_fn
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.cookies_loaded = 0
        self.search_requests = 0
        self.last_contacts_locked = False

    def __enter__(self):
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
            self.browser = self._pw.chromium.launch(headless=self.headless, args=args)
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
            self._goto(ADV_URL, settle_ms=7_000)
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

    def _goto(self, url, *, settle_ms=3_000):
        self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        self.page.wait_for_timeout(settle_ms)
        # Cloudflare иногда успевает завершить challenge уже после DOMContentLoaded.
        deadline = time.time() + 20
        while time.time() < deadline:
            title = (self.page.title() or "").lower()
            if "just a moment" not in title and "подождите" not in title:
                break
            self.page.wait_for_timeout(1_500)

    def _post(self, body):
        raw = self.page.evaluate(_SEARCH_XHR, body)
        self.search_requests += 1
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RusProfilePlaywrightError(
                "RusProfile advanced-search вернул невалидный JSON") from exc
        if not isinstance(payload, dict):
            raise RusProfilePlaywrightError(
                "RusProfile advanced-search вернул неожиданный ответ")
        return payload

    def _post_retry(self, body, log_fn=None):
        logger = log_fn or self.log
        last = ""
        for attempt in range(2):
            try:
                response = self._post(body)
                if response.get("success"):
                    return response
                last = str(response.get("message") or "success=false")[:120]
            except Exception as exc:
                last = str(exc).splitlines()[0][:120]
            if attempt == 0:
                logger(
                    f"  [warn] стр.{body.get('page')}: {last} — "
                    "обновляю Playwright-страницу и повторяю")
                self._goto(ADV_URL, settle_ms=6_000)
        logger(f"  [warn] стр.{body.get('page')}: {last} — стоп")
        return None

    def search(self, okved, revenue_from, max_pages=MAX_SEARCH_PAGES, pause=0.35, log=log):
        """Внутренний advanced-search: ОКВЭД + серверный порог выручки."""
        if "/search-advanced" not in (self.page.url or ""):
            self._goto(ADV_URL, settle_ms=5_000)
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
        out = []
        page_count = max(1, min(int(max_pages), MAX_SEARCH_PAGES))
        total = None
        for page_no in range(1, page_count + 1):
            response = self._post_retry(dict(base, page=page_no), log)
            if response is None:
                break
            data = response.get("data") or {}
            items = data.get("items") or []
            if total is None:
                total = data.get("total_count")
                try:
                    available = int((data.get("pagination") or {}).get("page_count") or 1)
                except (TypeError, ValueError):
                    available = 1
                page_count = min(page_count, max(1, available))
                log(f"  всего по фильтру: {total} (страниц до {page_count})")
            if not items:
                break
            out.extend(item for item in items if isinstance(item, dict))
            log(f"  стр.{page_no}: +{len(items)} (итого {len(out)})")
            if page_no >= page_count:
                break
            time.sleep(max(0.0, float(pause)))
        return out

    def _snapshot(self, link):
        url = link if str(link).startswith("http") else "https://www.rusprofile.ru" + str(link)
        self._goto(url, settle_ms=3_000)
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
        return {
            "phone": ", ".join(dict.fromkeys(phones))[:90],
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

    def contacts_by_url(self, link):
        _url, _html, text, combined = self._snapshot(link)
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
        return contacts

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
        manager = re.search(
            r"Руководитель\s*\r?\n\s*([^\r\n]+)\s*\r?\n\s*([^\r\n]+)",
            text,
            re.I,
        )
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

    def enrich_leads(self, leads, only_missing=True, log=log, checkpoint=None):
        """Открыть ровно выбранные карточки и заполнить сайт/телефон/email."""
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
        used = sites = phones = emails = failed = 0
        locked = False
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
            if self.last_contacts_locked:
                locked = True
                log(
                    "[RusProfile/Playwright] контакты закрыты — cookie истекли "
                    "или нет профессионального доступа")
                break
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
                phones += 1
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
            f"сайт {sites} | тел {phones} | email {emails}"
            + (f" | ошибок {failed}" if failed else ""))
        return {
            "used": used,
            "site": sites,
            "phone": phones,
            "email": emails,
            "failed": failed,
            "locked": locked,
        }
