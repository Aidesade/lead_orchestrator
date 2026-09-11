# -*- coding: utf-8 -*-
"""Bounded-добор новых госкомпаний поверх CRM, RusProfile и ЕГРЮЛ.

Профиль отбора фиксирован кодом: выручка ≥2 млрд ₽ за 2025
(порог — цифра профиля с тумблером, `STATE_LEAD_MIN_REVENUE`), среднесписочная
численность ≥50 за 2025 по карточке RusProfile (тумблер общий с обычным сбором —
`LEAD_MIN_STAFF`, правило — `SR.staff_verdict`),
прямая/косвенная госдоля ≥25% (ЕГРЮЛ + Росимущество) и юрадрес в регионе
`STATE_LEAD_REGION` (дефолт — Татарстан). Регион отклоняется дёшево по выдаче
RusProfile, а ПРИНИМАЕТСЯ только по субъекту РФ из официальной выписки ЕГРЮЛ:
выписка без распознанного адреса — отказ (fail-closed), как и всё остальное здесь."""
from __future__ import annotations

import json
import os
import time

import site_verify as SV
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


def _with_session(session, requested_count, crm_index, client_index, local_registry, ownership_verifier,
                  max_pages, max_candidates, deadline, out_path, log,
                  ownership_error_limit, card_error_limit, region_query, region_code,
                  verify_ownership, tz_limit, min_staff, city_query):
    from rusprofile_playwright import RusProfileCardSourceError, RusProfileDeadlineReached
    from state_ownership import (StateOwnershipDeadline, StateOwnershipSourceError,
                                 region_matches)

    if time.monotonic() >= deadline:
        raise SR.StateOwnershipUnavailable("общий лимит строгого добора истёк до RusProfile")
    min_revenue = lead_min_revenue()
    revenue_year = 2025
    # ССЧ читается с карточки (блок «Среднесписочная численность»), гейт — нижняя
    # граница за тот же 2025 год, что и выручка. Прежнее окно штата 240–260
    # удалено 2026-08-19; порог «от 50» введён 2026-09-03 (`LEAD_MIN_STAFF`).
    # Серверный фильтр региона критичен для воронки: без него выдача — топ РФ по
    # выручке, и Татарстана в первых 1000 строк единицы (боевой прогон: 24/1000).
    regions = split_regions(region_query)
    codes = split_regions(region_code)
    # Город — второй, более узкий срез поверх субъекта: серверный фильтр
    # RusProfile умеет только код региона, населённый пункт режем сами.
    cities = SR.city_patterns(city_query)

    def in_region(value):
        """Регион подходит, если совпал ХОТЯ БЫ с одним из перечисленных."""
        return any(region_matches(value, name) for name in regions)

    items = session.search(
        [], min_revenue, max_pages=max_pages, deadline=deadline,
        region_codes=codes if (regions and codes) else None)
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
        "client_duplicate": 0,
        "run_duplicate": 0,
        "invalid_inn": 0, "invalid_name": 0, "inactive": 0,
        "revenue": 0, "revenue_year": 0,
        "staff_below": 0, "staff_unknown": 0,
        "card_error": 0, "state_below_25": 0,
        "state_unknown": 0, "ownership_source_error": 0, "accepted": 0,
        "region_source": 0, "region_egrul": 0, "region_unknown": 0,
        "city_source": 0,
        "site_no_site": 0, "site_unreachable": 0, "site_no_phone": 0,
        "tz_far": 0, "tz_unknown": 0,
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
        # и ЕГРЮЛ. Пустой регион выдачи в ownership-режиме НЕ отклоняется — судьбу
        # решит строгая проверка юрадреса по выписке; БЕЗ ownership выписки не
        # будет, поэтому пустой регион при заданном фильтре = отказ (fail-closed).
        source_region = str(item.get("region") or item.get("region_name") or "").strip()
        if regions:
            if source_region and not in_region(source_region):
                stats["region_source"] += 1
                continue
            if not verify_ownership and not source_region:
                stats["region_unknown"] += 1
                continue

        # Город юрадреса — по адресу выдачи и тоже ДО карточки. Пустой адрес
        # при заданном городе = отказ (fail-closed, как пустой регион): выписка
        # ЕГРЮЛ подтверждает субъект, но не населённый пункт.
        if cities and not SR.city_matches(item.get("address"), cities):
            stats["city_source"] += 1
            continue

        # Часовой пояс: по региону выдачи, отклонение от МСК не больше tz_limit
        # часов. Регион без известного офсета (или пустой) — отказ: «не поняли,
        # где компания» не значит «в нашем поясе».
        if tz_limit is not None:
            offset = region_tz_offset(source_region)
            if offset is None:
                stats["tz_unknown"] += 1
                continue
            if abs(offset - 3) > tz_limit:
                stats["tz_far"] += 1
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
        # Клиент отсекается ДО карточки: с ним уже работают, и ресёрч по нему —
        # трата лимита RusProfile и денег на модель без единого шанса на сделку.
        if client_index is not None and client_index.contains(lead):
            stats["client_duplicate"] += 1
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
        if min_staff:
            verdict = SR.staff_verdict(lead, min_staff)
            if verdict != "ok":
                stats["staff_below" if verdict == "below" else "staff_unknown"] += 1
                continue

        result = None
        if verify_ownership:
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
            if regions:
                egrul_region = (result.region or "").strip()
                if not egrul_region:
                    stats["region_unknown"] += 1
                    continue
                if not in_region(egrul_region):
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
        # Сайт и телефоны — последний гейт, уже по контактам карточки: компания
        # без живого сайта или без опубликованных на нём телефонов в добор не
        # идёт (решение 2026-09-11). Отрасль ЖКХ стадию не проходит вовсе —
        # телефоны там добываются иначе, см. site_verify.
        site_verdict = SV.verify_lead(lead, deadline=deadline)
        if site_verdict == "deadline":
            stats["time_limit"] = 1
            break
        if site_verdict in SV.DROP_VERDICTS:
            stats[f"site_{site_verdict}"] += 1
            continue
        lead["_source_region"] = source_region
        if result is not None:
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
            f"ИНН {inn}"
            + (f" | госдоля >={result.share}%" if result is not None
               else f" | {source_region or 'регион не указан'}"))
        if out_path:
            SR._save(selected, out_path)

    log(
        f"[госкомпании] принято {len(selected)}/{requested_count} | "
        f"CRM-дублей {stats['crm_duplicate']} | клиентов {stats['client_duplicate']} | "
        f"локальных дублей {stats['local_duplicate']} | "
        f"не 2025 {stats['revenue_year']} | "
        f"ССЧ <{min_staff} {stats['staff_below']} | "
        f"ССЧ не {SR.STAFF_YEAR} {stats['staff_unknown']} | "
        f"госдоля <25 {stats['state_below_25']} | "
        f"ownership unknown {stats['state_unknown']} | "
        f"вне региона {stats['region_source'] + stats['region_egrul']} | "
        f"регион не подтверждён {stats['region_unknown']} | "
        + (f"вне города {stats['city_source']} | " if cities else "")
        + (f"без сайта {stats['site_no_site']} | "
           f"сайт не отвечает {stats['site_unreachable']} | "
           f"без телефонов на сайте {stats['site_no_phone']} | " if SV.enabled() else "")
        + f"чужой пояс {stats['tz_far']} | пояс неизвестен {stats['tz_unknown']}")
    if len(selected) != requested_count:
        if out_path:
            SR._save(selected, out_path)
        raise SR.StateLeadExhausted(requested_count, len(selected), stats)
    return selected


# Субъекты РФ с часовым поясом, ОТЛИЧНЫМ от «прочей» европейской России (UTC+3).
# Подстрочный матч по названию региона из выдачи RusProfile; всё, что не в
# таблице, считается UTC+3. Достаточно для гейта |офсет-3| <= N: точность нужна
# только на границах, а не для каждой области Центральной России.
_REGION_TZ = {
    "калининград": 2,
    "самарск": 4, "удмурт": 4, "ульяновск": 4, "саратов": 4, "астрахан": 4,
    "башкорт": 5, "пермск": 5, "оренбург": 5, "свердловск": 5, "челябинск": 5,
    "курган": 5, "тюмен": 5, "ханты": 5, "югра": 5, "ямал": 5,
    "омск": 6,
    "алтай": 7, "кемеров": 7, "кузбасс": 7, "красноярск": 7,
    "новосибирск": 7, "томск": 7, "тыва": 7, "хакас": 7,
    "иркутск": 8, "бурят": 8,
    "саха": 9, "якут": 9, "амурск": 9, "забайкал": 9,
    "приморск": 10, "хабаровск": 10, "еврейск": 10,
    "магадан": 11, "сахалин": 11,
    "камчат": 12, "чукот": 12,
}


def region_tz_offset(region_text):
    """UTC-офсет по названию субъекта из выдачи; None — регион не распознан."""
    low = str(region_text or "").strip().lower().replace("ё", "е")
    if not low:
        return None
    for marker, offset in _REGION_TZ.items():
        if marker in low:
            return offset
    return 3


def ownership_enabled():
    """Проверять ли госдолю (STATE_LEAD_OWNERSHIP, дефолт ВКЛ).

    `=0` превращает строгий добор в «крупные компании по выручке и региону»:
    гейты выручки/2025, дедуп CRM+реестр и регион-фильтры работают, ЕГРЮЛ и
    Росимущество не вызываются вовсе."""
    return (os.environ.get("STATE_LEAD_OWNERSHIP", "1").strip().lower()
            not in ("0", "false", "no", "off", "нет"))


def lead_tz_limit():
    """Макс. |отклонение| часового пояса региона от МСК; None — фильтр выключен."""
    raw = (os.environ.get("STATE_LEAD_TZ_LIMIT") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("STATE_LEAD_TZ_LIMIT должен быть целым числом часов") from exc
    if value < 0:
        raise ValueError("STATE_LEAD_TZ_LIMIT должен быть неотрицательным")
    return value


def lead_region_query():
    """Требуемый регион юрадреса госкомпании (`STATE_LEAD_REGION`, дефолт Татарстан).

    ``*``/«любой» — явный отключатель фильтра (см. `SR.geo_query`)."""
    return SR.geo_query("STATE_LEAD_REGION", "Татарстан")


def split_regions(value):
    """«Татарстан, Башкортостан» -> ["Татарстан", "Башкортостан"].

    Округ — это не один субъект: чтобы собрать по ПФО, гейт обязан принимать
    ЛЮБОЙ из перечисленных регионов. Пустой список = фильтр выключен."""
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def lead_region_code():
    """Код субъекта РФ для СЕРВЕРНОГО фильтра RusProfile (Татарстан — 16).

    Пустой ``STATE_LEAD_REGION_CODE=`` отключает только серверный фильтр:
    строгие гейты по выдаче и выписке ЕГРЮЛ работают независимо от него."""
    return str(os.environ.get("STATE_LEAD_REGION_CODE", "16") or "").strip()


def lead_city_query():
    """Населённый пункт юрадреса внутри региона (`STATE_LEAD_CITY`); пусто — фильтра нет."""
    return SR.geo_query("STATE_LEAD_CITY")


def lead_min_revenue():
    """Порог выручки строгого добора (`STATE_LEAD_MIN_REVENUE`, дефолт 2 млрд ₽).

    Тумблер нужен там, где субъект мал: критерий «≥2 млрд» выведен на Татарстане,
    а в регионе поменьше он оставляет от воронки десятки компаний — замер по
    Башкортостану 2026-08-26: 586 компаний ≥1 млрд на весь субъект.

    Ниже общего пола лидгена (`MIN_REVENUE_FLOOR`, 1 млрд) опускаться некуда:
    серверный фильтр RusProfile дешевле не отдаёт, и «порог» превратился бы
    в тихую фикцию. Значение невалидно -> ValueError, а не молчаливый дефолт:
    опечатка в пороге меняет ВЕСЬ отбор и обязана останавливать прогон."""
    raw = str(os.environ.get("STATE_LEAD_MIN_REVENUE", "") or "").strip()
    if not raw:
        return 2_000_000_000
    try:
        value = float(raw.replace(" ", "").replace(",", "."))
    except ValueError as exc:
        raise ValueError(
            "STATE_LEAD_MIN_REVENUE должен быть числом рублей (напр. 1e9)") from exc
    if value < SR.MIN_REVENUE_FLOOR:
        raise ValueError(
            f"STATE_LEAD_MIN_REVENUE={raw} ниже общего пола лидгена "
            f"({int(SR.MIN_REVENUE_FLOOR)} ₽) — источник такие компании не отдаёт")
    return int(value)


def harvest_state_owned(requested_count, *, headless=False, offscreen=False,
                        session=None, crm_index=None, client_index=None, local_registry=None,
                        ownership_verifier=None,
                        max_pages=None, max_candidates=None, max_seconds=None,
                        deadline=None,
                        out_path=None, log=SR.log,
                        ownership_error_limit=3, card_error_limit=3,
                        region=None, verify_ownership=None, tz_limit=None,
                        min_staff=None, city=None):
    """Ровно N новых компаний: CRM→RusProfile→2025→ССЧ→[госдоля ≥25%]→регион/пояс.

    ``region`` (дефолт — ``STATE_LEAD_REGION`` = Татарстан) проверяется по субъекту РФ
    из выписки ЕГРЮЛ; пустая строка осознанно выключает фильтр (вся РФ).
    ``verify_ownership`` (дефолт — ``STATE_LEAD_OWNERSHIP``) выключает проверку
    госдоли целиком; без неё регион-фильтр работает по выдаче RusProfile.
    ``tz_limit`` (дефолт — ``STATE_LEAD_TZ_LIMIT``) — макс. |отклонение| часового
    пояса региона от МСК в часах; None — пояс не проверяется.
    ``min_staff`` (дефолт — ``LEAD_MIN_STAFF``, 50) — нижняя граница ССЧ за
    ``SR.STAFF_YEAR`` по карточке; 0 выключает гейт.
    ``city`` (дефолт — ``STATE_LEAD_CITY``) сужает отбор до населённого пункта
    юрадреса ВНУТРИ региона: серверный фильтр RusProfile города не знает."""
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
    city = lead_city_query() if city is None else str(city or "").strip()
    if verify_ownership is None:
        verify_ownership = ownership_enabled()
    tz_limit = lead_tz_limit() if tz_limit is None else int(tz_limit)
    min_staff = SR.lead_min_staff() if min_staff is None else max(0, int(min_staff))
    log("[госкомпании] госдоля: "
        + (">=25% (ЕГРЮЛ + Росимущество)" if verify_ownership else "НЕ проверяется")
        + (f" | ССЧ >={min_staff} за {SR.STAFF_YEAR}" if min_staff else " | ССЧ НЕ проверяется")
        + f" | регион: {region or 'любой'}"
        + (f" (по {'выписке ЕГРЮЛ' if verify_ownership else 'выдаче RusProfile'})"
           if region else "")
        + (f" | город: {city} (по юрадресу выдачи)" if city else "")
        + (f" | серверный фильтр выдачи: код {region_code}"
           if region and region_code else "")
        + (f" | часовой пояс: МСК±{tz_limit} ч" if tz_limit is not None else ""))

    # Прекондишен до Chrome: сбой CRM не расходует сессию RusProfile.
    from crm_push import client_facts
    if crm_index is None:
        # CRM целиком на нас — значит и клиентов добираем сами.
        from crm_push import fetch_existing_leads, fetch_existing_clients
        crm_index = fetch_existing_leads(deadline=deadline)
        if client_index is None:
            client_index = fetch_existing_clients(deadline=deadline)
    log(f"[CRM] индекс существующих лидов загружен: {crm_index.total}")
    # Индекс лидов пришёл снаружи -> клиентский обязан прийти оттуда же: лезть за
    # ним в сеть за спиной вызывающего нельзя, иначе офлайн-тесты и разовые вызовы
    # начнут ходить в CRM. Что боевой вход его передаёт — держит test_state_owned_wiring.
    if client_index is not None:
        log(client_facts(client_index))
    if local_registry is None:
        local_registry = load_local_registry_strict(log=log)
    if ownership_verifier is None and verify_ownership:
        from state_ownership import OwnershipVerifier, RosimRegistry
        ownership_verifier = OwnershipVerifier(
            rosim=RosimRegistry.from_environment(deadline=deadline))

    args = (
        requested_count, crm_index, client_index, local_registry, ownership_verifier, max_pages,
        max_candidates, deadline, out_path, log, ownership_error_limit, card_error_limit,
        region, region_code, verify_ownership, tz_limit, min_staff, city,
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
