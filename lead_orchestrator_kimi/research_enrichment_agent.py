# -*- coding: utf-8 -*-
"""Пять Kimi-субагентов дополнительного ресёрча компании.

Роли выполняются не плоским веером, а по графу данных:

    official_sources
           ↓
    corporate_contour + secondary_sources
           ↓
       role_candidates
           ↓
      candidate_contacts

Каждая роль работает в отдельной SDK-сессии с read-only LeadSearch/LeadFetch/LeadCrawl.
Ответ проходит машинную проверку контракта и доказательств, после чего атомарно попадает
в версионированный checkpoint. При повторном запуске выполняются только отсутствующие или
инвалидированные роли и их downstream-зависимости.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import pathlib
import re
import sys
import warnings
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

# fastmcp тянет authlib.jose, который на каждый импорт шлёт AuthlibDeprecationWarning в stderr
# (сам authlib форсит simplefilter("always")). Это НЕ фатально, но раньше шум забивал хвост
# stderr и маскировал настоящую причину падения в родительском сообщении. Настоящая причина
# теперь берётся из stdout (см. writer_kimi.run_research_subagents); здесь глушим по мере сил.
warnings.filterwarnings("ignore", message=r".*authlib\.jose.*")


HERE = pathlib.Path(__file__).resolve().parent
AGENTS = HERE / "kimi_agent"
SCHEMA_VERSION = 3
PROMPT_VERSION = "2026-07-21.3"
EVIDENCE_POLICY_VERSION = "2026-07-21.3"

EXECUTION_WAVES = (
    ("official_sources",),
    ("corporate_contour", "secondary_sources"),
    ("role_candidates",),
    ("candidate_contacts",),
)
ROLE_ORDER = tuple(role for wave in EXECUTION_WAVES for role in wave)
ROLE_DEPENDENCIES = {
    "official_sources": (),
    "corporate_contour": ("official_sources",),
    "secondary_sources": ("official_sources",),
    "role_candidates": ("official_sources", "corporate_contour", "secondary_sources"),
    "candidate_contacts": ("official_sources", "corporate_contour", "role_candidates"),
}

STATUSES = frozenset({"confirmed", "probable", "historical", "unverified", "conflicting"})
OFFICIAL_SOURCE_TYPES = frozenset({
    "official_company_site", "official_holding_site", "government_registry",
    "government_disclosure", "official_tender", "regulation", "project_documentation",
    "reporting", "official_appointment_news", "corporate_pdf",
})
SECONDARY_SOURCE_TYPES = frozenset({
    "business_media", "industry_media", "university", "conference", "vacancy",
    "professional_profile", "directory", "business_aggregator", "social_media", "archive",
})
CONTOUR_RELATIONS = frozenset({
    "management_company", "parent_company", "holding", "centralized_service_center",
    "it_automation", "procurement_center", "financial_service_center", "production_branch",
})
DECISION_CENTER_TYPES = frozenset({
    "target", "management_company", "holding", "parent_company", "service_center",
    "it_company", "procurement_center", "unknown",
})
TARGET_FUNCTIONS = frozenset({
    "ceo", "owner", "technical", "chief_engineer", "cio_it", "automation", "digital",
    "commercial", "procurement", "supply", "finance", "legal", "hr", "production",
    "geology", "hse", "branch_management",
})
CONTACT_KINDS = frozenset({
    "personal_work", "functional_inbox", "corporate_inbox", "reception_phone",
    "branch_address", "official_professional_profile", "backup_channel",
})
ROUTING_CONTACT_KINDS = frozenset({
    "functional_inbox", "corporate_inbox", "reception_phone", "branch_address", "backup_channel",
})
CONTACT_SOURCE_CONTEXTS = frozenset({
    "official_company_site", "official_holding_site", "government_registry", "official_tender",
    "vacancy", "business_media", "professional_profile", "social_media",
    "business_aggregator", "other_public_source",
})
OUTREACH_POLICIES = frozenset({
    "direct_allowed", "routing_only", "internal_verification_only", "do_not_cold_outreach",
})
CHECK_OUTCOMES = frozenset({"evidence_found", "no_relevant_data", "unavailable"})

BUSINESS_AGGREGATOR_HOST_MARKERS = (
    "checko.ru", "checko.com", "rusprofile.ru", "list-org.com", "zachestnyibiznes.ru",
    "audit-it.ru", "spark-interfax.ru", "focus.kontur.ru", "sbis.ru", "saby.ru",
    "b2b.house", "synapsenet.ru", "companies.rbc.ru",
)
VACANCY_HOST_MARKERS = ("hh.ru", "superjob.ru", "rabota.ru")
PROFESSIONAL_PROFILE_HOST_MARKERS = ("linkedin.com",)
SOCIAL_MEDIA_HOST_MARKERS = (
    "vk.com", "ok.ru", "facebook.com", "instagram.com", "t.me",
)
PUBLIC_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "mail.ru", "bk.ru", "inbox.ru", "list.ru",
    "yandex.ru", "ya.ru", "rambler.ru", "outlook.com", "hotmail.com", "icloud.com",
})

# Домены, которые нельзя выдавать за официальный первоисточник. Список намеренно содержит
# только однозначные каталоги/агрегаторы/соцсети; неизвестный домен дополнительно должен быть
# либо заявлен агентом как официальный, либо относиться к известному госисточнику.
NON_OFFICIAL_HOST_MARKERS = (
    "checko.ru", "checko.com", "rusprofile.ru", "list-org.com", "zachestnyibiznes.ru", "audit-it.ru",
    "spark-interfax.ru", "focus.kontur.ru", "sbis.ru", "saby.ru", "tbank.ru",
    "b2b.house", "synapsenet.ru", "companies.rbc.ru", "hh.ru", "linkedin.com",
    "vk.com", "ok.ru", "facebook.com", "instagram.com", "t.me",
    "rbc.ru", "interfax.ru", "kommersant.ru", "vedomosti.ru", "forbes.ru", "tass.ru",
    "ria.ru", "cnews.ru", "tadviser.ru",
)
GOVERNMENT_OFFICIAL_HOST_MARKERS = (
    "gov.ru", "nalog.ru", "fedresurs.ru", "rosstat.gov.ru", "kad.arbitr.ru",
    "e-disclosure.ru", "disclosure.1prime.ru", "tatarstan.ru", "mos.ru", "gov.spb.ru",
    "publication.pravo.gov.ru", "pravo.gov.ru",
)
TENDER_OFFICIAL_HOST_MARKERS = (
    "zakupki.gov.ru", "roseltorg.ru",
    "rts-tender.ru", "tektorg.ru", "sberbank-ast.ru", "etpgpb.ru", "lot-online.ru",
    "fabrikant.ru",
)
KNOWN_OFFICIAL_HOST_MARKERS = (
    *GOVERNMENT_OFFICIAL_HOST_MARKERS,
    *TENDER_OFFICIAL_HOST_MARKERS,
)

REUSABLE_EVIDENCE_STATUSES = frozenset({"confirmed", "probable", "historical", "conflicting"})

ROLE_LIST_FIELDS = {
    "official_sources": ("confirmed_facts", "official_domains", "checked_sources", "gaps"),
    "corporate_contour": ("decision_centers", "organizations", "edges", "gaps"),
    "secondary_sources": ("findings", "hypotheses_for_verification", "checked_sources"),
    "role_candidates": ("candidates", "unfilled_functions", "conflicts"),
    "candidate_contacts": ("contacts", "routing_paths", "candidates_without_contacts"),
}


class ContractError(ValueError):
    """Ответ роли не соответствует машинному контракту."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.I | re.S)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ContractError("результат субагента должен быть JSON-объектом")
    return value


def _safe(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*]+', "_", str(value or "").strip())
    return re.sub(r"\s+", " ", value).strip(" .")[:100] or "company"


def _stable_hash(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _prompt_hash() -> str:
    digest = hashlib.sha256(PROMPT_VERSION.encode("utf-8"))
    for role in ROLE_ORDER:
        for suffix in (".md", ".yaml"):
            path = AGENTS / f"{role}{suffix}"
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _input_hash(request: dict, prompt_hash: str) -> str:
    return _stable_hash({
        "schema_version": SCHEMA_VERSION,
        "prompt_hash": prompt_hash,
        "evidence_policy_version": EVIDENCE_POLICY_VERSION,
        "known": _trusted_known(request),
        "model": request.get("model") or os.environ.get("KIMI_MODEL_NAME") or "",
        "execution_flags": {
            "max_steps": os.environ.get("KIMI_RESEARCH_SUBAGENT_MAX_STEPS", ""),
            "thinking": os.environ.get("KIMI_WRITER_THINKING", ""),
        },
    })


def _role_input_hash(role: str, input_hash: str, completed: dict, request: dict) -> str:
    process_seed, roles_seed = _seed_parts(request)
    if role in {"official_sources", "candidate_contacts"}:
        context_input = {"known": _trusted_known(request)}
    else:
        known = dict(_known(request))
        known.pop("research_started_at", None)
        context_input = {
            "known": known,
            "process_seed": process_seed,
            "roles_seed": roles_seed,
        }
    return _stable_hash({
        "role": role,
        "input_hash": input_hash,
        "context_input": context_input,
        "dependencies": {name: completed[name] for name in ROLE_DEPENDENCIES[role]},
    })


def _log_path(request: dict) -> pathlib.Path | None:
    root = os.environ.get("KIMI_TOOL_LOG_DIR", "").strip()
    if not root:
        return None
    path = pathlib.Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{_safe(request.get('company') or request.get('inn'))}__research_subagents__{os.getpid()}.jsonl"


def _log(path: pathlib.Path | None, event: dict) -> None:
    if path is None:
        return
    row = {"timestamp": _now(), **event}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _clip_text(value, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = max(1, limit * 2 // 5)
    tail = max(1, limit - head)
    return text[:head] + "\n\n...[середина seed сокращена]...\n\n" + text[-tail:]


def _seed_parts(request: dict) -> tuple[str, str]:
    seed = request.get("seed") or {}
    if isinstance(seed, dict):
        return str(seed.get("process") or ""), str(seed.get("roles") or "")
    return "", str(seed)


def _known(request: dict) -> dict:
    lead = request.get("lead") or {}
    return {
        "company_name": request.get("company") or lead.get("name") or "",
        "inn": request.get("inn") or lead.get("_inn") or "",
        "ogrn": lead.get("_ogrn") or "",
        "website": lead.get("website") or "",
        "known_director": lead.get("contact_person") or "",
        "known_phone": lead.get("phone") or "",
        "known_email": lead.get("email") or "",
        "research_started_at": request.get("_observed_at") or _now(),
    }


def _trusted_known(request: dict) -> dict:
    """Идентификаторы, допустимые в independent official/contact контексте."""
    known = _known(request)
    return {key: known[key] for key in ("company_name", "inn", "ogrn", "website")}


def _context(request: dict, role: str, completed: dict, correction: str = "") -> str:
    """Собрать role-specific контекст, не раскрывая контактнику неподтверждённых людей."""
    process_seed, roles_seed = _seed_parts(request)
    dependencies = {name: completed[name] for name in ROLE_DEPENDENCIES[role]}
    known = _trusted_known(request) if role in {"official_sources", "candidate_contacts"} else _known(request)
    blocks = [
        "ИСХОДНЫЕ ДАННЫЕ:\n" + json.dumps(known, ensure_ascii=False, indent=2),
        "РЕЗУЛЬТАТЫ ЗАВИСИМОСТЕЙ:\n" + json.dumps(dependencies, ensure_ascii=False, indent=2),
    ]
    if role == "official_sources":
        blocks.append(
            "Важно: seed основного deep research намеренно не передан, потому что в нём смешаны "
            "официальные и вторичные источники. Начни независимый official-only поиск по реквизитам."
        )
    elif role == "candidate_contacts":
        candidates = (completed.get("role_candidates") or {}).get("candidates") or []
        support = {
            "allowed_candidates": candidates,
            "official_domains": (completed.get("official_sources") or {}).get("official_domains") or [],
            "corporate_organizations": (completed.get("corporate_contour") or {}).get("organizations") or [],
        }
        blocks.append(
            "РАЗРЕШЁННЫЕ АДРЕСАТЫ И ОРГАНИЗАЦИИ:\n"
            + json.dumps(support, ensure_ascii=False, indent=2)
        )
        blocks.append(
            "Не создавай новые ФИО. Непустой candidate_full_name обязан дословно соответствовать "
            "одному full_name из allowed_candidates. Для общего маршрута оставь candidate_full_name пустым."
        )
    else:
        limits = {
            "corporate_contour": (16000, 30000),
            "secondary_sources": (12000, 26000),
            "role_candidates": (10000, 45000),
        }
        process_limit, roles_limit = limits[role]
        blocks.append(
            "SEED ПРОЦЕССНОГО ПРОХОДА:\n" + _clip_text(process_seed, process_limit)
            + "\n\nSEED ПРОХОДА РОЛЕЙ/КОНТАКТОВ:\n" + _clip_text(roles_seed, roles_limit)
        )
    if correction:
        blocks.append(
            "ПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН ВАЛИДАТОРОМ:\n" + correction
            + "\nИсправь только перечисленные нарушения и верни контракт целиком."
        )
    blocks.append(
        "Верни только один валидный JSON-объект по контракту своей роли. Не добавляй markdown "
        "или пояснения вне JSON. Пустые списки допустимы; выдуманные факты — нет. observed_at "
        "ставит код — переданное моделью значение будет заменено временем запуска."
    )
    return "\n\n".join(blocks)


def _need_object(value, path: str) -> dict:
    if not isinstance(value, dict):
        raise ContractError(f"{path}: ожидается объект")
    return value


def _need_list(value, path: str) -> list:
    if not isinstance(value, list):
        raise ContractError(f"{path}: ожидается список")
    return value


def _need_string(row: dict, key: str, path: str, *, empty: bool = False) -> str:
    value = row.get(key)
    if not isinstance(value, str) or (not empty and not value.strip()):
        qualifier = "строка" if empty else "непустая строка"
        raise ContractError(f"{path}.{key}: ожидается {qualifier}")
    return value.strip()


def _list_of_strings(value, path: str, *, allowed: frozenset | None = None) -> list[str]:
    rows = _need_list(value, path)
    for index, item in enumerate(rows):
        if not isinstance(item, str) or not item.strip():
            raise ContractError(f"{path}[{index}]: ожидается непустая строка")
        if allowed is not None and item not in allowed:
            raise ContractError(f"{path}[{index}]: неизвестное значение {item!r}")
    return rows


def _enum(row: dict, key: str, allowed: frozenset, path: str) -> str:
    value = _need_string(row, key, path)
    if value not in allowed:
        raise ContractError(f"{path}.{key}: неизвестное значение {value!r}")
    return value


def _confidence(row: dict, path: str) -> float:
    value = row.get("confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ContractError(f"{path}.confidence: ожидается число от 0 до 1")
    return float(value)


def _url(value: str, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{path}: ожидается непустой URL")
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ContractError(f"{path}: разрешены только абсолютные http(s)-URL")
    if parsed.username is not None or parsed.password is not None:
        raise ContractError(f"{path}: credentials в URL запрещены")
    return value.strip()


def _urls(value, path: str, *, nonempty: bool = False) -> list[str]:
    rows = _need_list(value, path)
    if nonempty and not rows:
        raise ContractError(f"{path}: нужен хотя бы один URL")
    for index, item in enumerate(rows):
        _url(item, f"{path}[{index}]")
    return rows


def _publication_date(row: dict, path: str, observed_at: str) -> None:
    value = _need_string(row, "publication_date", path, empty=True)
    if not value:
        return
    try:
        published = date.fromisoformat(value)
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ContractError(f"{path}.publication_date: нужна дата YYYY-MM-DD или пустая строка") from exc
    if published > observed:
        raise ContractError(f"{path}.publication_date: дата позже observed_at")


def _observed_at(row: dict, path: str, *, stamp: str | None) -> str:
    if stamp is not None:
        row["observed_at"] = stamp
    value = _need_string(row, "observed_at", path)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{path}.observed_at: нужен ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{path}.observed_at: нужен timestamp с часовым поясом")
    return value


def _host(value: str) -> str:
    text = str(value or "").strip()
    if "://" not in text:
        text = "https://" + text
    return (urlsplit(text).hostname or "").lower().rstrip(".").removeprefix("www.")


def _host_matches(host: str, candidate: str) -> bool:
    candidate = candidate.lower().rstrip(".")
    return host == candidate or host.endswith("." + candidate)


def _host_has_marker(host: str, markers) -> bool:
    return bool(host) and any(_host_matches(host, marker) for marker in markers)


def _host_in(host: str, candidates) -> bool:
    return bool(host) and any(_host_matches(host, candidate) for candidate in candidates if candidate)


def _is_non_official_host(host: str) -> bool:
    return not host or any(marker in host for marker in NON_OFFICIAL_HOST_MARKERS)


def _linked_hosts(text: str) -> set[str]:
    """Извлечь только явно напечатанные URL/домены, а не угадывать связь по тексту."""
    values = re.findall(r"(?i)https?://[^\s\]\[()<>\"']+", text or "")
    values.extend(re.findall(r"(?i)(?:^|\s)(www\.[a-z0-9.-]+\.[a-z]{2,})(?=$|[\s/,:;])", text or ""))
    return {host for value in values if (host := _host(value.rstrip(".,;:")))}


def _email_domain(value: str) -> str:
    """Домен рабочей почты лида из Фазы 1 (kgmk@kolagmk.ru -> kolagmk.ru); '' если адреса нет."""
    first = str(value or "").split(",")[0].strip()
    return first.split("@", 1)[1].strip() if "@" in first else ""


def _trusted_official_hosts(result: dict, request: dict, trace: dict) -> set[str]:
    """Подтвердить заявленные домены цепочкой от известного официального источника.

    Модель не может сделать домен официальным одним полем ``official_domains``. Корнями
    доверия служат только уже известный сайт лида (после deny-list) и государственные/
    тендерные площадки из локальной политики. Новый домен должен быть явно связан URL-ссылкой
    со страницей одного из этих корней либо с уже подтверждённым доменом.
    """
    declared = {_host(value) for value in (result.get("official_domains") or [])}
    declared.discard("")
    lead = request.get("lead") or {}
    # Корни доверия из ДЕТЕРМИНИРОВАННЫХ данных Фазы 1: сайт лида И домен его рабочей почты
    # (kgmk@kolagmk.ru -> kolagmk.ru). Их подтвердил сбор, а не LLM, поэтому валидатор не должен
    # требовать от модели заново доказывать уже известный домен ссылкой с доверенной страницы.
    lead_roots = set()
    for raw in (lead.get("website"), _email_domain(lead.get("email"))):
        host = _host(raw or "")
        if host and not _is_non_official_host(host):
            lead_roots.add(host)
    trusted = set(lead_roots)
    trusted.update(host for host in declared if _host_has_marker(host, KNOWN_OFFICIAL_HOST_MARKERS))

    pages = trace.get("opened_pages") or []
    opened_hosts = {_host(page.get("url") or "") for page in pages}
    link_proofs: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for page in pages:
            page_host = _host(page.get("url") or "")
            if not (_host_in(page_host, trusted)
                    or _host_has_marker(page_host, KNOWN_OFFICIAL_HOST_MARKERS)):
                continue
            linked = _linked_hosts(page.get("text") or "")
            for candidate in declared - trusted:
                candidate_opened = any(
                    _host_matches(opened, candidate) or _host_matches(candidate, opened)
                    for opened in opened_hosts)
                if candidate_opened and any(
                        _host_matches(link, candidate) or _host_matches(candidate, link)
                        for link in linked):
                    trusted.add(candidate)
                    link_proofs[candidate] = page.get("url") or ""
                    changed = True
    proofs = []
    for candidate in sorted(declared & trusted):
        opened_url = next((
            page.get("url") or "" for page in pages
            if _host_in(_host(page.get("url") or ""), {candidate})
        ), "")
        if _host_has_marker(candidate, KNOWN_OFFICIAL_HOST_MARKERS):
            basis, linked_from = "known_official_policy", ""
        elif _host_in(candidate, lead_roots):
            basis, linked_from = "lead_registered_domain", ""
        else:
            basis, linked_from = "linked_from_trusted_page", link_proofs.get(candidate, "")
        proofs.append({
            "domain": candidate, "basis": basis,
            "linked_from": linked_from, "opened_url": opened_url,
        })
    trace["official_domain_proofs"] = proofs
    return trusted


def _validate_official_trace_policy(result: dict, request: dict, trace: dict,
                                    approved_hosts=None) -> set[str]:
    declared = {_host(value) for value in result.get("official_domains") or []}
    if approved_hosts is None:
        trusted = _trusted_official_hosts(result, request, trace)
        opened_hosts = {_host(page.get("url") or "") for page in trace.get("opened_pages") or []}
        unopened = sorted(
            host for host in declared
            if not _host_has_marker(host, KNOWN_OFFICIAL_HOST_MARKERS)
            and not any(_host_matches(opened, host) or _host_matches(host, opened)
                        for opened in opened_hosts))
        if unopened:
            raise ContractError(
                "official_sources.official_domains: домены не были успешно открыты: "
                + ", ".join(unopened))
    else:
        trusted = {_host(host) for host in approved_hosts if _host(host)}
        proof_domains = {
            _host(row.get("domain") or "") for row in trace.get("official_domain_proofs") or []
            if isinstance(row, dict)
        }
        missing_proofs = sorted(
            host for host in declared
            if not _host_has_marker(host, KNOWN_OFFICIAL_HOST_MARKERS)
            and not _host_in(host, proof_domains))
        if missing_proofs:
            raise ContractError(
                "official_sources.official_domains: в checkpoint отсутствует trust proof: "
                + ", ".join(missing_proofs))
    untrusted = sorted(host for host in declared if not _host_in(host, trusted))
    if untrusted:
        raise ContractError(
            "official_sources.official_domains: домены не подтверждены известным сайтом лида "
            "или ссылкой с доверенного официального источника: " + ", ".join(untrusted))

    for field in ("checked_sources", "confirmed_facts"):
        for index, row in enumerate(result.get(field) or []):
            host = _host(row.get("source_url") or "")
            source_type = row.get("source_type") or ""
            allowed = (_host_in(host, trusted)
                       or _host_has_marker(host, KNOWN_OFFICIAL_HOST_MARKERS))
            if not allowed:
                raise ContractError(
                    f"official_sources.{field}[{index}].source_url: домен не подтверждён как официальный")
            if source_type in {"government_registry", "government_disclosure", "regulation"}:
                if not _host_has_marker(host, GOVERNMENT_OFFICIAL_HOST_MARKERS):
                    raise ContractError(
                        f"official_sources.{field}[{index}].source_type: {source_type} требует "
                        "домен государственного/официального раскрытия")
            elif source_type == "official_tender":
                if not (_host_has_marker(host, TENDER_OFFICIAL_HOST_MARKERS)
                        or _host_in(host, trusted)):
                    raise ContractError(
                        f"official_sources.{field}[{index}].source_type: official_tender требует "
                        "официальную ЭТП либо подтверждённый домен компании")
    return trusted


def _validate_checked_sources(rows, path: str, allowed_types: frozenset, stamp: str | None) -> None:
    for index, raw in enumerate(_need_list(rows, path)):
        item_path = f"{path}[{index}]"
        row = _need_object(raw, item_path)
        _enum(row, "source_type", allowed_types, item_path)
        _url(row.get("source_url"), f"{item_path}.source_url")
        _enum(row, "outcome", CHECK_OUTCOMES, item_path)
        _observed_at(row, item_path, stamp=stamp)


def _validate_official_gaps(rows) -> set[str]:
    gap_ids: set[str] = set()
    for index, raw in enumerate(_need_list(rows, "official_sources.gaps")):
        path = f"official_sources.gaps[{index}]"
        row = _need_object(raw, path)
        gap_id = _need_string(row, "gap_id", path)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{2,63}", gap_id):
            raise ContractError(f"{path}.gap_id: нужен стабильный ASCII id длиной 3–64 символа")
        if gap_id in gap_ids:
            raise ContractError(f"{path}.gap_id: повтор {gap_id!r}")
        gap_ids.add(gap_id)
        _need_string(row, "description", path)
    return gap_ids


def _validate_official(result: dict, request: dict, stamp: str | None) -> None:
    domains = _list_of_strings(result["official_domains"], "official_sources.official_domains")
    for index, domain in enumerate(domains):
        host = _host(domain)
        if _is_non_official_host(host):
            raise ContractError(
                f"official_sources.official_domains[{index}]: домен является вторичным/агрегатором")
    _validate_official_gaps(result["gaps"])
    _validate_checked_sources(
        result["checked_sources"], "official_sources.checked_sources", OFFICIAL_SOURCE_TYPES, stamp)
    if not result["checked_sources"]:
        raise ContractError(
            "official_sources.checked_sources: нужен хотя бы один фактически вызванный официальный URL")
    for index, raw in enumerate(result["confirmed_facts"]):
        path = f"official_sources.confirmed_facts[{index}]"
        row = _need_object(raw, path)
        _need_string(row, "claim", path)
        _enum(row, "source_type", OFFICIAL_SOURCE_TYPES, path)
        source_url = _url(row.get("source_url"), f"{path}.source_url")
        observed = _observed_at(row, path, stamp=stamp)
        _publication_date(row, path, observed)
        _need_string(row, "evidence_text", path)
        _confidence(row, path)


def _validate_contour(result: dict, request: dict, stamp: str | None) -> None:
    target = _need_object(result.get("target_company"), "corporate_contour.target_company")
    target_name = _need_string(target, "name", "corporate_contour.target_company")
    target_inn = _need_string(target, "inn", "corporate_contour.target_company", empty=True)
    known = _known(request)
    if known["inn"] and target_inn != str(known["inn"]):
        raise ContractError("corporate_contour.target_company.inn: не совпадает с ИНН лида")
    if not target_name:
        raise ContractError("corporate_contour.target_company.name: пустое название")
    _list_of_strings(result["gaps"], "corporate_contour.gaps")
    nodes = {_person_key(target_name)}
    organization_keys: set[str] = set()
    organization_inns: set[str] = set()
    for index, raw in enumerate(result["organizations"]):
        path = f"corporate_contour.organizations[{index}]"
        row = _need_object(raw, path)
        name = _need_string(row, "name", path)
        row_inn = _need_string(row, "inn", path, empty=True)
        relation = _enum(row, "relation", CONTOUR_RELATIONS, path)
        _list_of_strings(row.get("functions"), f"{path}.functions")
        _urls(row.get("source_urls"), f"{path}.source_urls", nonempty=True)
        _enum(row, "status", STATUSES, path)
        same_target = (name.casefold() == target_name.casefold()
                       or bool(target_inn and row_inn and target_inn == row_inn))
        if relation != "production_branch" and same_target:
            raise ContractError(f"{path}: целевую компанию нельзя выдать за связанную организацию")
        name_key = _person_key(name)
        if name_key in nodes:
            raise ContractError(f"{path}.name: повтор узла {name!r}")
        if relation != "production_branch" and row_inn and row_inn in organization_inns:
            raise ContractError(f"{path}.inn: один ИНН повторён в нескольких узлах")
        nodes.add(name_key)
        organization_keys.add(name_key)
        if relation != "production_branch" and row_inn:
            organization_inns.add(row_inn)
    for index, raw in enumerate(result["decision_centers"]):
        path = f"corporate_contour.decision_centers[{index}]"
        row = _need_object(raw, path)
        _need_string(row, "function", path)
        organization = _need_string(row, "organization", path)
        if _person_key(organization) not in nodes:
            raise ContractError(f"{path}.organization: организация отсутствует среди узлов графа")
        _enum(row, "type", DECISION_CENTER_TYPES, path)
        _need_string(row, "rationale", path)
        _confidence(row, path)
        _enum(row, "status", STATUSES, path)
        _urls(row.get("source_urls"), f"{path}.source_urls", nonempty=True)
    adjacency: dict[str, set[str]] = {node: set() for node in nodes}
    edge_keys: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(result["edges"]):
        path = f"corporate_contour.edges[{index}]"
        row = _need_object(raw, path)
        from_name = _need_string(row, "from", path)
        to_name = _need_string(row, "to", path)
        from_key, to_key = _person_key(from_name), _person_key(to_name)
        if from_key not in nodes or to_key not in nodes:
            raise ContractError(f"{path}: from/to обязаны ссылаться на существующие узлы")
        if from_key == to_key:
            raise ContractError(f"{path}: петля узла на самого себя запрещена")
        relation = _enum(row, "relation", CONTOUR_RELATIONS, path)
        edge_key = (from_key, to_key, relation)
        if edge_key in edge_keys:
            raise ContractError(f"{path}: повтор ребра")
        edge_keys.add(edge_key)
        adjacency[from_key].add(to_key)
        adjacency[to_key].add(from_key)
        _url(row.get("source_url"), f"{path}.source_url")
        _confidence(row, path)
        _enum(row, "status", STATUSES, path)
    reachable = {_person_key(target_name)}
    frontier = list(reachable)
    while frontier:
        current = frontier.pop()
        for neighbor in adjacency.get(current, set()) - reachable:
            reachable.add(neighbor)
            frontier.append(neighbor)
    disconnected = organization_keys - reachable
    if disconnected:
        raise ContractError(
            "corporate_contour.organizations: каждый узел должен участвовать хотя бы в одном edge")
    if not result["decision_centers"] and not result["organizations"] and not result["edges"] and not result["gaps"]:
        raise ContractError("corporate_contour: пустой граф без gaps")


def _validate_secondary(result: dict, completed: dict, stamp: str | None) -> None:
    gap_ids = {
        row.get("gap_id") for row in (completed.get("official_sources") or {}).get("gaps") or []
        if isinstance(row, dict) and row.get("gap_id")
    }
    _validate_checked_sources(
        result["checked_sources"], "secondary_sources.checked_sources", SECONDARY_SOURCE_TYPES, stamp)
    for index, raw in enumerate(result["findings"]):
        path = f"secondary_sources.findings[{index}]"
        row = _need_object(raw, path)
        _need_string(row, "claim", path)
        _enum(row, "status", STATUSES, path)
        _enum(row, "source_type", SECONDARY_SOURCE_TYPES, path)
        _url(row.get("source_url"), f"{path}.source_url")
        observed = _observed_at(row, path, stamp=stamp)
        _publication_date(row, path, observed)
        _need_string(row, "evidence_text", path)
        gap_id = _need_string(row, "official_gap_id", path)
        if gap_id not in gap_ids:
            raise ContractError(f"{path}.official_gap_id: id отсутствует в official_sources.gaps")
        _confidence(row, path)
    for index, raw in enumerate(result["hypotheses_for_verification"]):
        path = f"secondary_sources.hypotheses_for_verification[{index}]"
        row = _need_object(raw, path)
        _need_string(row, "hypothesis", path)
        _need_string(row, "verification_needed", path)
        gap_id = _need_string(row, "official_gap_id", path)
        if gap_id not in gap_ids:
            raise ContractError(f"{path}.official_gap_id: id отсутствует в official_sources.gaps")
        _urls(row.get("source_urls"), f"{path}.source_urls", nonempty=True)
    if not result["findings"] and not result["hypotheses_for_verification"] and not result["checked_sources"]:
        raise ContractError("secondary_sources: пустой результат без следов проверки")


def _validate_roles(result: dict, completed: dict, stamp: str | None) -> None:
    contour = completed.get("corporate_contour") or {}
    target = contour.get("target_company") or {}
    allowed_organizations: dict[str, set[str]] = {}
    for row in [target, *(contour.get("organizations") or [])]:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        allowed_organizations.setdefault(_person_key(row.get("name")), set()).add(
            str(row.get("inn") or ""))
    unfilled_rows = _list_of_strings(
        result["unfilled_functions"], "role_candidates.unfilled_functions", allowed=TARGET_FUNCTIONS)
    unfilled = set(unfilled_rows)
    if len(unfilled) != len(unfilled_rows):
        raise ContractError("role_candidates.unfilled_functions: повтор функции")
    _list_of_strings(result["conflicts"], "role_candidates.conflicts")
    found_functions: set[str] = set()
    identities: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(result["candidates"]):
        path = f"role_candidates.candidates[{index}]"
        row = _need_object(raw, path)
        _need_string(row, "full_name", path)
        target_function = _enum(row, "target_function", TARGET_FUNCTIONS, path)
        _need_string(row, "reported_title", path)
        organization = _need_string(row, "organization", path)
        organization_key = _person_key(organization)
        if organization_key not in allowed_organizations:
            raise ContractError(f"{path}.organization: организация отсутствует в corporate_contour")
        candidate_inn = _need_string(row, "inn", path, empty=True)
        known_inns = allowed_organizations[organization_key] - {""}
        if known_inns and candidate_inn not in known_inns:
            raise ContractError(f"{path}.inn: ИНН не совпадает с узлом corporate_contour")
        _enum(row, "status", STATUSES, path)
        if row.get("is_current_role_confirmed") is not False:
            raise ContractError(
                f"{path}.is_current_role_confirmed: агент ролей создаёт кандидата, а не присваивает текущую должность")
        _urls(row.get("source_urls"), f"{path}.source_urls", nonempty=True)
        observed = _observed_at(row, path, stamp=stamp)
        observed_date = datetime.fromisoformat(observed.replace("Z", "+00:00")).date()
        dates = _need_list(row.get("publication_dates"), f"{path}.publication_dates")
        if len(dates) != len(row.get("source_urls") or []):
            raise ContractError(
                f"{path}.publication_dates: нужен один элемент на каждый source_urls в том же порядке")
        for date_index, value in enumerate(dates):
            if not isinstance(value, str):
                raise ContractError(f"{path}.publication_dates[{date_index}]: ожидается строка")
            if value:
                try:
                    published = date.fromisoformat(value)
                except ValueError as exc:
                    raise ContractError(
                        f"{path}.publication_dates[{date_index}]: нужна дата YYYY-MM-DD или пустая строка") from exc
                if published > observed_date:
                    raise ContractError(f"{path}.publication_dates[{date_index}]: дата позже observed_at")
        _need_string(row, "evidence", path)
        _confidence(row, path)
        found_functions.add(target_function)
        identity = (
            _person_key(row.get("full_name")), target_function,
            _person_key(row.get("organization")),
        )
        if identity in identities:
            raise ContractError(f"{path}: повтор одного кандидата/функции/организации")
        identities.add(identity)
    overlap = found_functions & unfilled
    if overlap:
        raise ContractError(
            "role_candidates.unfilled_functions: функции одновременно найдены и объявлены "
            "незаполненными: " + ", ".join(sorted(overlap)))
    missing = TARGET_FUNCTIONS - found_functions - unfilled
    if missing:
        raise ContractError(
            "role_candidates: каждая целевая функция должна иметь кандидата либо попасть в "
            "unfilled_functions; отсутствуют: " + ", ".join(sorted(missing)))


def _person_key(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", " ", str(value or "").casefold().replace("ё", "е")).strip()


def _validate_contact_source(row: dict, path: str, completed: dict, kind: str) -> tuple[str, float]:
    source_context = _enum(row, "source_context", CONTACT_SOURCE_CONTEXTS, path)
    host = _host(row.get("source_url") or "")
    official_domains = {
        _host(value) for value in (completed.get("official_sources") or {}).get("official_domains") or []
    }
    forced_context = None
    if _host_has_marker(host, BUSINESS_AGGREGATOR_HOST_MARKERS):
        forced_context = "business_aggregator"
    elif _host_has_marker(host, VACANCY_HOST_MARKERS):
        forced_context = "vacancy"
    elif _host_has_marker(host, PROFESSIONAL_PROFILE_HOST_MARKERS):
        forced_context = "professional_profile"
    elif _host_has_marker(host, SOCIAL_MEDIA_HOST_MARKERS):
        forced_context = "social_media"
    if forced_context and source_context != forced_context:
        raise ContractError(
            f"{path}.source_context: домен требует классификацию {forced_context!r}, "
            f"а не {source_context!r}")
    if source_context in {"official_company_site", "official_holding_site"}:
        if not _host_in(host, official_domains):
            raise ContractError(
                f"{path}.source_context: домен отсутствует в подтверждённых official_domains")
    elif source_context == "government_registry":
        if not _host_has_marker(host, GOVERNMENT_OFFICIAL_HOST_MARKERS):
            raise ContractError(f"{path}.source_context: government_registry требует официальный домен")
    elif source_context == "official_tender":
        if not (_host_has_marker(host, TENDER_OFFICIAL_HOST_MARKERS)
                or _host_in(host, official_domains)):
            raise ContractError(f"{path}.source_context: official_tender требует ЭТП/официальный домен")
    status = _enum(row, "status", STATUSES, path)
    confidence = _confidence(row, path)
    if source_context == "business_aggregator" and (
            status not in ("unverified", "conflicting") or confidence > 0.5):
        raise ContractError(
            f"{path}: агрегаторный контакт должен быть unverified/conflicting с confidence <= 0.5")
    secondary_caps = {
        "vacancy": 0.7, "business_media": 0.8, "professional_profile": 0.8,
        "social_media": 0.6, "other_public_source": 0.7,
    }
    if source_context in secondary_caps and (
            status == "confirmed" or confidence > secondary_caps[source_context]):
        raise ContractError(
            f"{path}: вторичный контекст {source_context} не может быть confirmed и имеет "
            f"confidence cap {secondary_caps[source_context]:.1f}")
    outreach_policy = _enum(row, "outreach_policy", OUTREACH_POLICIES, path)
    if source_context == "vacancy" and outreach_policy != "do_not_cold_outreach":
        raise ContractError(f"{path}.outreach_policy: контакт из вакансии нельзя использовать для cold outreach")
    if source_context == "business_aggregator" and outreach_policy not in {
            "internal_verification_only", "do_not_cold_outreach"}:
        raise ContractError(f"{path}.outreach_policy: агрегатор допустим только для внутренней проверки")
    if source_context == "official_tender" and outreach_policy == "direct_allowed":
        raise ContractError(f"{path}.outreach_policy: тендерный контакт требует routing_only")
    if outreach_policy == "direct_allowed" and (
            kind != "personal_work"
            or source_context not in {"official_company_site", "official_holding_site",
                                      "government_registry"}):
        raise ContractError(
            f"{path}.outreach_policy: direct_allowed разрешён только персональному рабочему "
            "контакту из официального источника")
    return status, confidence


def _validate_contact_value(value: str, kind: str, path: str) -> None:
    value = value.strip()
    if value.casefold() in {"n/a", "none", "нет", "не найден", "unknown", "-"}:
        raise ContractError(f"{path}: заглушка не является контактом")
    email_match = re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@([A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,})",
        value,
    )
    digits = re.sub(r"\D", "", value)
    if kind in {"functional_inbox", "corporate_inbox"} and not email_match:
        raise ContractError(f"{path}: для {kind} нужен email")
    if kind == "reception_phone" and not 7 <= len(digits) <= 15:
        raise ContractError(f"{path}: для reception_phone нужен рабочий телефон")
    if kind == "personal_work" and not email_match and not 7 <= len(digits) <= 15:
        raise ContractError(f"{path}: personal_work должен быть рабочим email или телефоном")
    if kind == "official_professional_profile":
        _url(value, path)
    if email_match and _host(email_match.group(1)) in PUBLIC_EMAIL_DOMAINS:
        raise ContractError(f"{path}: публичный/личный email запрещён; нужен корпоративный рабочий")


def _validate_contacts(result: dict, completed: dict, stamp: str | None) -> None:
    candidates = (completed.get("role_candidates") or {}).get("candidates") or []
    allowed_by_person: dict[str, set[tuple[str, str]]] = {}
    required: set[tuple[str, str, str]] = set()
    for candidate in candidates:
        person_key = _person_key(candidate.get("full_name"))
        function = candidate.get("target_function") or ""
        organization_key = _person_key(candidate.get("organization"))
        allowed_by_person.setdefault(person_key, set()).add((function, organization_key))
        required.add((person_key, function, organization_key))
    covered: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(result["contacts"]):
        path = f"candidate_contacts.contacts[{index}]"
        row = _need_object(raw, path)
        person = _need_string(row, "candidate_full_name", path)
        key = _person_key(person)
        if key not in allowed_by_person:
            raise ContractError(f"{path}.candidate_full_name: ФИО отсутствует в role_candidates")
        candidate_function = _need_string(row, "candidate_function", path)
        if candidate_function not in TARGET_FUNCTIONS:
            raise ContractError(f"{path}.candidate_function: неизвестная функция {candidate_function!r}")
        organization = _need_string(row, "organization", path)
        identity = (key, candidate_function, _person_key(organization))
        if (candidate_function, identity[2]) not in allowed_by_person[key]:
            raise ContractError(
                f"{path}: функция/организация не совпадает с точной записью role_candidates")
        covered.add(identity)
        value = _need_string(row, "value", path)
        kind = _enum(row, "contact_kind", CONTACT_KINDS, path)
        _validate_contact_value(value, kind, f"{path}.value")
        _need_string(row, "best_use", path)
        _url(row.get("source_url"), f"{path}.source_url")
        observed = _observed_at(row, path, stamp=stamp)
        _publication_date(row, path, observed)
        _validate_contact_source(row, path, completed, kind)
    for index, raw in enumerate(result["routing_paths"]):
        path = f"candidate_contacts.routing_paths[{index}]"
        row = _need_object(raw, path)
        _need_string(row, "purpose", path)
        route = _need_string(row, "route", path)
        kind = _enum(row, "contact_kind", ROUTING_CONTACT_KINDS, path)
        _validate_contact_value(route, kind, f"{path}.route")
        _need_string(row, "best_use", path)
        _need_string(row, "organization", path)
        _url(row.get("source_url"), f"{path}.source_url")
        observed = _observed_at(row, path, stamp=stamp)
        _publication_date(row, path, observed)
        _validate_contact_source(row, path, completed, kind)
    missing_identities: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(_need_list(
            result["candidates_without_contacts"],
            "candidate_contacts.candidates_without_contacts")):
        path = f"candidate_contacts.candidates_without_contacts[{index}]"
        row = _need_object(raw, path)
        person = _need_string(row, "candidate_full_name", path)
        function = _enum(row, "candidate_function", TARGET_FUNCTIONS, path)
        organization = _need_string(row, "organization", path)
        _need_string(row, "reason", path)
        identity = (_person_key(person), function, _person_key(organization))
        if identity not in required:
            raise ContractError(f"{path}: кандидат отсутствует в role_candidates")
        if identity in missing_identities:
            raise ContractError(f"{path}: повтор кандидата без контакта")
        missing_identities.add(identity)
    overlap = covered & missing_identities
    if overlap:
        raise ContractError("candidate_contacts: кандидат одновременно имеет контакт и объявлен без контакта")
    uncovered = required - covered - missing_identities
    if uncovered:
        raise ContractError(
            "candidate_contacts: каждый кандидат должен иметь контакт либо точную запись в "
            f"candidates_without_contacts; не покрыто записей: {len(uncovered)}")


def _validate_role_result(role: str, result: dict, request: dict, completed: dict,
                          *, stamp: str | None = None) -> dict:
    result = _need_object(result, role)
    for field in ROLE_LIST_FIELDS[role]:
        if field not in result:
            raise ContractError(f"{role}.{field}: обязательное поле отсутствует")
        _need_list(result[field], f"{role}.{field}")
    if role == "official_sources":
        _validate_official(result, request, stamp)
    elif role == "corporate_contour":
        _validate_contour(result, request, stamp)
    elif role == "secondary_sources":
        _validate_secondary(result, completed, stamp)
    elif role == "role_candidates":
        _validate_roles(result, completed, stamp)
    elif role == "candidate_contacts":
        _validate_contacts(result, completed, stamp)
    else:  # pragma: no cover — ROLE_ORDER задаётся рядом
        raise ContractError(f"неизвестная роль {role}")
    return result


def _iter_source_urls(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "source_url" and isinstance(item, str):
                yield item
            elif key == "source_urls" and isinstance(item, list):
                yield from (url for url in item if isinstance(url, str))
            else:
                yield from _iter_source_urls(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_source_urls(item)


def _normalize_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return ""
    host = (parsed.hostname or "").lower().rstrip(".")
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return f"{parsed.scheme.lower()}://{host}{port}{path}" + (
        f"?{parsed.query}" if parsed.query else "")


def _iter_reusable_evidence_urls(completed: dict):
    """URL, которые downstream вправе наследовать как уже проверанные доказательства."""
    field_map = {
        "official_sources": ("confirmed_facts",),
        "corporate_contour": ("decision_centers", "organizations", "edges"),
        "secondary_sources": ("findings",),
        "role_candidates": ("candidates",),
    }
    for role, fields in field_map.items():
        payload = completed.get(role) or {}
        for field in fields:
            for row in payload.get(field) or []:
                if not isinstance(row, dict):
                    continue
                status = row.get("status")
                if status is not None and status not in REUSABLE_EVIDENCE_STATUSES:
                    continue
                yield from _iter_source_urls(row)


def _trace_allows(url: str, trace: dict, completed: dict) -> bool:
    normalized = _normalize_url(url)
    upstream = {_normalize_url(item) for item in _iter_reusable_evidence_urls(completed)}
    opened = {_normalize_url(item) for item in trace["opened_urls"]}
    return bool(normalized and (normalized in upstream or normalized in opened))


def _validate_evidence_trace(role: str, result: dict, trace: dict, completed: dict,
                             request: dict) -> None:
    """Сниппет поиска не считается доказательством: URL должен быть открыт или унаследован."""
    factual_fields = {
        "official_sources": ("confirmed_facts",),
        "corporate_contour": ("decision_centers", "organizations", "edges"),
        "secondary_sources": ("findings",),
        "role_candidates": ("candidates",),
        "candidate_contacts": ("contacts", "routing_paths"),
    }[role]
    reusable_upstream = {} if role == "candidate_contacts" else completed
    for field in factual_fields:
        for index, row in enumerate(result.get(field) or []):
            urls = list(_iter_source_urls(row))
            for url in urls:
                if not _trace_allows(url, trace, reusable_upstream):
                    raise ContractError(
                        f"{role}.{field}[{index}]: URL не был открыт LeadFetch/LeadCrawl и отсутствует в upstream")
    attempted_urls = {_normalize_url(item) for item in trace["attempted_urls"]}
    attempted_crawls = {_normalize_url(item) for item in trace.get("attempted_crawls") or []}
    if role == "secondary_sources":
        opened_urls = {_normalize_url(item) for item in trace["opened_urls"]}
        for index, row in enumerate(result.get("hypotheses_for_verification") or []):
            for url in _iter_source_urls(row):
                normalized = _normalize_url(url)
                if (normalized not in attempted_urls and normalized not in attempted_crawls
                        and normalized not in opened_urls):
                    raise ContractError(
                        f"secondary_sources.hypotheses_for_verification[{index}]: URL гипотезы "
                        "не был хотя бы вызван через LeadFetch")
    if role in ("official_sources", "secondary_sources"):
        for index, row in enumerate(result.get("checked_sources") or []):
            url = row.get("source_url") or ""
            normalized = _normalize_url(url)
            attempted = normalized in attempted_urls or normalized in attempted_crawls
            if not attempted:
                raise ContractError(
                    f"{role}.checked_sources[{index}]: URL не вызывался через LeadFetch/LeadCrawl")
            if row.get("outcome") != "unavailable" and not _trace_allows(url, trace, {}):
                raise ContractError(
                    f"{role}.checked_sources[{index}]: outcome требует успешного LeadFetch/LeadCrawl")
    if role == "official_sources":
        approved = trace.get("approved_official_domains")
        trusted = _validate_official_trace_policy(
            result, request, trace, approved_hosts=approved if approved is not None else None)
        trace["approved_official_domains"] = set(trusted)


def _tool_arguments(call) -> dict:
    raw = getattr(getattr(call, "function", None), "arguments", None) or "{}"
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _tool_success(text: str) -> bool:
    lowered = (text or "").casefold()
    failures = (
        "страница не прочитана", "не собрал ни одной страницы", "research tool failed",
        "не удалось запустить", "таймаут", "traceback", "toolerror",
    )
    return bool(text.strip()) and not any(marker in lowered for marker in failures)


def _fetch_result_page(text: str, requested_url: str) -> dict:
    match = re.search(r"(?im)^URL:\s*(https?://\S+)", text or "")
    returned_url = match.group(1).rstrip(".,;:") if match else requested_url
    return {"url": returned_url, "text": text or ""}


def _crawl_result_pages(text: str) -> list[dict]:
    pattern = re.compile(
        r"(?ims)^---\s+(https?://\S+)\s*\r?\n(.*?)(?=^---\s+https?://|\Z)")
    return [
        {"url": match.group(1).rstrip(".,;:"), "text": match.group(2)}
        for match in pattern.finditer(text or "")
    ]


def _trace_for_checkpoint(trace: dict) -> dict:
    payload = {key: sorted(value) for key, value in trace.items() if isinstance(value, set)}
    payload["opened_pages"] = [
        {"url": page.get("url") or "", "characters": len(page.get("text") or "")}
        for page in trace.get("opened_pages") or []
    ]
    payload["redirects"] = list(trace.get("redirects") or [])
    payload["official_domain_proofs"] = list(trace.get("official_domain_proofs") or [])
    return payload


async def _run_role(prompt, request: dict, role: str, completed: dict,
                    log_path: pathlib.Path | None) -> tuple[dict, dict]:
    model = request.get("model") or os.environ.get("KIMI_MODEL_NAME")
    max_steps_raw = os.environ.get("KIMI_RESEARCH_SUBAGENT_MAX_STEPS", "").strip()
    attempts_raw = os.environ.get("KIMI_RESEARCH_SUBAGENT_ATTEMPTS", "2").strip()
    try:
        max_steps_value = max(0, int(max_steps_raw or "0"))
        attempts = max(1, int(attempts_raw or "2"))
    except ValueError as exc:
        raise RuntimeError("KIMI_RESEARCH_SUBAGENT_MAX_STEPS/ATTEMPTS должны быть целыми") from exc

    correction = ""
    last_error: Exception | None = None
    trace = {"opened_urls": set(), "attempted_urls": set(),
             "attempted_hosts": set(), "attempted_crawls": set(),
             "opened_pages": [], "redirects": [], "official_domain_proofs": []}
    for attempt in range(1, attempts + 1):
        final = ""
        pending_tools: dict[str, tuple[str, str]] = {}
        _log(log_path, {"event": "agent_start", "role": role, "attempt": attempt})
        try:
            async for message in prompt(
                _context(request, role, completed, correction),
                model=model,
                thinking=os.environ.get("KIMI_WRITER_THINKING", "").lower() in ("1", "true", "yes"),
                yolo=True,
                final_message_only=False,
                agent_file=AGENTS / f"{role}.yaml",
                max_steps_per_turn=max_steps_value if max_steps_value > 0 else None,
            ):
                for call in message.tool_calls or []:
                    name = call.function.name
                    args = _tool_arguments(call)
                    call_id = str(getattr(call, "id", "") or "")
                    if name == "LeadFetch" and args.get("url"):
                        target = str(args["url"])
                        pending_tools[call_id] = (name, target)
                        trace["attempted_urls"].add(target)
                    elif name == "LeadCrawl" and args.get("domain"):
                        target = str(args["domain"])
                        pending_tools[call_id] = (name, target)
                        host = _host(target)
                        if host:
                            trace["attempted_hosts"].add(host)
                        trace["attempted_crawls"].add(
                            target if "://" in target else "https://" + target)
                    _log(log_path, {
                        "event": "tool_call", "role": role, "attempt": attempt, "tool": name,
                        "call_id": call_id or None, "arguments": call.function.arguments or "{}",
                    })
                text = message.extract_text().strip()
                if message.role == "tool":
                    call_id = str(getattr(message, "tool_call_id", "") or "")
                    tool = pending_tools.get(call_id)
                    if tool and _tool_success(text):
                        name, target = tool
                        if name == "LeadFetch":
                            page = _fetch_result_page(text, target)
                            trace["opened_urls"].add(page["url"])
                            trace["attempted_urls"].add(page["url"])
                            trace["opened_pages"].append(page)
                            if _normalize_url(target) != _normalize_url(page["url"]):
                                trace["redirects"].append({
                                    "requested": target, "returned": page["url"]})
                        else:
                            for page in _crawl_result_pages(text):
                                trace["opened_urls"].add(page["url"])
                                trace["opened_pages"].append(page)
                    _log(log_path, {
                        "event": "tool_result", "role": role, "attempt": attempt,
                        "call_id": call_id or None, "result": text,
                    })
                if message.role == "assistant" and text and not message.tool_calls:
                    final = text
            if not final:
                raise RuntimeError(f"субагент {role} не вернул финальный ответ")
            result = _extract_json(final)
            _validate_role_result(
                role, result, request, completed, stamp=request.get("_observed_at") or _now())
            _validate_evidence_trace(role, result, trace, completed, request)
            serializable_trace = _trace_for_checkpoint(trace)
            _log(log_path, {
                "event": "agent_complete", "role": role, "attempt": attempt,
                "evidence_trace": serializable_trace, "result": result,
            })
            print(f"[research-subagent] готово: {role}", flush=True)
            return result, serializable_trace
        except Exception as exc:
            last_error = exc
            correction_parts = ["Ошибка валидатора: " + str(exc)[:1800]]
            if final:
                correction_parts.append("Отклонённый JSON предыдущей попытки:\n" + _clip_text(final, 7000))
            if trace["opened_urls"]:
                correction_parts.append(
                    "URL, уже успешно открытые в этой роли и разрешённые для повторного JSON:\n"
                    + "\n".join(sorted(trace["opened_urls"])))
            correction = "\n\n".join(correction_parts)
            _log(log_path, {
                "event": "agent_invalid", "role": role, "attempt": attempt, "error": correction,
            })
            if attempt < attempts:
                print(
                    f"[research-subagent] повтор {role} ({attempt + 1}/{attempts}): {correction[:180]}",
                    flush=True,
                )
    assert last_error is not None
    raise last_error


def _latest_observed_at(completed: dict, fallback: str) -> str:
    values: list[datetime] = []

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "observed_at" and isinstance(item, str):
                    try:
                        parsed = datetime.fromisoformat(item.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                        values.append(parsed)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(completed)
    return max(values).isoformat() if values else fallback


def _snapshot(request: dict, completed: dict, role_hashes: dict, role_traces: dict, *,
              input_hash: str, prompt_hash: str, complete: bool,
              checkpoint_created_at: str, failed_roles: dict | None = None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "prompt_hash": prompt_hash,
        "input_hash": input_hash,
        "company": request.get("company") or "",
        "inn": request.get("inn") or "",
        "observed_at": _latest_observed_at(
            completed, request.get("_observed_at") or checkpoint_created_at),
        "checkpoint_created_at": checkpoint_created_at,
        "execution": "dependency_graph",
        "execution_waves": [list(wave) for wave in EXECUTION_WAVES],
        "complete": complete,
        "role_input_hashes": role_hashes,
        "role_output_hashes": {
            role: _stable_hash({"result": completed[role], "evidence_trace": role_traces[role]})
            for role in ROLE_ORDER if role in completed and role in role_traces},
        "evidence_traces": {
            role: role_traces[role] for role in ROLE_ORDER if role in role_traces},
        "roles": {role: completed[role] for role in ROLE_ORDER if role in completed},
        "roles_failed": dict(failed_roles or {}),
    }


def _write_checkpoint(path: pathlib.Path | None, payload: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _checkpoint_fresh(previous: dict, ttl_h: float) -> bool:
    if ttl_h <= 0:
        return False
    try:
        created = datetime.fromisoformat(
            str(previous.get("checkpoint_created_at") or "").replace("Z", "+00:00"))
        if created.tzinfo is None or created.utcoffset() is None:
            return False
        age_seconds = (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return False
    return 0 <= age_seconds < ttl_h * 3600


def _load_checkpoint(path: pathlib.Path | None, request: dict, *, input_hash: str,
                     prompt_hash: str, ttl_h: float) -> tuple[dict, dict, dict, str]:
    fresh_created_at = request.get("_observed_at") or _now()
    if path is None or not path.is_file():
        return {}, {}, {}, fresh_created_at
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}, {}, {}, fresh_created_at
    if (
        not isinstance(previous, dict)
        or not _checkpoint_fresh(previous, ttl_h)
        or previous.get("schema_version") != SCHEMA_VERSION
        or previous.get("prompt_hash") != prompt_hash
        or previous.get("input_hash") != input_hash
        or str(previous.get("inn") or "") != str(request.get("inn") or "")
    ):
        return {}, {}, {}, fresh_created_at
    checkpoint_created_at = str(previous.get("checkpoint_created_at") or fresh_created_at)
    previous_roles = previous.get("roles") or {}
    previous_hashes = previous.get("role_input_hashes") or {}
    previous_output_hashes = previous.get("role_output_hashes") or {}
    previous_traces = previous.get("evidence_traces") or {}
    completed: dict[str, dict] = {}
    role_hashes: dict[str, str] = {}
    role_traces: dict[str, dict] = {}
    for role in ROLE_ORDER:
        if not all(dependency in completed for dependency in ROLE_DEPENDENCIES[role]):
            continue
        expected_hash = _role_input_hash(role, input_hash, completed, request)
        candidate = previous_roles.get(role)
        candidate_trace = previous_traces.get(role)
        if (not isinstance(candidate, dict)
                or not isinstance(candidate_trace, dict)
                or previous_hashes.get(role) != expected_hash
                or previous_output_hashes.get(role) != _stable_hash({
                    "result": candidate, "evidence_trace": candidate_trace})):
            continue
        try:
            _validate_role_result(role, candidate, request, completed, stamp=None)
            restored_trace = {
                "opened_urls": set(candidate_trace.get("opened_urls") or []),
                "attempted_urls": set(candidate_trace.get("attempted_urls") or []),
                "attempted_hosts": set(candidate_trace.get("attempted_hosts") or []),
                "attempted_crawls": set(candidate_trace.get("attempted_crawls") or []),
                "approved_official_domains": set(
                    candidate_trace.get("approved_official_domains") or []),
                "opened_pages": list(candidate_trace.get("opened_pages") or []),
                "redirects": list(candidate_trace.get("redirects") or []),
                "official_domain_proofs": list(
                    candidate_trace.get("official_domain_proofs") or []),
            }
            _validate_evidence_trace(role, candidate, restored_trace, completed, request)
        except (ContractError, TypeError, ValueError):
            continue
        completed[role] = candidate
        role_hashes[role] = expected_hash
        role_traces[role] = candidate_trace
    return completed, role_hashes, role_traces, checkpoint_created_at


async def run(request: dict, *, prompt_fn=None) -> dict:
    if prompt_fn is None:
        # Runtime выбирает родитель (writer_kimi.py) через ORQ_LLM_RUNTIME: claude —
        # Kimi-совместимый адаптер поверх Claude Agent SDK в ОСНОВНОМ окружении,
        # иначе — прежний kimi_agent_sdk из .venv_kimi. Граф/валидация/чекпойнт общие.
        if (os.environ.get("ORQ_LLM_RUNTIME") or "").strip().lower() == "claude":
            try:
                from claude_kimi_adapter import prompt as prompt_fn
            except ImportError as exc:
                raise RuntimeError(
                    "claude-runtime: research_enrichment_agent надо запускать "
                    "python'ом основного окружения (claude-agent-sdk)") from exc
        else:
            try:
                from kimi_agent_sdk import prompt as prompt_fn
            except ImportError as exc:
                raise RuntimeError("research_enrichment_agent надо запускать из .venv_kimi") from exc

    request = dict(request)
    request["_observed_at"] = _now()
    prompt_hash = _prompt_hash()
    input_hash = _input_hash(request, prompt_hash)
    try:
        ttl_h = float(request.get("checkpoint_ttl_h", os.environ.get("KIMI_RESEARCH_CHECKPOINT_TTL_H", "72")))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("checkpoint_ttl_h должен быть числом") from exc
    checkpoint = pathlib.Path(request["checkpoint"]) if request.get("checkpoint") else None
    completed, role_hashes, role_traces, checkpoint_created_at = _load_checkpoint(
        checkpoint, request, input_hash=input_hash, prompt_hash=prompt_hash, ttl_h=ttl_h)
    log_path = _log_path(request)
    for role in ROLE_ORDER:
        if role in completed:
            print(f"[research-subagent] checkpoint: {role}", flush=True)

    changed = False
    # Мягкая деградация (2026-07-22): падение роли НЕ роняет всю компанию. Упавшая роль и роли,
    # чьи зависимости не выполнены, помечаются в failed_roles; наружу уходит частичное досье
    # (complete=False), а писатель добирает недостающее из находок движка. Раньше любая ошибка
    # роли бросала RuntimeError → exit 1 → writer вообще не запускался (orchestrator._research_one_kimi).
    failed_roles: dict[str, str] = {}
    for wave in EXECUTION_WAVES:
        pending = []
        for role in wave:
            if role in completed:
                continue
            missing = [dep for dep in ROLE_DEPENDENCIES[role] if dep not in completed]
            if missing:
                failed_roles.setdefault(
                    role, "пропущена: не выполнены зависимости " + ", ".join(missing))
                continue
            pending.append(role)
        if not pending:
            continue
        baseline = dict(completed)

        async def execute(role: str) -> tuple[str, dict | None, dict | None, Exception | None]:
            try:
                result, evidence_trace = await _run_role(
                    prompt_fn, request, role, baseline, log_path)
                return role, result, evidence_trace, None
            except Exception as exc:  # остальные роли этой волны должны закончить и сохраниться
                _log(log_path, {"event": "agent_error", "role": role, "error": str(exc)})
                return role, None, None, exc

        tasks = [asyncio.create_task(execute(role), name=f"research-{role}") for role in pending]
        for finished in asyncio.as_completed(tasks):
            role, result, evidence_trace, error = await finished
            if error is not None:
                failed_roles[role] = str(error)[:300]
                print(f"[research-subagent] ошибка: {role}: {str(error)[:200]}", flush=True)
                continue
            assert result is not None
            assert evidence_trace is not None
            changed = True
            completed[role] = result
            role_traces[role] = evidence_trace
            role_hashes[role] = _role_input_hash(role, input_hash, baseline, request)
            _write_checkpoint(
                checkpoint,
                _snapshot(
                    request, completed, role_hashes, role_traces, input_hash=input_hash,
                    prompt_hash=prompt_hash, complete=len(completed) == len(ROLE_ORDER),
                    checkpoint_created_at=checkpoint_created_at, failed_roles=failed_roles,
                ),
            )

    result = _snapshot(
        request, completed, role_hashes, role_traces, input_hash=input_hash,
        prompt_hash=prompt_hash, complete=len(completed) == len(ROLE_ORDER),
        checkpoint_created_at=checkpoint_created_at, failed_roles=failed_roles,
    )
    if changed or failed_roles:
        _write_checkpoint(checkpoint, result)
    if failed_roles:
        print("[research-subagent] частичное досье: не выполнены "
              + ", ".join(sorted(failed_roles)), flush=True)
    return result


def selftest() -> int:
    from kimi_cli.agentspec import load_agent_spec

    for role in ROLE_ORDER:
        spec = load_agent_spec(AGENTS / f"{role}.yaml")
        expected = {
            "leadgen_tools:LeadSearch", "leadgen_tools:LeadFetch", "leadgen_tools:LeadCrawl",
        }
        assert expected <= set(spec.tools), (role, spec.tools)
        assert spec.subagents == {}, role
    assert ROLE_DEPENDENCIES["secondary_sources"] == ("official_sources",)
    assert "role_candidates" in ROLE_DEPENDENCIES["candidate_contacts"]
    assert _extract_json('до ```json\n{"ok": true}\n``` после') == {"ok": True}
    print("selftest passed: five enrichment specs + dependency graph")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Пять субагентов enrichment для deep research")
    ap.add_argument("request", nargs="?", help="входной JSON")
    ap.add_argument("result", nargs="?", help="выходной JSON")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.request or not args.result:
        ap.error("нужны request и result")
    request = json.loads(pathlib.Path(args.request).read_text(encoding="utf-8"))
    result = asyncio.run(run(request))
    pathlib.Path(args.result).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
