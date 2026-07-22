# -*- coding: utf-8 -*-
r"""
Источник лидов OfData API для ФАЗЫ 1 — без браузера и антибота.

Контракт намеренно совпадает с source_checko.harvest(): отрасли берутся из
source_rusprofile.INDUSTRY, короткие коды ОКВЭД разворачиваются до NN.NN, регион
разбирается теми же функциями, а результат имеет ту же форму лида.

Последовательность:
  1. /search — действующие юрлица по основному ОКВЭД и региону;
  2. /finances — выручка по строке 2110; бесплатная заглушка -> ГИР БО ФНС;
  3. строгий фильтр: выручка >= max(переданный порог, 1 млрд рублей);
  4. /company — контакты и руководитель только для уже прошедших фильтр лидов.

Ключ читается ТОЛЬКО из OFDATA_API_KEY (либо передаётся конструктору в тесте).
Запросы отправляются POST form-urlencoded: ключ не попадает в URL и логи.
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
import warnings

from checko_enrich import extract_company_contacts
from project_env import load_project_env
from source_checko import region_name, resolve_region, usable_okved

load_project_env()
warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "https://api.ofdata.ru/v2"
PAGE_LIMIT = 100
MAX_PAGES_HARD = 50
MIN_REVENUE_FLOOR = 1_000_000_000
FINANCES_DOC_URL = "https://ofdata.ru/api/finances"


class OfDataSourceError(RuntimeError):
    """Ошибка транспорта, авторизации или ответа OfData."""


def log(message):
    print(message, flush=True)


def _digits(value):
    return re.sub(r"\D", "", str(value or ""))


def _env_int(name, default, minimum=0):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        log(f"[!] {name}={raw!r} не является целым числом — использую {default}")
        return default


def _env_float(name, default, minimum=0.0):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        log(f"[!] {name}={raw!r} не является числом — использую {default}")
        return default


def _number(value):
    """Число из JSON/строки; bool и пустые значения не считаем суммой."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else float(value)
    text = str(value).replace("\xa0", "").replace(" ", "").replace(",", ".").strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def extract_latest_revenue(finances):
    """Ответ data из /finances -> (выручка, год).

    Строка 2110 бывает числом (обычный ответ) либо объектом с «СумОтч»
    (extended=true). Поддерживаем оба официальных представления.
    """
    if not isinstance(finances, dict):
        return None, None
    years = sorted(
        ((int(str(year)), report) for year, report in finances.items()
         if str(year).isdigit() and isinstance(report, dict)),
        key=lambda item: item[0],
        reverse=True,
    )
    for year, report in years:
        row = report.get("2110")
        if row is None:
            row = report.get(2110)
        if isinstance(row, dict):
            value = None
            for key in ("СумОтч", "Сумма", "Значение", "value"):
                if key in row:
                    value = _number(row.get(key))
                    break
        else:
            value = _number(row)
        if value is not None:
            return value, year
    return None, None


def finances_are_paywalled(finances):
    """True, если OfData явно вернул заглушки бесплатного тарифа вместо отчётности."""
    if not isinstance(finances, dict):
        return False
    year_values = [value for year, value in finances.items() if str(year).isdigit()]
    if not year_values:
        return False
    return all(
        isinstance(value, str)
        and "недоступ" in value.lower()
        and "бесплат" in value.lower()
        for value in year_values
    )


class OfDataClient:
    """Тонкий клиент методов /search, /finances и /company."""

    def __init__(self, api_key=None, pause=None, retries=None, timeout=None):
        self.api_key = (api_key or os.environ.get("OFDATA_API_KEY") or "").strip()
        if not self.api_key:
            raise OfDataSourceError(
                "не задан OFDATA_API_KEY в env/.env или окружении")
        self.pause = _env_float("OFDATA_PAUSE", 0.10) if pause is None else max(0.0, pause)
        self.retries = (
            _env_int("OFDATA_RETRIES", 3, minimum=1)
            if retries is None else max(1, int(retries))
        )
        self.timeout = (
            _env_float("OFDATA_TIMEOUT", 30.0, minimum=1.0)
            if timeout is None else max(1.0, float(timeout))
        )
        self.requests_used = 0
        self.requests_by_method = {"search": 0, "finances": 0, "company": 0}
        self.today_request_count = None
        self.balance = None

    def _safe(self, value):
        text = str(value or "")
        if self.api_key:
            text = text.replace(self.api_key, "<скрыто>")
        return text[:400]

    def _request(self, method, params):
        if method not in self.requests_by_method:
            raise ValueError(f"неподдерживаемый метод OfData: {method}")
        url = f"{BASE}/{method}"
        body = urllib.parse.urlencode(dict(params, key=self.api_key)).encode("utf-8")
        last_error = ""

        for attempt in range(1, self.retries + 1):
            req = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                    "User-Agent": "lead-orchestrator-ofdata/1.0",
                },
            )
            self.requests_used += 1
            self.requests_by_method[method] += 1
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read().decode("utf-8", "replace")
                except Exception:
                    detail = ""
                last_error = f"HTTP {exc.code}" + (f": {self._safe(detail)}" if detail else "")
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if retryable and attempt < self.retries:
                    try:
                        retry_after = float(exc.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        retry_after = 0.0
                    time.sleep(max(retry_after, 1.5 * attempt))
                    continue
                if exc.code in (401, 403):
                    raise OfDataSourceError(
                        f"/{method}: ключ отклонён или метод недоступен тарифу ({last_error})") from exc
                raise OfDataSourceError(f"/{method}: {last_error}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = self._safe(getattr(exc, "reason", exc))
                if attempt < self.retries:
                    time.sleep(1.0 * attempt)
                    continue
                raise OfDataSourceError(
                    f"/{method}: сеть недоступна после {self.retries} попыток ({last_error})") from exc

            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise OfDataSourceError(f"/{method}: OfData вернул невалидный JSON") from exc
            if not isinstance(payload, dict):
                raise OfDataSourceError(f"/{method}: неожиданный тип ответа OfData")
            meta = payload.get("meta") or {}
            if isinstance(meta, dict):
                self.today_request_count = meta.get(
                    "today_request_count", self.today_request_count)
                self.balance = meta.get("balance", self.balance)
                status = str(meta.get("status") or "").lower()
                if status and status != "ok":
                    message = self._safe(meta.get("message") or "неизвестная ошибка API")
                    raise OfDataSourceError(f"/{method}: {message}")
            if self.pause:
                time.sleep(self.pause)
            return payload
        raise OfDataSourceError(f"/{method}: {last_error or 'запрос не выполнен'}")


    def by_okved(self, okved, region_code=None, max_pages=MAX_PAGES_HARD, active=True):
        """Действующие юрлица с основным ОКВЭД=okved; генератор записей."""
        params = {
            "by": "okved", "obj": "org", "query": okved, "limit": PAGE_LIMIT,
        }
        if region_code:
            params["region"] = str(region_code).zfill(2)
        if active:
            params["active"] = "true"

        page, total_pages = 1, 1
        max_pages = max(1, min(int(max_pages), MAX_PAGES_HARD))
        while page <= min(total_pages, max_pages):
            data = (self._request("search", dict(params, page=page)).get("data") or {})
            records = data.get("Записи") or []
            if page == 1:
                try:
                    total_pages = max(0, int(data.get("СтрВсего") or 0))
                except (TypeError, ValueError):
                    total_pages = 1 if records else 0
                if total_pages > max_pages:
                    log(f"    [!] ОКВЭД {okved}: страниц {total_pages}, берём первые "
                        f"{max_pages} — остальное не просмотрено")
            for record in records:
                if isinstance(record, dict):
                    yield record
            if not records:
                return
            page += 1

    def finances(self, inn):
        """ИНН -> словарь отчётностей по годам."""
        data = self._request("finances", {"inn": _digits(inn)}).get("data") or {}
        return data if isinstance(data, dict) else {}

    def company(self, inn):
        """ИНН -> карточка компании, включая Контакты и Руковод."""
        data = self._request("company", {"inn": _digits(inn)}).get("data") or {}
        return data if isinstance(data, dict) else {}

    def usage_line(self):
        parts = ", ".join(f"/{name} {count}" for name, count in self.requests_by_method.items())
        extra = []
        if self.today_request_count is not None:
            extra.append(f"сегодня {self.today_request_count}")
        if self.balance is not None:
            extra.append(f"баланс {self.balance} ₽")
        return parts + (f" | {', '.join(extra)}" if extra else "")


def item_to_lead(record, cfg, industry, okved):
    """Запись /search -> общий формат лида первой фазы."""
    return {
        "name": record.get("НаимСокр") or record.get("НаимПолн") or "",
        "niche": f"{cfg['label']} (ОКВЭД {okved})",
        "website": "", "phone": "", "email": "", "contact_person": "",
        "source": "OfData API (/search)",
        "pain": cfg["pain"], "offer": cfg["offer"], "status": "", "next_step": "",
        "_inn": _digits(record.get("ИНН")),
        "_ogrn": _digits(record.get("ОГРН")),
        "_address": record.get("ЮрАдрес") or "",
        "_region": region_name(record.get("РегионКод")),
        "_region_code": str(record.get("РегионКод") or ""),
        "_okved": okved,
        "_okved_descr": record.get("ОКВЭД") or "",
        "_status": record.get("Статус") or "",
        "_revenue": None,
        "_industry": industry,
        "_ofdata_search_url": "https://ofdata.ru/api/search",
    }


def _apply_company_data(lead, data):
    """Карточка /company -> контакты и уточнённые реквизиты лида."""
    contacts = extract_company_contacts(data)
    if contacts["website"] and not lead.get("website"):
        lead["website"] = contacts["website"]
    if contacts["phone"] and not lead.get("phone"):
        lead["phone"] = contacts["phone"]
    if contacts["email"] and not lead.get("email"):
        lead["email"] = contacts["email"]
        lead["_email_kind"] = contacts["email_kind"]
        lead["_email_is_target"] = contacts["email_is_target"]
        lead["_email_src"] = "OfData API (/company)"
    if contacts["ceo"] and not lead.get("contact_person"):
        lead["contact_person"] = contacts["ceo"]

    if data.get("НаимСокр") or data.get("НаимПолн"):
        lead["name"] = data.get("НаимСокр") or data.get("НаимПолн")
    status = data.get("Статус")
    if isinstance(status, dict):
        lead["_status"] = status.get("Наим") or lead.get("_status", "")
    elif status:
        lead["_status"] = status
    region = data.get("Регион")
    if isinstance(region, dict):
        lead["_region_code"] = str(region.get("Код") or lead.get("_region_code") or "")
        lead["_region"] = region.get("Наим") or region_name(lead["_region_code"])
    address = data.get("ЮрАдрес")
    if isinstance(address, dict):
        lead["_address"] = address.get("АдресРФ") or lead.get("_address", "")
    elif address:
        lead["_address"] = address
    okved = data.get("ОКВЭД")
    if isinstance(okved, dict):
        lead["_okved"] = okved.get("Код") or lead.get("_okved", "")
        lead["_okved_descr"] = okved.get("Наим") or lead.get("_okved_descr", "")
    if "OfData API (/company)" not in lead.get("source", ""):
        lead["source"] = (lead.get("source") or "OfData API (/search)") + " + OfData API (/company)"


def ofdata_contacts_pass(client, leads, cap=None, only_missing=True, log=log):
    """Заполнить сайт/телефон/email/ЛПР через /company после фильтра по выручке."""
    def needs_company(lead):
        if not lead.get("_inn"):
            return False
        if not only_missing:
            return True
        return not (lead.get("website") or lead.get("phone") or lead.get("email"))

    todo = [lead for lead in leads if needs_company(lead)]
    limit = len(todo) if cap is None or int(cap) <= 0 else min(len(todo), int(cap))
    log(f"=== OfData контакты: кандидатов {len(todo)} | лимит {limit} ===")
    used = sites = phones = emails = 0
    for lead in todo[:limit]:
        used += 1
        try:
            data = client.company(lead["_inn"])
        except OfDataSourceError as exc:
            log(f"  [stop] контакты OfData: {exc}")
            break
        if not data:
            continue
        before = bool(lead.get("website")), bool(lead.get("phone")), bool(lead.get("email"))
        _apply_company_data(lead, data)
        sites += int(not before[0] and bool(lead.get("website")))
        phones += int(not before[1] and bool(lead.get("phone")))
        emails += int(not before[2] and bool(lead.get("email")))
        if used % 20 == 0:
            log(f"  OfData: {used} запросов | сайт {sites} тел {phones} email {emails}")
    if limit < len(todo):
        log(f"  [stop] достигнут OFDATA_CONTACTS_CAP={limit}; без карточки осталось "
            f"{len(todo) - limit}")
    log(f"=== OfData контакты: {used} запросов | сайт {sites} | тел {phones} | "
        f"email {emails} ===")
    return {"used": used, "site": sites, "phone": phones, "email": emails}


def _save(leads, out_path):
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(leads, handle, ensure_ascii=False, indent=1)
    log(f"=== Сохранено: {out_path} ({len(leads)} лидов) ===")


def harvest(industries, min_revenue=MIN_REVENUE_FLOOR, per_industry=40, region=None,
            headless=False, out_path=None, exclude_regions=None, offscreen=False,
            max_candidates=3000, client=None):
    """Drop-in источник: ОКВЭД/регион -> /finances -> порог -> квота отрасли."""
    del headless, offscreen  # совместимость с браузерными источниками
    try:
        from source_rusprofile import INDUSTRY
    except ImportError as exc:
        raise OfDataSourceError(
            "не импортируется source_rusprofile.INDUSTRY — конфиг отраслей недоступен") from exc

    cli = client or OfDataClient()
    threshold = max(float(min_revenue), float(MIN_REVENUE_FLOOR))
    if float(min_revenue) < MIN_REVENUE_FLOOR:
        log(f"[!] порог {float(min_revenue):g} ₽ поднят до обязательного минимума "
            f"{MIN_REVENUE_FLOOR:,} ₽".replace(",", " "))
    inc_codes, exc_names = resolve_region(region)
    if exclude_regions:
        exc_names = (exc_names or []) + list(exclude_regions)
    if exc_names and not inc_codes:
        log(f"[!] регион задан только исключением ({', '.join(exc_names)}) — OfData не "
            f"умеет отрицательный серверный фильтр; перечисляем страну и отсеиваем в коде")

    max_candidates = max(1, int(max_candidates))
    max_pages = _env_int("OFDATA_MAX_PAGES_PER_CODE", MAX_PAGES_HARD, minimum=1)
    by_inn = {}
    for industry in industries:
        if industry not in INDUSTRY:
            raise OfDataSourceError(f"неизвестная отрасль: {industry}")
        cfg = INDUSTRY[industry]
        codes, coarse = usable_okved(cfg["okved"])
        if coarse:
            log(f"[!] {industry}: не удалось развернуть короткие ОКВЭД: "
                f"{', '.join(coarse[:8])}{'...' if len(coarse) > 8 else ''}")
        if not codes:
            log(f"[!!] {industry}: не осталось ОКВЭД уровня NN.NN — отрасль пропущена")
            continue

        log(f"\n=== {industry}: {cfg['label']} | ОКВЭД {len(codes)} шт"
            + (f" | регион {', '.join(region_name(code) for code in inc_codes)}"
               if inc_codes else "")
            + " ===")
        before = len(by_inn)
        stop = False
        for okved in codes:
            for region_code in (inc_codes or [None]):
                for record in cli.by_okved(
                        okved, region_code=region_code, max_pages=max_pages, active=True):
                    inn = _digits(record.get("ИНН"))
                    if not inn or inn in by_inn:
                        continue
                    lead = item_to_lead(record, cfg, industry, okved)
                    if exc_names and any(
                            excluded.lower().replace("ё", "е")[:12]
                            in (lead["_region"] or "").lower().replace("ё", "е")
                            for excluded in exc_names):
                        continue
                    by_inn[inn] = lead
                    if len(by_inn) >= max_candidates:
                        log(f"    [!] достигнут потолок кандидатов ({max_candidates}) — "
                            f"сузь регион или подними OFDATA_MAX_CANDIDATES")
                        stop = True
                        break
                if stop:
                    break
            if stop:
                break
        log(f"    кандидатов по отрасли: +{len(by_inn) - before}")
        if stop:
            break

    leads = list(by_inn.values())
    log(f"\n=== Кандидатов всего: {len(leads)} | {cli.usage_line()} ===")
    if not leads:
        return []
    revenue_mode = (os.environ.get("OFDATA_REVENUE_SOURCE") or "auto").strip().lower()
    if revenue_mode not in ("auto", "ofdata", "girbo"):
        raise OfDataSourceError(
            "OFDATA_REVENUE_SOURCE должен быть auto, ofdata или girbo")
    finance_cache = {}
    if revenue_mode == "auto":
        first = leads[0]
        try:
            first_finances = cli.finances(first["_inn"])
        except OfDataSourceError as exc:
            log(f"[!] OfData /finances недоступен ({exc}); переключаю выручку на ГИР БО ФНС")
            revenue_mode = "girbo"
        else:
            finance_cache[first["_inn"]] = first_finances
            balance = _number(getattr(cli, "balance", None))
            if finances_are_paywalled(first_finances):
                log("[!] OfData /finances явно сообщил «Недоступно для бесплатного тарифа»; "
                    "переключаю выручку на ГИР БО ФНС")
                revenue_mode = "girbo"
            elif not first_finances and balance is not None and balance <= 0:
                log("[!] OfData /finances вернул пусто при балансе 0 ₽; "
                    "переключаю выручку на ГИР БО ФНС")
                revenue_mode = "girbo"
            else:
                revenue_mode = "ofdata"

    kept, no_report, below, broken = [], [], [], []
    if revenue_mode == "girbo":
        from revenue_enrich import add_revenue

        log(f"=== ГИР БО ФНС: проверяем выручку {len(leads)} кандидатов; "
            f"строгий порог >= {threshold / 1e9:g} млрд ₽ ===")
        add_revenue(leads, log=log)
        for lead in leads:
            lead["source"] = "OfData API (/search) + ГИР БО ФНС (выручка)"
            if lead.get("_revenue_error"):
                broken.append(lead)
                continue
            revenue = _number(lead.get("_revenue"))
            if revenue is None or revenue <= 0:
                no_report.append(lead)
            elif revenue >= threshold:
                kept.append(lead)
            else:
                below.append(lead)
    else:
        log(f"=== OfData /finances: потребуется до {len(leads)} запросов; "
            f"строгий порог >= {threshold / 1e9:g} млрд ₽ ===")
        for index, lead in enumerate(leads, 1):
            try:
                finances = finance_cache.pop(lead["_inn"], None)
                if finances is None:
                    finances = cli.finances(lead["_inn"])
            except OfDataSourceError as exc:
                raise OfDataSourceError(
                    f"выручка для ИНН {lead['_inn']} не получена: {exc}. "
                    f"Поставь OFDATA_REVENUE_SOURCE=auto для fallback на ГИР БО.") from exc
            revenue, year = extract_latest_revenue(finances)
            if revenue is None:
                no_report.append(lead)
            else:
                lead["_revenue"] = revenue
                lead["_revenue_year"] = year
                lead["_revenue_src"] = "ofdata:finances:2110"
                lead["_revenue_source_name"] = "OfData / ГИР БО ФНС, строка 2110"
                lead["_revenue_source_url"] = FINANCES_DOC_URL
                lead["source"] = "OfData API (/search + /finances)"
                if revenue >= threshold:
                    kept.append(lead)
                else:
                    below.append(lead)
            if index % 50 == 0:
                log(f"  OfData выручка: проверено {index}/{len(leads)} | прошли {len(kept)}")

    kept.sort(key=lambda lead: -(lead.get("_revenue") or 0))
    log(f"=== Порог >= {threshold / 1e9:g} млрд: прошли {len(kept)} из {len(leads)} "
        f"(ниже: {len(below)}, нет отчётности/строки 2110: {len(no_report)}, "
        f"сбой проверки: {len(broken)}) ===")

    output, per = [], {}
    for lead in kept:
        industry = lead.get("_industry")
        if per.get(industry, 0) >= per_industry:
            continue
        per[industry] = per.get(industry, 0) + 1
        output.append(lead)

    if out_path:
        _save(output, out_path)
    return output


def _cli(argv):
    parser = argparse.ArgumentParser(
        description="Лиды из OfData API: ОКВЭД + выручка >= 1 млрд ₽ + контакты")
    parser.add_argument("--industries", default="", help="ключи INDUSTRY через запятую")
    parser.add_argument("--okved", default="", help="конкретные ОКВЭД через запятую")
    parser.add_argument("--min-revenue", type=float, default=MIN_REVENUE_FLOOR,
                        help="порог в рублях; значения ниже 1e9 автоматически поднимаются")
    parser.add_argument("--per-industry", type=int, default=40)
    parser.add_argument("--region", default=None)
    parser.add_argument("--max-candidates", type=int,
                        default=_env_int("OFDATA_MAX_CANDIDATES", 3000, minimum=1))
    parser.add_argument("--contacts-cap", type=int, default=0,
                        help="0 = карточка /company для всех прошедших фильтр")
    parser.add_argument("--no-contacts", action="store_true")
    parser.add_argument("--print-json", action="store_true",
                        help="вывести итоговый JSON лидов в терминал (ключ не выводится)")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    from source_rusprofile import INDUSTRY
    if args.okved:
        codes = [code.strip() for code in args.okved.split(",") if code.strip()]
        usable, coarse = usable_okved(codes)
        if coarse:
            log(f"[!] не удалось развернуть коды: {', '.join(coarse)}")
        if not usable:
            raise SystemExit("не осталось пригодных кодов ОКВЭД")
        base_cfg = next(iter(INDUSTRY.values()))
        INDUSTRY["_adhoc"] = {
            "label": "Произвольные ОКВЭД", "okved": usable,
            "pain": base_cfg.get("pain", ""), "offer": base_cfg.get("offer", ""),
        }
        industries = ["_adhoc"]
    else:
        industries = [part.strip() for part in args.industries.split(",") if part.strip()]
        if not industries:
            raise SystemExit("укажи --industries или --okved")

    client = OfDataClient()
    leads = harvest(
        industries, min_revenue=args.min_revenue, per_industry=args.per_industry,
        region=args.region, max_candidates=args.max_candidates, client=client,
    )
    if not args.no_contacts:
        ofdata_contacts_pass(client, leads, cap=args.contacts_cap)
    if args.out:
        _save(leads, args.out)
    if args.print_json:
        log("\n=== JSON результата OfData (без API-ключа) ===")
        log(json.dumps(leads, ensure_ascii=False, indent=2))
    for lead in leads[:20]:
        log(f"  {(lead.get('_revenue') or 0) / 1e9:6.2f} млрд  "
            f"{lead.get('_inn', ''):<12} {lead.get('name', '')[:52]}")
    log(f"\nИТОГО: {len(leads)} лидов | {client.usage_line()}")
    return 0 if leads else 1


if __name__ == "__main__":
    try:
        sys.exit(_cli(sys.argv[1:]))
    except OfDataSourceError as error:
        raise SystemExit(f"OfData: {error}") from error
