# -*- coding: utf-8 -*-
r"""Стадия 9 outreach-пайплайна: отправленное письмо → раздел «Лиды» в CRM ЦИТ РТ.

Зачем: до этой стадии факт холодного касания жил только в `outreach_registry`
(JSON на машине, где крутилась рассылка). Менеджер, который берёт трубку после
письма, в этот файл не смотрит — ему нужен раздел в CRM. Стадия отдаёт CRM
компанию, добранные лидгеном данные и факт отправки; дальше менеджер работает
там: пишет итог холодного звонка и заводит сделку.

Контракт с CRM: `POST <CRM_URL>/api/leads/ingest`, заголовок `X-Ingest-Token`.
Upsert по ИНН — повторный прогон обновит запись, а не заведёт дубль. Статус,
до которого лид довёл менеджер (ответили / был звонок / отказ / сделка), CRM
повторной заливкой не откатывает.

⚠️ Стадия НИКОГДА не роняет рассылку. Письмо уже ушло — это необратимо, и упасть
после отправки означало бы потерять запись о ней в реестре. Любая проблема
(CRM не настроена, сеть, 500) возвращается как `(False, причина)` и уходит в лог
и в реестр, а прогон продолжается.

Настройка (env/.env):
  CRM_URL          базовый адрес CRM, напр. https://crm.example.ru или http://localhost:8000
  CRM_INGEST_TOKEN тот же токен, что LEADGEN_INGEST_TOKEN в .env самой CRM
  CRM_PUSH=0       выключить стадию, не трогая остальные ключи

CLI (ручная проверка связки, письма не шлёт):
  py crm_push.py --ping
  py crm_push.py --demo
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass

from inn_util import valid_inn, valid_ogrn

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TIMEOUT = float(os.environ.get("CRM_PUSH_TIMEOUT", "20"))


def _flag(name, default="1"):
    return (os.environ.get(name) or default).strip().lower() not in ("0", "false", "no", "")


def base_url():
    value = (os.environ.get("CRM_URL") or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    return value


def token():
    return (os.environ.get("CRM_INGEST_TOKEN") or "").strip()


def is_configured():
    """Настроена ли выгрузка в CRM. Пустые ключи — не ошибка: у пайплайна есть
    режимы, где CRM не нужна (dry-run, чужой стенд), и падать из-за них нельзя."""
    return bool(base_url() and token()) and _flag("CRM_PUSH")


class CRMIndexError(RuntimeError):
    """GET-индекс CRM нельзя использовать как надёжную прекондицию сбора."""


class CRMBatchError(RuntimeError):
    """Атомарная регистрация квалифицированного набора не состоялась."""


def _deadline_timeout(deadline, fallback=TIMEOUT):
    if deadline is None:
        return float(fallback)
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise CRMIndexError("общий лимит строгого добора истёк на CRM-прекондишене")
    return min(float(fallback), remaining)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _urlopen(req, timeout=TIMEOUT):
    """Не переносить X-Ingest-Token даже через HTTP redirect."""
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


def _digits(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _normal_name(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    text = re.sub(r"[\"'«»„“”`]+", "", text)
    text = " ".join(text.split())
    return re.sub(r"\s*([+&])\s*", r"\1", text).strip()


def _valid_org_inn(value):
    digits = _digits(value)
    return len(digits) == 10 and valid_inn(digits)


@dataclass(frozen=True)
class ExistingLeads:
    """Локальный снимок CRM: один GET, затем только детерминированные проверки."""
    inns: frozenset[str]
    ogrns: frozenset[str]
    legacy_names: frozenset[str]
    total: int

    def contains(self, lead):
        lead = lead or {}
        inn = _digits(lead.get("_inn") or lead.get("inn"))
        if _valid_org_inn(inn) and inn in self.inns:
            return True
        ogrn = _digits(lead.get("_ogrn") or lead.get("ogrn"))
        if len(ogrn) == 13 and valid_ogrn(ogrn) and ogrn in self.ogrns:
            return True
        name = _normal_name(lead.get("name") or lead.get("company_name"))
        return bool(name and name in self.legacy_names)


def _safe_index_error(value):
    text = str(value or "")
    secret = token()
    return text.replace(secret, "[REDACTED]") if secret else text


def _parse_existing_index(data):
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise CRMIndexError("CRM вернула неверную схему GET-индекса")
    items = data["items"]
    total = data.get("total")
    if not isinstance(total, int) or total != len(items):
        raise CRMIndexError("CRM вернула несогласованный total GET-индекса")

    inns, ogrns, legacy_names = set(), set(), set()
    for row in items:
        if not isinstance(row, dict) or not isinstance(row.get("id"), int):
            raise CRMIndexError("CRM вернула неверную строку GET-индекса")
        name = row.get("company_name")
        if not isinstance(name, str) or not name.strip():
            raise CRMIndexError("CRM вернула строку GET-индекса без названия")
        inn = _digits(row.get("inn"))
        ogrn = _digits(row.get("ogrn"))
        if _valid_org_inn(inn):
            inns.add(inn)
        else:
            legacy_names.add(_normal_name(name))
        if len(ogrn) == 13 and valid_ogrn(ogrn):
            ogrns.add(ogrn)
    return ExistingLeads(frozenset(inns), frozenset(ogrns),
                         frozenset(legacy_names), total)


def fetch_existing_leads(attempts=3, *, deadline=None):
    """Получить минимальный индекс лидов ДО RusProfile; при сомнении бросает ошибку.

    В отличие от мягкой стадии 9, здесь fail-open создал бы дубли и зря потратил
    лимит источника, поэтому отсутствие CRM/токена и любой неверный ответ фатальны.
    """
    if not (base_url() and token()):
        raise CRMIndexError("CRM не настроена (нет CRM_URL / CRM_INGEST_TOKEN)")

    req = urllib.request.Request(f"{base_url()}/api/leads/ingest/index", method="GET")
    req.add_header("Accept", "application/json")
    req.add_header("X-Ingest-Token", token())
    attempts = max(1, int(attempts or 1))
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            with _urlopen(req, timeout=_deadline_timeout(deadline)) as resp:
                raw = resp.read(5 * 1024 * 1024 + 1)
            if len(raw) > 5 * 1024 * 1024:
                raise CRMIndexError("GET-индекс CRM превышает 5 МиБ")
            return _parse_existing_index(json.loads(raw.decode("utf-8", "replace")))
        except CRMIndexError:
            raise
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8", "replace")).get("detail") or ""
            except Exception:
                pass
            last_error = CRMIndexError(_safe_index_error(
                f"CRM GET-индекс HTTP {exc.code}: {detail}"))
            transient = exc.code == 429 or 500 <= exc.code < 600
            if not transient or attempt >= attempts:
                raise last_error from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = CRMIndexError(_safe_index_error(
                f"CRM GET-индекс недоступен: {getattr(exc, 'reason', None) or exc}"))
            if attempt >= attempts:
                raise last_error from None
        except (ValueError, TypeError) as exc:
            raise CRMIndexError(_safe_index_error(
                f"CRM вернула невалидный JSON GET-индекса: {exc}")) from None
        delay = min(2.0, 0.5 * attempt)
        if deadline is not None and delay >= _deadline_timeout(deadline):
            raise CRMIndexError("общий лимит строгого добора истёк перед повтором CRM")
        time.sleep(delay)
    raise last_error or CRMIndexError("CRM GET-индекс недоступен")


def build_payload(lead, sent=None, *, draft=False, onepager=""):
    """Лид пайплайна + факт отправки -> тело запроса CRM.

    Служебные ключи лида (`_inn`, `_industry`, …) в контракт наружу не торчат:
    CRM про внутренние имена лидгена знать не должна, маппинг живёт здесь."""
    lead = lead or {}
    sent = sent or {}
    payload = {
        "inn": str(lead.get("_inn") or lead.get("inn") or "").strip() or None,
        "ogrn": str(lead.get("_ogrn") or lead.get("ogrn") or "").strip() or None,
        "company_name": (lead.get("name") or "").strip(),
        "website": lead.get("website") or None,
        "phone": lead.get("phone") or None,
        "email": lead.get("email") or None,
        "industry": lead.get("_industry") or None,
        "region": lead.get("_region") or lead.get("region") or None,
        "revenue": lead.get("_revenue") or None,
        "revenue_year": str(lead.get("_revenue_year") or "").strip() or None,
        "contact_person": lead.get("contact_person") or None,
        "contact_post": lead.get("_ceo_post") or None,
        "sent_to": sent.get("to") or None,
        "sent_subject": sent.get("subject") or None,
        "sent_at": sent.get("at") or None,
        "sent_draft": bool(draft),
        "onepager": os.path.basename(onepager) if onepager else None,
        "note": lead.get("_crm_note") or None,
    }
    return {k: v for k, v in payload.items() if v is not None}


def create_researched_batch(leads, run_id, attempts=3):
    """Атомарный create-only ingest после успешной Фазы 2."""
    if not is_configured():
        raise CRMBatchError("CRM не настроена (нет CRM_URL / CRM_INGEST_TOKEN)")
    try:
        run_id = str(uuid.UUID(str(run_id)))
    except (ValueError, AttributeError):
        raise CRMBatchError("run_id должен быть UUID") from None
    leads = list(leads or [])
    if not 1 <= len(leads) <= 200:
        raise CRMBatchError("batch должен содержать 1..200 лидов")

    items, inns = [], set()
    for lead in leads:
        item = build_payload(lead)
        inn = _digits(item.get("inn"))
        if not _valid_org_inn(inn) or not item.get("company_name"):
            raise CRMBatchError("каждый исследованный лид требует название и валидный ИНН")
        if inn in inns:
            raise CRMBatchError(f"ИНН {inn} повторён внутри batch")
        inns.add(inn)
        for key in ("sent_to", "sent_subject", "sent_at", "sent_draft"):
            item.pop(key, None)
        items.append(item)

    body = json.dumps({"run_id": run_id, "items": items}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url()}/api/leads/ingest/researched-batch", data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Accept", "application/json")
    req.add_header("X-Ingest-Token", token())
    last = None
    attempts = max(1, int(attempts))
    for attempt in range(1, attempts + 1):
        try:
            with _urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            ids = data.get("ids")
            replayed = data.get("replayed") is True
            created = data.get("created")
            if (data.get("run_id") != run_id or not isinstance(ids, list)
                    or len(ids) != len(leads) or not all(isinstance(value, int) for value in ids)
                    or (replayed and created != 0) or (not replayed and created != len(leads))):
                raise CRMBatchError("CRM вернула неверную схему atomic batch")
            return data
        except CRMBatchError:
            raise
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8", "replace")).get("detail") or ""
            except Exception:
                pass
            safe = _safe_index_error(detail)
            if exc.code == 409:
                raise CRMBatchError(f"CRM atomic batch conflict: {safe}") from None
            last = CRMBatchError(f"CRM atomic batch HTTP {exc.code}: {safe}")
            if not (exc.code == 429 or 500 <= exc.code < 600) or attempt >= attempts:
                raise last from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = CRMBatchError(_safe_index_error(f"CRM atomic batch недоступен: {exc}"))
            if attempt >= attempts:
                raise last from None
        time.sleep(min(2.0, 0.5 * attempt))
    raise last or CRMBatchError("CRM atomic batch недоступен")


def push_lead(lead, sent=None, *, draft=False, onepager=""):
    """Завести/обновить лид в CRM. -> (ok, причина). Исключений не бросает."""
    if not is_configured():
        return False, "CRM не настроена (нет CRM_URL / CRM_INGEST_TOKEN)"

    payload = build_payload(lead, sent, draft=draft, onepager=onepager)
    if not payload.get("company_name"):
        return False, "у лида нет названия компании"

    url = f"{base_url()}/api/leads/ingest"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Accept", "application/json")
    req.add_header("X-Ingest-Token", token())

    try:
        with _urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8", "replace")).get("detail") or ""
        except Exception:
            pass
        if e.code in (401, 403):
            return False, f"CRM отвергла токен ({e.code}): {detail or 'проверь CRM_INGEST_TOKEN'}"
        if e.code == 503:
            return False, f"в CRM выключен приём лидов: {detail or 'не задан LEADGEN_INGEST_TOKEN'}"
        return False, f"CRM HTTP {e.code}: {str(detail)[:160]}"
    except urllib.error.URLError as e:
        return False, f"CRM недоступна: {e.reason}"
    except Exception as e:                          # noqa: BLE001 — стадия не валит рассылку
        return False, f"CRM: {type(e).__name__}: {str(e)[:140]}"

    lead_id = data.get("id")
    if not lead_id:
        return False, f"неожиданный ответ CRM: {str(data)[:160]}"
    what = "заведён" if data.get("created") else "обновлён"
    return True, f"лид #{lead_id} {what} ({data.get('status') or '—'})"


def lead_url(lead_id=None):
    """Ссылка на раздел «Лиды» — её печатает стадия 8 в отчёте."""
    root = base_url()
    return f"{root}/leads" if root else ""


def ping():
    """Жива ли CRM и принимает ли она наш токен. Данных НЕ создаёт. -> (ok, причина).

    Токен проверяется пустым телом: CRM сперва спрашивает заголовок, и только потом
    валидирует payload. Значит 422 = «токен принят, просто нечего заводить», 401 =
    «токен не тот». Слать ради проверки настоящий лид нельзя — в разделе «Лиды»
    у менеджера появился бы мусор."""
    if not is_configured():
        return False, "CRM не настроена (нет CRM_URL / CRM_INGEST_TOKEN)"

    req = urllib.request.Request(f"{base_url()}/health", method="GET")
    try:
        with _urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        return False, f"CRM отвечает HTTP {e.code} на /health"
    except Exception as e:                          # noqa: BLE001
        return False, f"CRM недоступна: {getattr(e, 'reason', None) or e}"

    req = urllib.request.Request(f"{base_url()}/api/leads/ingest", data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("X-Ingest-Token", token())
    try:
        with _urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
        return False, "CRM приняла пустой лид — проверь версию приёмника"
    except urllib.error.HTTPError as e:
        if e.code == 422:
            return True, "CRM отвечает, токен принят"
        if e.code in (401, 403):
            return False, f"CRM отвергла токен ({e.code}) — сверь CRM_INGEST_TOKEN с LEADGEN_INGEST_TOKEN"
        if e.code == 503:
            return False, "в CRM выключен приём лидов (не задан LEADGEN_INGEST_TOKEN)"
        return False, f"CRM HTTP {e.code} на /api/leads/ingest"
    except Exception as e:                          # noqa: BLE001
        return False, f"CRM недоступна: {getattr(e, 'reason', None) or e}"


# ------------------------------------------------------------------- CLI -----
def main():
    import argparse

    try:
        from project_env import load_project_env
        load_project_env()
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="стадия 9: выгрузка лида в CRM ЦИТ РТ")
    ap.add_argument("--ping", action="store_true", help="проверить настройку и доступность CRM")
    ap.add_argument("--demo", action="store_true", help="залить тестовый лид (ИНН 0000000000)")
    args = ap.parse_args()

    if not (args.ping or args.demo):
        ap.print_help()
        return 0

    print(f"CRM_URL          = {base_url() or '— не задан —'}")
    print(f"CRM_INGEST_TOKEN = {'задан' if token() else '— не задан —'}")
    print(f"стадия 9         = {'включена' if is_configured() else 'выключена'}")
    if not is_configured():
        return 2

    if args.ping:
        ok, note = ping()
        print(f"[ping] {'OK' if ok else 'СБОЙ'}: {note}")
        if not args.demo:
            return 0 if ok else 3

    demo_lead = {
        "name": "ООО «Демо-компания» (тест стадии 9)",
        "_inn": "0000000000",
        "website": "https://example.com",
        "phone": "+7 (000) 000-00-00",
        "_industry": "processing",
        "_revenue": 1234000000,
        "_revenue_year": "2025",
        "contact_person": "Иванов Иван Иванович",
        "_ceo_post": "Генеральный директор",
    }
    ok, note = push_lead(
        demo_lead,
        {"to": "ivanov@example.com", "subject": "Проверка стадии 9", "at": None},
        draft=True,
    )
    print(f"[demo] {'OK' if ok else 'СБОЙ'}: {note}")
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
