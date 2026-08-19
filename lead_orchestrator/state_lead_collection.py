# -*- coding: utf-8 -*-
"""Bounded-добор новых госкомпаний поверх CRM, RusProfile и ЕГРЮЛ.

Профиль отбора фиксирован кодом: выручка ≥2 млрд ₽ за 2025,
прямая/косвенная госдоля ≥25% (ЕГРЮЛ + Росимущество) и юрадрес в регионе
`STATE_LEAD_REGION` (дефолт — Татарстан). Регион отклоняется дёшево по выдаче
RusProfile, а ПРИНИМАЕТСЯ только по субъекту РФ из официальной выписки ЕГРЮЛ:
выписка без распознанного адреса — отказ (fail-closed), как и всё остальное здесь."""
from __future__ import annotations

import json
import os
import time

import source_rusprofile as SR
from inn_util import valid_inn


def load_local_registry_strict(path=None, log=SR.log):
    """Загрузить локальные дубли fail-closed и добавить готовые deliverables."""
    from outreach_registry import Registry, registry_path

    path = path or registry_path()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict) or not isinstance(raw.get("companies", {}), dict):
                raise ValueError("ожидался объект companies")
        except Exception as exc:
            raise RuntimeError(f"локальный реестр не читается: {path}: {exc}") from exc
    registry = Registry(path=path)
    registry.backfill_deliverables(log=log)
    return registry


def _with_session(session, requested_count, crm_index, local_registry, ownership_verifier,
                  max_pages, max_candidates, deadline, out_path, log,
                  ownership_error_limit, card_error_limit, region_query, region_code):
    from rusprofile_playwright import RusProfileCardSourceError, RusProfileDeadlineReached
    from state_ownership import (StateOwnershipDeadline, StateOwnershipSourceError,
                                 region_matches)

    if time.monotonic() >= deadline:
        raise SR.StateOwnershipUnavailable("общий лимит строгого добора истёк до RusProfile")
    min_revenue = 2_000_000_000
    revenue_year = 2025
    # Фильтр по штату (240–260) удалён 2026-08-19: критерий устарел.
    # Серверный фильтр региона критичен для воронки: без него выдача — топ РФ по
    # выручке, и Татарстана в первых 1000 строк единицы (боевой прогон: 24/1000).
    items = session.search(
        [], min_revenue, max_pages=max_pages, deadline=deadline,
        region_codes=[region_code] if (region_query and region_code) else None)
    if time.monotonic() >= deadline:
        raise RusProfileDeadlineReached(
            "общий лимит строгого добора истёк внутри поиска RusProfile")
    items = sorted(
        (item for item in items if isinstance(item, dict)),
        key=lambda item: SR.revenue_value(item.get("finance_revenue")) or -1,
        reverse=True,
    )[:max_candidates]
    stats = {
        "source_candidates": len(items), "crm_duplicate": 0, "local_duplicate": 0,
        "run_duplicate": 0,
        "invalid_inn": 0, "invalid_name": 0, "inactive": 0,
        "revenue": 0, "revenue_year": 0,
        "card_error": 0, "state_below_25": 0,
        "state_unknown": 0, "ownership_source_error": 0, "accepted": 0,
        "region_source": 0, "region_egrul": 0, "region_unknown": 0,
        "time_limit": 0,
    }
    selected, seen = [], set()
    consecutive_source_errors = 0
    consecutive_card_errors = 0

    for raw in items:
        if len(selected) >= requested_count:
            break
        if time.monotonic() >= deadline:
            stats["time_limit"] = 1
            break
        item = dict(raw)
        if not item.get("link") and item.get("url"):
            item["link"] = item["url"]
        inn = "".join(ch for ch in str(item.get("inn") or "") if ch.isdigit())
        if len(inn) != 10 or not valid_inn(inn):
            stats["invalid_inn"] += 1
            continue
        if inn in seen:
            stats["run_duplicate"] += 1
            continue
        seen.add(inn)
        if item.get("inactive"):
            stats["inactive"] += 1
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            stats["invalid_name"] += 1
            continue
        item["name"] = name

        # Дешёвый регион-фильтр по выдаче: чужой регион отклоняется ДО карточки
        # и ЕГРЮЛ. Пустой регион выдачи НЕ отклоняется — судьбу решит строгая
        # проверка юрадреса по выписке ниже.
        if region_query:
            source_region = str(item.get("region") or item.get("region_name") or "").strip()
            if source_region and not region_matches(source_region, region_query):
                stats["region_source"] += 1
                continue

        industry = SR._industry_for_okved(item.get("main_okved_id"))
        lead = SR.item_to_lead(item, SR.INDUSTRY[industry], industry)
        lead["_inn"] = inn
        if lead.get("_revenue") is None or lead["_revenue"] < min_revenue:
            stats["revenue"] += 1
            continue
        if crm_index.contains(lead):
            stats["crm_duplicate"] += 1
            continue
        if local_registry.is_worked(inn):
            stats["local_duplicate"] += 1
            continue

        # Сессия сама нормализует относительный /id/<n>; так тесты и браузер
        # используют тот же канонический link из выдачи.
        url = item.get("link") or lead.get("_rusprofile_url") or lead.get("_revenue_source_url")
        try:
            from rusprofile_playwright import canonical_card_url
            url = canonical_card_url(url).removeprefix("https://www.rusprofile.ru")
            if hasattr(session, "card_facts_by_url"):
                facts = session.card_facts_by_url(
                    url, expected_inn=inn, deadline=deadline)
                accepted_contacts = None
            else:
                facts = session.contacts_by_url(url, deadline=deadline)
                accepted_contacts = facts
        except RusProfileDeadlineReached:
            stats["time_limit"] = 1
            break
        except RusProfileCardSourceError as exc:
            stats["card_error"] += 1
            log(f"  [error] схема карточки {inn} недостоверна: {exc}")
            raise SR.StateOwnershipUnavailable(
                f"схема карточки RusProfile не подтверждена: {exc}") from exc
        except Exception as exc:
            stats["card_error"] += 1
            consecutive_card_errors += 1
            log(f"  [warn] карточка {inn} не разобрана: {type(exc).__name__}")
            if consecutive_card_errors >= card_error_limit:
                raise SR.StateOwnershipUnavailable(
                    f"карточки RusProfile не разобраны {consecutive_card_errors} "
                    f"раза подряд: {exc}") from exc
            continue
        consecutive_card_errors = 0
        SR._merge_state_contacts(lead, facts)
        if SR.integer_value(lead.get("_revenue_year")) != revenue_year:
            stats["revenue_year"] += 1
            continue
        card_revenue = SR.displayed_revenue_value(lead.get("_revenue_display"))
        if card_revenue is None or card_revenue < min_revenue:
            stats["revenue"] += 1
            continue
        # Значение и год теперь принадлежат одному блоку основной карточки.
        lead["_revenue"] = card_revenue

        try:
            result = ownership_verifier.verify(
                lead.get("name") or "", inn, deadline=deadline)
        except StateOwnershipDeadline:
            stats["time_limit"] = 1
            break
        except StateOwnershipSourceError as exc:
            stats["ownership_source_error"] += 1
            consecutive_source_errors += 1
            if consecutive_source_errors >= ownership_error_limit:
                raise SR.StateOwnershipUnavailable(
                    f"официальная проверка госучастия не выполнена "
                    f"{consecutive_source_errors} раза подряд: {exc}") from exc
            continue
        consecutive_source_errors = 0
        if not result.verified:
            stats["state_below_25" if result.complete else "state_unknown"] += 1
            continue

        # Строгий регион-гейт: принимается только юрадрес нужного субъекта РФ
        # из той же выписки ЕГРЮЛ, что доказала госдолю. Нет региона в выписке —
        # отказ: «не извлекли» не значит «Татарстан».
        if region_query:
            egrul_region = (result.region or "").strip()
            if not egrul_region:
                stats["region_unknown"] += 1
                continue
            if not region_matches(egrul_region, region_query):
                stats["region_egrul"] += 1
                continue

        if time.monotonic() >= deadline:
            stats["time_limit"] = 1
            break
        if accepted_contacts is None:
            try:
                accepted_contacts = session.contacts_by_url(url, deadline=deadline)
            except RusProfileDeadlineReached:
                stats["time_limit"] = 1
                break
            except Exception as exc:
                accepted_contacts = {}
                log(f"  [warn] контакты {inn} не разобраны: {type(exc).__name__}")
        if time.monotonic() >= deadline:
            stats["time_limit"] = 1
            break
        SR._merge_state_contacts(lead, accepted_contacts)
        lead.update({
            "_state_share": float(result.share),
            "_state_share_direct": float(result.direct_share),
            "_state_share_indirect": float(result.indirect_share),
            "_state_ownership_complete": bool(result.complete),
            "_state_ownership_urls": list(result.source_urls),
            "_state_ownership_trace": list(result.trace),
            "_state_ownership_reasons": list(result.reasons),
            "_egrul_region": (result.region or "").strip(),
        })
        selected.append(lead)
        stats["accepted"] = len(selected)
        log(
            f"  [принят {len(selected)}/{requested_count}] {lead['name']} | "
            f"ИНН {inn} | госдоля >={result.share}%")
        if out_path:
            SR._save(selected, out_path)

    log(
        f"[госкомпании] принято {len(selected)}/{requested_count} | "
        f"CRM-дублей {stats['crm_duplicate']} | локальных дублей {stats['local_duplicate']} | "
        f"не 2025 {stats['revenue_year']} | "
        f"госдоля <25 {stats['state_below_25']} | "
        f"ownership unknown {stats['state_unknown']} | "
        f"вне региона {stats['region_source'] + stats['region_egrul']} | "
        f"регион не подтверждён {stats['region_unknown']}")
    if len(selected) != requested_count:
        if out_path:
            SR._save(selected, out_path)
        raise SR.StateLeadExhausted(requested_count, len(selected), stats)
    return selected


def lead_region_query():
    """Требуемый регион юрадреса госкомпании; "" — фильтр выключен явно."""
    return str(os.environ.get("STATE_LEAD_REGION", "Татарстан") or "").strip()


def lead_region_code():
    """Код субъекта РФ для СЕРВЕРНОГО фильтра RusProfile (Татарстан — 16).

    Пустой ``STATE_LEAD_REGION_CODE=`` отключает только серверный фильтр:
    строгие гейты по выдаче и выписке ЕГРЮЛ работают независимо от него."""
    return str(os.environ.get("STATE_LEAD_REGION_CODE", "16") or "").strip()


def harvest_state_owned(requested_count, *, headless=False, offscreen=False,
                        session=None, crm_index=None, local_registry=None,
                        ownership_verifier=None,
                        max_pages=None, max_candidates=None, max_seconds=None,
                        deadline=None,
                        out_path=None, log=SR.log,
                        ownership_error_limit=3, card_error_limit=3,
                        region=None):
    """Ровно N новых компаний: CRM→RusProfile→2025→госдоля ≥25%→юрадрес региона.

    ``region`` (дефолт — ``STATE_LEAD_REGION`` = Татарстан) проверяется по субъекту РФ
    из выписки ЕГРЮЛ; пустая строка осознанно выключает фильтр (вся РФ)."""
    try:
        requested_count = int(requested_count)
    except (TypeError, ValueError) as exc:
        raise ValueError("requested_count должен быть положительным целым числом") from exc
    if requested_count <= 0:
        raise ValueError("requested_count должен быть положительным целым числом")
    if max_pages is None:
        max_pages = os.environ.get("STATE_LEAD_MAX_PAGES", str(SR.MAX_SEARCH_PAGES))
    try:
        max_pages = max(1, min(SR.MAX_SEARCH_PAGES, int(max_pages)))
    except (TypeError, ValueError) as exc:
        raise ValueError("STATE_LEAD_MAX_PAGES должен быть целым числом 1..20") from exc
    if max_candidates is None:
        max_candidates = os.environ.get("STATE_LEAD_MAX_CANDIDATES", "1000")
    if max_seconds is None:
        max_seconds = os.environ.get("STATE_LEAD_MAX_SECONDS", "1200")
    try:
        max_candidates = int(max_candidates)
        max_seconds = float(max_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("лимиты кандидатов/времени должны быть числами") from exc
    if max_candidates <= 0 or max_seconds <= 0:
        raise ValueError("лимиты кандидатов/времени должны быть положительными")
    if deadline is None:
        deadline = time.monotonic() + max_seconds
    else:
        deadline = float(deadline)
    ownership_error_limit = max(1, int(ownership_error_limit))
    card_error_limit = max(1, int(card_error_limit))
    region = lead_region_query() if region is None else str(region or "").strip()
    region_code = lead_region_code()
    log(f"[госкомпании] регион юрадреса (по выписке ЕГРЮЛ): {region or 'любой'}"
        + (f" | серверный фильтр выдачи: код {region_code}"
           if region and region_code else ""))

    # Прекондишен до Chrome: сбой CRM не расходует сессию RusProfile.
    if crm_index is None:
        from crm_push import fetch_existing_leads
        crm_index = fetch_existing_leads(deadline=deadline)
    log(f"[CRM] индекс существующих лидов загружен: {crm_index.total}")
    if local_registry is None:
        local_registry = load_local_registry_strict(log=log)
    if ownership_verifier is None:
        from state_ownership import OwnershipVerifier, RosimRegistry
        ownership_verifier = OwnershipVerifier(
            rosim=RosimRegistry.from_environment(deadline=deadline))

    args = (
        requested_count, crm_index, local_registry, ownership_verifier, max_pages,
        max_candidates, deadline, out_path, log, ownership_error_limit, card_error_limit,
        region, region_code,
    )
    if session is not None:
        if not hasattr(session, "contacts_by_url"):
            raise RuntimeError(
                "режим госкомпаний требует RUSPROFILE_BROWSER=playwright: "
                "UC-fallback не умеет читать карточки")
        return _with_session(session, *args)
    session_cls = SR._browser_session_class()
    with session_cls(
            headless=headless, offscreen=offscreen, deadline=deadline) as owned_session:
        return _with_session(owned_session, *args)
