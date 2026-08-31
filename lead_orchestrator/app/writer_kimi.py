# -*- coding: utf-8 -*-
"""Писатель двух пресейл-.docx (агентный, runtime kimi|claude).

Писатель — АГЕНТ в подпроцессе: основной писатель вызывает веер scout'ов, verifier'ов и
critic; scout/verifier получают read-only поиск/чтение/краул через subprocess-мост в
основной venv. Финальный JSON возвращается сюда, а .docx рендерят ТЕ ЖЕ функции
CRA._write_*_docx. Runtime выбирает kimi_config.runtime():

  * claude (дефолт ветки claude-sdk) — те же скрипты соседней папки запускаются
    python'ом ОСНОВНОГО окружения; под ними Kimi-совместимый адаптер prompt() поверх
    Claude Agent SDK (claude_kimi_adapter.py), субагенты — нативные субагенты SDK.
  * kimi (ORQ_KIMI_ONLY=1) — прежний маршрут: python изолированного .venv_kimi,
    kimi-agent-sdk, нативный Task. Несовместимые SDK по-прежнему не импортируются
    одним интерпретатором.

У каждого документа СВОЙ системный промпт (CRA.PROCESS_MAP_SYSTEM / CRA.ROLES_CONTACTS_SYSTEM)
и СВОИ находки — со своего прохода движка (orchestrator.RESEARCH_PASSES).
Раньше был один PRESALE_SYSTEM и одни общие находки на оба документа.

Старый одиночный OpenAI-совместимый HTTP-писатель оставлен как явный аварийный режим
`KIMI_WRITER_AGENT=0` ТОЛЬКО для kimi-runtime; у claude HTTP-режима нет — там всегда
агент. Автоматически на HTTP не откатываемся, иначе поломка субагентов молчаливо
ухудшила бы качество документов.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile

import kimi_config as KC

KC.ensure_env()


def _cra():
    """Ленивый импорт общих схем и DOCX-рендереров (без загрузки legacy Claude SDK)."""
    import company_research_agent as CRA
    return CRA


BASE_URL_DEFAULT = KC.DEFAULT_BASE_URL
MODEL_DEFAULT = KC.DEFAULT_MODEL

MAX_TOKENS = int(os.environ.get("KIMI_WRITER_MAX_TOKENS", "16000"))
TIMEOUT = float(os.environ.get("KIMI_WRITER_TIMEOUT", "600"))
ATTEMPTS = int(os.environ.get("KIMI_WRITER_ATTEMPTS", "3"))

HERE = pathlib.Path(__file__).resolve().parent
# HERE — lead_orchestrator/app, соседняя папка Kimi-стадии лежит на уровень выше неё.
KIMI_DIR = pathlib.Path(
    os.environ.get("KIMI_DIR") or (HERE.parents[1] / "lead_orchestrator_kimi"))
KIMI_PY = pathlib.Path(os.environ.get("KIMI_PY") or (
    KIMI_DIR / ".venv_kimi" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
))
KIMI_AGENT_CLI = KIMI_DIR / "writer_kimi_agent.py"
KIMI_RESEARCH_CLI = KIMI_DIR / "research_enrichment_agent.py"
ADAPTER_FILE = KIMI_DIR / "claude_kimi_adapter.py"
RESEARCH_TOOL = HERE / "kimi_research_cli.py"

# По умолчанию агенту не ставим общий дедлайн: реальный scout-ресёрч может быть долгим.
# Положительное значение env возвращает опциональный предохранитель для оператора.
AGENT_TIMEOUT = float(os.environ.get("KIMI_WRITER_AGENT_TIMEOUT", "0"))
RESEARCH_SUBAGENTS_TIMEOUT = float(os.environ.get("KIMI_RESEARCH_SUBAGENTS_TIMEOUT", "2400"))
RESEARCH_SUBAGENTS_SCHEMA_VERSION = 3
RESEARCH_COMPANY_CONCURRENCY = int(os.environ.get("KIMI_RESEARCH_COMPANY_CONCURRENCY", "2"))
_RESEARCH_SEMS = {}

_AGENT_ENV_ALLOW = frozenset({
    "PATH", "PYTHONIOENCODING", "PYTHONUTF8", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR",
    "COMSPEC", "PATHEXT", "OS", "TEMP", "TMP", "TMPDIR", "USERPROFILE", "HOMEDRIVE",
    "HOMEPATH", "APPDATA", "LOCALAPPDATA", "ALLUSERSPROFILE", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "PUBLIC",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "HOME", "USER", "LANG", "LC_ALL",
    "TZ", "XDG_RUNTIME_DIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "PLAYWRIGHT_BROWSERS_PATH", "KIMI_API_KEY", "KIMI_BASE_URL", "KIMI_MODEL_NAME",
    "KIMI_WRITER_MAX_STEPS", "KIMI_WRITER_THINKING", "ORQ_KIMI_TOOL_TIMEOUT",
    "KIMI_RESEARCH_SUBAGENT_MAX_STEPS", "KIMI_RESEARCH_SUBAGENT_ATTEMPTS",
    "KIMI_RESEARCH_CHECKPOINT_TTL_H", "KIMI_TOOL_LOG_DIR",
    # claude-runtime: авторизация Claude Code/Anthropic и модели стадий.
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_OAUTH_TOKEN",
    "ORQ_WRITER_MODEL", "ORQ_ENRICH_MODEL",
    "ORQ_SCOUT_MODEL", "ORQ_CRITIC_MODEL", "ORQ_VERIFIER_MODEL",
})


def kimi_key() -> str:
    """Ключ провайдера. Тот же порядок, что у стадии one-pager (orchestrator._kimi_key)."""
    return KC.api_key()


def kimi_model(model: str | None = None) -> str:
    """Имя модели ПИСАТЕЛЯ у активного провайдера. UI/CLI передаёт псевдоним
    ('kimi'/'claude') — реальное имя резолвит конфиг; явное имя модели идёт как есть."""
    m = (model or "").strip()
    if m and m.lower() not in ("kimi", "kimi-writer", "claude", "claude-writer"):
        return m                                   # явное имя модели передали как есть
    if KC.runtime() == "claude":
        return KC.claude_model("writer")
    return KC.model_name()


def _enrich_model(model: str | None) -> str:
    """Модель пяти enrichment-ролей: у claude свой (дешёвый) тир, у kimi — общая K2.7."""
    if KC.runtime() == "claude":
        return KC.claude_model("enrich")
    return kimi_model(model)


def kimi_base_url() -> str:
    return KC.base_url()


def is_kimi(model: str | None) -> bool:
    return str(model or "").strip().lower().startswith("kimi")


def is_claude(model: str | None) -> bool:
    """Псевдоним claude-runtime текущего пайплайна (НЕ legacy-ветка opus/sonnet)."""
    return str(model or "").strip().lower() in ("claude", "claude-writer")


def _agent_python() -> pathlib.Path:
    """Интерпретатор агентных подпроцессов: claude — ОСНОВНОЙ python (claude-agent-sdk
    живёт здесь, CLI бандлится в пакет); kimi — python изолированного .venv_kimi."""
    return pathlib.Path(sys.executable) if KC.runtime() == "claude" else KIMI_PY


def _agent_files_missing(cli: pathlib.Path) -> list[str]:
    """Чего не хватает для запуска агентного подпроцесса (пусто — всё на месте)."""
    required = [_agent_python(), cli, RESEARCH_TOOL]
    if KC.runtime() == "claude":
        required.append(ADAPTER_FILE)              # kimi-совместимый prompt() поверх SDK
    return [str(p) for p in required if not p.is_file()]


def _require_provider_key() -> None:
    """Ключ нужен только kimi-runtime; Claude авторизуется логином Claude Code/ANTHROPIC_*."""
    if KC.runtime() != "claude" and not kimi_key():
        raise RuntimeError("нет ключа Kimi: задай KIMI_API_KEY или GPLLM_API_KEY")


def _agent_enabled() -> bool:
    if KC.runtime() == "claude":
        return True     # у claude-runtime HTTP-фолбэка нет — только агентный писатель
    return os.environ.get("KIMI_WRITER_AGENT", "1").strip().lower() not in (
        "0", "false", "no", "off", "нет",
    )


def _agent_env(api_model: str, share_dir: str) -> dict[str, str]:
    """Минимальное окружение агентного подпроцесса: без токенов Диска/Checko/Dadata.
    Для claude-runtime дополнительно проходят ANTHROPIC_*/CLAUDE_CODE_* из allowlist —
    авторизация Claude; ключи Kimi при этом ему не нужны, но и не мешают."""
    runtime = KC.runtime()
    env = {k: v for k, v in os.environ.items() if k.upper() in _AGENT_ENV_ALLOW}
    env["KIMI_API_KEY"] = kimi_key()
    env["KIMI_BASE_URL"] = kimi_base_url()
    # У claude имя модели едет в request.json, а KIMI_MODEL_NAME остаётся валидным дефолтом.
    env["KIMI_MODEL_NAME"] = api_model if runtime == "kimi" else KC.DEFAULT_MODEL
    env["ORQ_LLM_RUNTIME"] = runtime
    env["PYTHONIOENCODING"] = "utf-8"
    env["ORQ_MAIN_PY"] = sys.executable
    env["ORQ_RESEARCH_TOOL"] = str(RESEARCH_TOOL)
    # kimi-cli пишет session metadata неатомарно; отдельная папка исключает гонку между
    # параллельными company/writer-процессами и не даёт им портить общий ~/.kimi/kimi.json.
    env["KIMI_SHARE_DIR"] = share_dir
    return env


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Остановить Kimi writer вместе с Task-субагентами и браузерами."""
    if proc.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(proc.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except (TimeoutError, ProcessLookupError):
        pass


def _research_stage_sem() -> asyncio.Semaphore:
    """Ограничить число одновременных company-level enrichment-подпроцессов."""
    loop = asyncio.get_running_loop()
    key = id(loop)
    sem = _RESEARCH_SEMS.get(key)
    if sem is None:
        sem = asyncio.Semaphore(max(1, RESEARCH_COMPANY_CONCURRENCY))
        _RESEARCH_SEMS[key] = sem
    return sem


async def _ask_agent_json(system: str, user: str, api_model: str, idx: int,
                          label: str, temp_parent: str, company: str = "",
                          inn: str = "") -> tuple[dict, dict]:
    """Один документ через агентный SDK (kimi или claude) в отдельном подпроцессе."""
    _require_provider_key()
    missing = _agent_files_missing(KIMI_AGENT_CLI)
    if missing:
        raise RuntimeError("не готовы файлы агентного writer: " + ", ".join(missing))

    with tempfile.TemporaryDirectory(prefix=f"kimi_{label}_", dir=temp_parent) as td:
        share_dir = pathlib.Path(td) / "kimi_share"
        share_dir.mkdir()
        req = pathlib.Path(td) / "request.json"
        result = pathlib.Path(td) / "result.json"
        req.write_text(json.dumps({"system": system, "user": user, "model": api_model,
                                   "company": company, "inn": inn, "label": label,
                                   "index": idx},
                                  ensure_ascii=False), encoding="utf-8")
        spawn = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                 if os.name == "nt" else {"start_new_session": True})
        proc = await asyncio.create_subprocess_exec(
            str(_agent_python()), str(KIMI_AGENT_CLI), str(req), str(result),
            cwd=str(KIMI_DIR), env=_agent_env(api_model, str(share_dir)),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **spawn,
        )
        try:
            if AGENT_TIMEOUT > 0:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=AGENT_TIMEOUT)
            else:
                stdout, stderr = await proc.communicate()
        except TimeoutError:
            await _kill_tree(proc)
            raise RuntimeError(
                f"Kimi Agent writer превысил таймаут {AGENT_TIMEOUT:g} с ({label})"
            ) from None
        except asyncio.CancelledError:
            await _kill_tree(proc)
            raise
        except BaseException:
            await _kill_tree(proc)
            raise
        out = stdout.decode("utf-8", "replace").strip()
        err = stderr.decode("utf-8", "replace").strip()
        for line in out.splitlines():
            if "[kimi-agent]" in line:
                print(f"    [{idx}] {line}")
        if proc.returncode or not result.is_file():
            tail = (err or out or "нет вывода")[-1000:]
            raise RuntimeError(f"Kimi Agent writer упал ({label}, code={proc.returncode}): {tail}")
        data = json.loads(result.read_text(encoding="utf-8"))
        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError(f"Kimi Agent writer вернул не объект ({label})")
        return payload, data


async def run_research_subagents(lead: dict, idx: int, seed,
                                 temp_parent: str, model: str | None = None,
                                 checkpoint: str = "", checkpoint_ttl_h: float = 72) -> dict:
    """Запустить dependency-aware граф пяти enrichment-субагентов и вернуть JSON-досье."""
    async with _research_stage_sem():
        return await _run_research_subagents_unlocked(
            lead, idx, seed, temp_parent, model, checkpoint, checkpoint_ttl_h)


async def _run_research_subagents_unlocked(lead: dict, idx: int, seed,
                                            temp_parent: str, model: str | None,
                                            checkpoint: str, checkpoint_ttl_h: float) -> dict:
    _require_provider_key()
    missing = _agent_files_missing(KIMI_RESEARCH_CLI)
    if missing:
        raise RuntimeError("не готовы файлы research-субагентов: " + ", ".join(missing))
    api_model = _enrich_model(model)
    name = (lead.get("name") or "").strip()
    inn = str(lead.get("_inn") or "").strip()
    with tempfile.TemporaryDirectory(prefix="kimi_enrichment_", dir=temp_parent) as td:
        share_dir = pathlib.Path(td) / "kimi_share"
        share_dir.mkdir()
        req = pathlib.Path(td) / "request.json"
        result = pathlib.Path(td) / "result.json"
        req.write_text(json.dumps({
            "lead": lead, "company": name, "inn": inn, "seed": seed, "model": api_model,
            "checkpoint": checkpoint, "checkpoint_ttl_h": checkpoint_ttl_h,
        }, ensure_ascii=False), encoding="utf-8")
        spawn = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                 if os.name == "nt" else {"start_new_session": True})
        proc = await asyncio.create_subprocess_exec(
            str(_agent_python()), str(KIMI_RESEARCH_CLI), str(req), str(result),
            cwd=str(KIMI_DIR), env=_agent_env(api_model, str(share_dir)),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **spawn,
        )
        try:
            if RESEARCH_SUBAGENTS_TIMEOUT > 0:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=RESEARCH_SUBAGENTS_TIMEOUT)
            else:
                stdout, stderr = await proc.communicate()
        except TimeoutError:
            await _kill_tree(proc)
            raise RuntimeError(
                f"пять research-субагентов превысили таймаут {RESEARCH_SUBAGENTS_TIMEOUT:g} с"
            ) from None
        except asyncio.CancelledError:
            await _kill_tree(proc)
            raise
        except BaseException:
            await _kill_tree(proc)
            raise
        out = stdout.decode("utf-8", "replace").strip()
        err = stderr.decode("utf-8", "replace").strip()
        for line in out.splitlines():
            if "[research-subagent]" in line:
                print(f"    [{idx}] {line}")
        data = None
        if result.is_file():
            try:
                data = json.loads(result.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
        # Досье пригодно ДАЖЕ частичным (мягкая деградация ролей): писателю нужен хоть какой-то
        # структурированный контекст, а недостающие роли он доберёт из находок движка. Раньше тут
        # требовались result.json + complete=True + все 5 ролей, и любой сбой ронял писателя.
        if (not isinstance(data, dict)
                or data.get("schema_version") != RESEARCH_SUBAGENTS_SCHEMA_VERSION
                or not isinstance(data.get("roles"), dict)):
            # Пригодного JSON-досье нет — поднимаем НАСТОЯЩУЮ причину. Субагент печатает её в
            # stdout ("[research-subagent] ошибка: ..."), а НЕ в stderr, где висит шум
            # authlib.jose DeprecationWarning (раньше в лог попадал именно он, а не причина).
            err_lines = [ln.strip() for ln in out.splitlines()
                         if "[research-subagent] ошибка" in ln or "ContractError" in ln]
            reason = " | ".join(err_lines) or (err or out or "нет вывода")[-800:]
            raise RuntimeError(f"research-субагенты упали (code={proc.returncode}): {reason}")
        required = {"official_sources", "corporate_contour", "secondary_sources",
                    "role_candidates", "candidate_contacts"}
        missing = sorted(required - set(data.get("roles") or {}))
        if missing or data.get("complete") is not True:
            failed = data.get("roles_failed") or {}
            print(f"    [{idx}] research-субагенты: частичное досье "
                  f"(нет ролей: {', '.join(missing) or '—'}; "
                  f"провалено: {', '.join(sorted(failed)) or '—'}) — пишем из того, что есть")
        return data


_FUNCTION_LABELS = {
    "ceo": "CEO / генеральный директор", "owner": "Собственник",
    "technical": "Технический директор", "chief_engineer": "Главный инженер",
    "cio_it": "CIO / IT", "automation": "Автоматизация", "digital": "Цифровизация",
    "commercial": "Коммерция", "procurement": "Закупки", "supply": "Снабжение",
    "finance": "Финансы", "legal": "Юридический блок", "hr": "HR",
    "production": "Производство", "geology": "Геология", "hse": "HSE",
    "branch_management": "Филиалы / региональное управление",
}
_CONTACT_KIND_LABELS = {
    "personal_work": "персональный рабочий", "functional_inbox": "функциональный inbox",
    "corporate_inbox": "общий корпоративный inbox", "reception_phone": "телефон приёмной",
    "branch_address": "адрес филиала", "official_professional_profile": "официальный профиль",
    "backup_channel": "резервный канал",
}
_SOURCE_CONTEXT_LABELS = {
    "official_company_site": "официальный сайт компании",
    "official_holding_site": "официальный сайт холдинга",
    "government_registry": "государственный реестр", "official_tender": "тендер",
    "vacancy": "вакансия", "business_media": "деловое СМИ",
    "professional_profile": "профессиональный профиль", "social_media": "соцсеть",
    "business_aggregator": "неподтверждённый агрегатор",
    "other_public_source": "иной публичный источник",
}
_STATUS_LABELS = {
    "confirmed": "подтверждено", "probable": "вероятно", "historical": "историческое",
    "unverified": "не подтверждено", "conflicting": "противоречие",
}
_OUTREACH_POLICY_LABELS = {
    "direct_allowed": "прямой outreach допустим", "routing_only": "только маршрутизация",
    "internal_verification_only": "только внутренняя проверка",
    "do_not_cold_outreach": "не использовать для cold outreach",
}


def _source_cell(row: dict) -> str:
    url = row.get("source_url") or ""
    published = row.get("publication_date") or ""
    observed = str(row.get("observed_at") or "")[:10]
    suffix = []
    if published:
        suffix.append(f"опубликовано {published}")
    if observed:
        suffix.append(f"проверено {observed}")
    return url + (" · " + " · ".join(suffix) if suffix else "")


def _status_cell(row: dict) -> str:
    status = _STATUS_LABELS.get(row.get("status"), row.get("status") or "")
    confidence = row.get("confidence")
    return status + (f" · confidence {float(confidence):.2f}" if isinstance(confidence, (int, float)) else "")


def _person_key(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", " ", str(value or "").casefold().replace("ё", "е")).strip()


def apply_research_enrichment(payload: dict, enrichment: dict | None) -> dict:
    """Детерминированно перенести пять ролей в поля DOCX, не полагаясь на пересказ LLM."""
    if not enrichment or enrichment.get("complete") is not True:
        return payload
    roles = enrichment.get("roles") or {}
    official = roles.get("official_sources") or {}
    contour = roles.get("corporate_contour") or {}
    secondary = roles.get("secondary_sources") or {}
    candidates = roles.get("role_candidates") or {}
    contact_result = roles.get("candidate_contacts") or {}

    payload["decision_centers_table"] = [{
        "function": row.get("function") or "",
        "organization": row.get("organization") or "",
        "type": row.get("type") or "",
        "rationale": row.get("rationale") or "",
        "status": _status_cell(row),
        "source": "\n".join(row.get("source_urls") or []),
    } for row in contour.get("decision_centers") or []]

    target = (contour.get("target_company") or {}).get("name") or payload.get("org_name") or "Целевая компания"
    graph_rows = []
    organizations = {
        _person_key(row.get("name")): row for row in contour.get("organizations") or []}
    for row in contour.get("edges") or []:
        node = organizations.get(_person_key(row.get("to"))) or organizations.get(
            _person_key(row.get("from"))) or {}
        graph_rows.append({
            "from": row.get("from") or "", "to": row.get("to") or "",
            "relation": row.get("relation") or "",
            "functions": "; ".join(node.get("functions") or []),
            "status": _status_cell(row), "source": row.get("source_url") or "",
        })
    payload["corporate_graph_table"] = graph_rows

    candidate_rows = [{
        "fio": row.get("full_name") or "",
        "function": _FUNCTION_LABELS.get(row.get("target_function"), row.get("target_function") or ""),
        "reported_title": row.get("reported_title") or "",
        "organization": row.get("organization") or "",
        "inn": row.get("inn") or "",
        "evidence_status": _status_cell(row),
        "current_role": "не присвоена: это кандидат",
        "evidence": row.get("evidence") or "",
        "publication_dates": "; ".join(row.get("publication_dates") or []),
        "observed_at": str(row.get("observed_at") or "")[:10],
        "source": "\n".join(row.get("source_urls") or []),
    } for row in candidates.get("candidates") or []]
    for function in candidates.get("unfilled_functions") or []:
        candidate_rows.append({
            "fio": "не найден", "function": _FUNCTION_LABELS.get(function, function),
            "reported_title": "", "organization": target,
            "inn": "",
            "evidence_status": "пробел исследования", "current_role": "не присвоена",
            "evidence": "кандидат не найден после целевого поиска",
            "publication_dates": "", "observed_at": "",
            "source": "",
        })
    payload["role_candidates_table"] = candidate_rows

    contact_rows = []
    for row in contact_result.get("contacts") or []:
        contact_rows.append({
            "contact": row.get("value") or "",
            "type": _CONTACT_KIND_LABELS.get(row.get("contact_kind"), row.get("contact_kind") or ""),
            "source_context": _SOURCE_CONTEXT_LABELS.get(
                row.get("source_context"), row.get("source_context") or ""),
            "best_use": row.get("best_use") or "",
            "outreach_policy": _OUTREACH_POLICY_LABELS.get(
                row.get("outreach_policy"), row.get("outreach_policy") or ""),
            "candidate": row.get("candidate_full_name") or "общий маршрут",
            "function": _FUNCTION_LABELS.get(row.get("candidate_function"), row.get("candidate_function") or ""),
            "organization": row.get("organization") or "",
            "status": _status_cell(row),
            "source": _source_cell(row),
        })
    for row in contact_result.get("routing_paths") or []:
        contact_rows.append({
            "contact": row.get("route") or "",
            "type": _CONTACT_KIND_LABELS.get(row.get("contact_kind"), row.get("contact_kind") or ""),
            "source_context": _SOURCE_CONTEXT_LABELS.get(
                row.get("source_context"), row.get("source_context") or ""),
            "best_use": row.get("best_use") or row.get("purpose") or "",
            "outreach_policy": _OUTREACH_POLICY_LABELS.get(
                row.get("outreach_policy"), row.get("outreach_policy") or ""),
            "candidate": "общий маршрут", "function": row.get("purpose") or "",
            "organization": row.get("organization") or target,
            "status": _status_cell(row),
            "source": _source_cell(row),
        })
    for row in contact_result.get("candidates_without_contacts") or []:
        contact_rows.append({
            "contact": "не найден", "type": "", "source_context": "",
            "best_use": row.get("reason") or "уточнить ответственного через официальный маршрут",
            "outreach_policy": "только маршрутизация",
            "candidate": row.get("candidate_full_name") or "",
            "function": _FUNCTION_LABELS.get(
                row.get("candidate_function"), row.get("candidate_function") or ""),
            "organization": row.get("organization") or target,
            "status": "пробел исследования", "source": "",
        })
    payload["research_contacts_table"] = contact_rows

    evidence_rows = []
    for row in official.get("confirmed_facts") or []:
        evidence_rows.append({
            "claim": row.get("claim") or "", "level": "официальный",
            "source_type": row.get("source_type") or "", "status": "подтверждено",
            "confidence": f"{float(row.get('confidence') or 0):.2f}",
            "publication_date": row.get("publication_date") or "",
            "observed_at": str(row.get("observed_at") or "")[:10],
            "evidence_text": row.get("evidence_text") or "",
            "source": row.get("source_url") or "",
        })
    for row in secondary.get("findings") or []:
        evidence_rows.append({
            "claim": row.get("claim") or "", "level": "вторичный",
            "source_type": row.get("source_type") or "",
            "status": _STATUS_LABELS.get(row.get("status"), row.get("status") or ""),
            "confidence": f"{float(row.get('confidence') or 0):.2f}",
            "publication_date": row.get("publication_date") or "",
            "observed_at": str(row.get("observed_at") or "")[:10],
            "evidence_text": row.get("evidence_text") or "",
            "source": row.get("source_url") or "",
        })
    for row in secondary.get("hypotheses_for_verification") or []:
        evidence_rows.append({
            "claim": row.get("hypothesis") or "", "level": "гипотеза",
            "source_type": "вторичный источник", "status": "требует проверки",
            "confidence": "", "publication_date": "", "observed_at": "",
            "evidence_text": "Проверить: " + (row.get("verification_needed") or ""),
            "source": "\n".join(row.get("source_urls") or []),
        })
    for gap in official.get("gaps") or []:
        evidence_rows.append({
            "claim": gap.get("description") or "", "level": "официальный пробел",
            "source_type": gap.get("gap_id") or "",
            "status": "не найдено", "confidence": "", "publication_date": "",
            "observed_at": "", "evidence_text": "", "source": "",
        })
    for gap in contour.get("gaps") or []:
        evidence_rows.append({
            "claim": gap, "level": "пробел корпоративного контура", "source_type": "",
            "status": "не найдено", "confidence": "", "publication_date": "",
            "observed_at": "", "evidence_text": "", "source": "",
        })
    for level, source_group in (
            ("проверка официального источника", official),
            ("проверка вторичного источника", secondary)):
        for row in source_group.get("checked_sources") or []:
            evidence_rows.append({
                "claim": "Проверен источник: " + (row.get("source_url") or ""),
                "level": level, "source_type": row.get("source_type") or "",
                "status": row.get("outcome") or "", "confidence": "",
                "publication_date": "", "observed_at": str(row.get("observed_at") or "")[:10],
                "evidence_text": "", "source": row.get("source_url") or "",
            })
    payload["research_evidence_table"] = evidence_rows

    # JSON-досье доступно писателю, но его role_candidates не являются подтверждением текущей
    # должности. Убираем возможное повышение кандидата из legacy-таблиц до детерминированного вывода.
    candidate_keys = {_person_key(row.get("full_name")) for row in candidates.get("candidates") or []}
    candidate_keys.discard("")
    removed = 0
    for field in ("leadership_table", "contacts_table"):
        rows = payload.get(field) or []
        kept = [row for row in rows if _person_key(row.get("fio")) not in candidate_keys]
        removed += len(rows) - len(kept)
        payload[field] = kept
    lpr_text = " ".join((str(payload.get("lpr_profile_title") or ""),
                         str(payload.get("lpr_profile") or "")))
    if any(key and key in _person_key(lpr_text) for key in candidate_keys):
        payload["lpr_profile_title"] = ""
        payload["lpr_profile"] = ""
        removed += 1

    raw_disclaimers = payload.get("disclaimers") or []
    disclaimers = list(raw_disclaimers) if isinstance(raw_disclaimers, list) else [str(raw_disclaimers)]
    note = ("Специализированный агент ролей формирует кандидатов и не присваивает им текущую "
            "должность; статусы и confidence приведены в отдельных таблицах.")
    if note not in disclaimers:
        disclaimers.append(note)
    if removed:
        disclaimers.append(
            f"Из legacy-разделов удалено потенциальных атрибуций кандидатов как текущих ЛПР: {removed}.")
    for conflict in candidates.get("conflicts") or []:
        text = "Конфликт по кандидату: " + str(conflict)
        if text not in disclaimers:
            disclaimers.append(text)
    payload["disclaimers"] = disclaimers
    return payload


# Дополнение к системным промптам для АВАРИЙНОГО legacy HTTP-режима
# (`KIMI_WRITER_AGENT=0`). В агентном режиме этот хвост не используется: там имена
# Task-субагентов и JSON-деливерабл переопределяет writer_kimi_agent.py.
SYSTEM_TAIL = """

--- РЕЖИМ РАБОТЫ (ПЕРЕОПРЕДЕЛЯЕТ ВСЁ ПРО ИНСТРУМЕНТЫ ВЫШЕ) ---
Инструментов у тебя НЕТ: ни deep_research, ни WebSearch/WebFetch, ни save_*_docx.
Дипресёрч уже отработал отдельным движком под ЭТОТ документ, его находки целиком даны
в сообщении пользователя. Ничего не вызывай, ничего не проси, не пиши «я не могу
выполнить поиск» — весь нужный материал уже перед тобой.

ДЕЛИВЕРАБЛ — не вызов инструмента, а ОТВЕТ: строго ОДИН JSON-объект по схеме из
запроса. Без markdown, без ```-заборов, без пояснений до и после. Никаких комментариев
внутри JSON. Требование «работа не выполнена, пока не вызван save_*_docx» здесь не
действует: заполненный JSON и есть сохранение — .docx соберёт вызывающий код.

ДАННЫЕ: бери из находок. Ничего не выдумывай: если данных по полю нет — оставь пустую
строку или пустой список. Но и не пиши «не подтверждено» там, где в находках данные ЕСТЬ
(филиалы с директорами и телефонами, соцсети, официальные контакты, прямые контакты ЛПР) —
их надо перенести вместе с источником.
"""


def _user_prompt(title: str, schema: dict, name: str, inn: str,
                 findings: str, extra: str = "") -> str:
    return (
        f"КОМПАНИЯ: {name}\n"
        f"ИНН: {inn}\n\n"
        "НАХОДКИ ДИПРЕСЁРЧА (у строк проставлен source URL):\n"
        "-----8<-----\n"
        f"{findings}\n"
        "----->8-----\n\n"
        f"ЗАДАЧА: на основе ЭТИХ находок заполни документ «{title}» и верни ОДИН JSON-объект "
        "строго по приведённой схеме.\n"
        + (extra + "\n" if extra else "")
        + "\nСХЕМА (JSON Schema):\n"
        + json.dumps(schema, ensure_ascii=False)
    )


PROCESS_EXTRA = (
    "Обязательно: сначала process_catalog — ВЕСЬ спектр процессов компании (включая те, где "
    "точки внедрения ИИ нет, с «—»), каталог не обрезай; затем processes — детальные карточки "
    "по приоритетным процессам as-is с болями и точкой внедрения ИИ (RAG / автономные агенты "
    "с упором в агентный режим / ИИ-Коуч). Опирайся на профиль, финансы и контракты из находок. "
    "У фактов проставляй source."
)

ROLES_EXTRA = (
    "Обязательно перенеси из находок: таблицу филиалов (директор + телефон), соцсети, "
    "официальные контакты, блок «Экосистема и вертикаль» -> ecosystem_table, а если есть "
    "блок «ПРЯМЫЕ КОНТАКТЫ ЛПР» — прямой email/телефон ЛПР с источником и уровнем доверия. "
    "Обязательно заполни contacts_table — сводную таблицу ВСЕХ найденных лиц по корзинам "
    "(IT/цифровизация/продукт, коммерция/продажи/закупки, первые лица, финансы, филиалы); "
    "в неё лица из leadership_table и branches_table переносятся намеренно."
)


def _client():
    from openai import AsyncOpenAI          # локальный импорт: не тянем SDK, если Kimi не выбран

    key = kimi_key()
    if not key:
        raise RuntimeError("нет ключа Kimi: задай KIMI_API_KEY или GPLLM_API_KEY")
    return AsyncOpenAI(base_url=kimi_base_url(), api_key=key,
                       timeout=TIMEOUT, max_retries=0)


async def _ask_json(client, model: str, system: str, user: str, idx: int, label: str):
    """Спросить у Kimi JSON. Возвращает (payload, usage)."""
    last = ""
    prompt = user
    for attempt in range(1, ATTEMPTS + 1):
        kwargs = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "max_tokens": MAX_TOKENS,
            "temperature": 0.2,
        }
        # JSON-режим поддерживают не все OpenAI-совместимые шлюзы. Просим его только на
        # первой попытке: если шлюз ругнётся — идём дальше без него, а не валим стадию.
        if attempt == 1:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            r = await client.chat.completions.create(**kwargs)
        except Exception as e:                     # noqa: BLE001 — сеть/шлюз/лимиты
            last = f"{type(e).__name__}: {str(e)[:140]}"
            print(f"    [{idx}] [kimi:{label}] попытка {attempt}/{ATTEMPTS}: {last}")
            await asyncio.sleep(2 * attempt)
            continue

        text = (r.choices[0].message.content or "").strip()
        try:
            return _cra()._extract_json(text), getattr(r, "usage", None)
        except (ValueError, json.JSONDecodeError) as e:
            last = f"не JSON: {str(e)[:120]}"
            print(f"    [{idx}] [kimi:{label}] попытка {attempt}/{ATTEMPTS}: {last}")
            prompt = (user + "\n\nПРЕДЫДУЩИЙ ОТВЕТ НЕ РАЗОБРАЛСЯ КАК JSON. "
                             "Верни ТОЛЬКО JSON-объект по схеме, без единого лишнего символа.")

    raise RuntimeError(f"Kimi не вернул валидный JSON за {ATTEMPTS} попыток ({label}): {last}")


def _tokens(usage) -> int:
    return int(getattr(usage, "total_tokens", 0) or 0) if usage else 0


async def write_two_docx(lead: dict, idx: int, findings_process: str, findings_roles: str,
                         d_tmp: str, s_tmp: str, model: str | None = None,
                         enrichment: dict | None = None) -> float:
    """Сделать оба .docx активным runtime. Возвращает стоимость: у claude её отдаёт
    SDK (сумма сессий писателя), у kimi всегда 0.0 — шлюз цену не отдаёт.

    У каждого документа СВОЙ системный промпт (PROCESS_MAP_SYSTEM / ROLES_CONTACTS_SYSTEM)
    и СВОИ находки — со своего прохода движка. Раньше был один PRESALE_SYSTEM и одни общие
    находки на оба документа.

    Ретраи и проверку размеров файлов делает вызывающий (orchestrator: 3 попытки по факту
    отсутствия/малого размера .docx) — здесь не дублируем.
    """
    CRA = _cra()
    name = (lead.get("name") or "").strip()
    inn = str(lead.get("_inn") or "").strip()
    api_model = kimi_model(model)

    jobs = (
        (CRA.PROCESS_MAP_SYSTEM, "карта бизнес-процессов", CRA.PROCESS_MAP_SCHEMA,
         PROCESS_EXTRA, findings_process, CRA._write_process_map_docx, d_tmp, "процессы"),
        (CRA.ROLES_CONTACTS_SYSTEM, "карта ролей и контактов · пресейл", CRA.ROLES_CONTACTS_SCHEMA,
         ROLES_EXTRA, findings_roles, CRA._write_roles_contacts_docx, s_tmp, "роли"),
    )

    use_agent = _agent_enabled()
    client = None if use_agent else _client()
    total_tokens = 0
    cost = 0.0
    tag = KC.runtime()                       # 'kimi' | 'claude' — префикс строк лога
    try:
        for system, title, schema, extra, findings, render, path, label in jobs:
            user = _user_prompt(title, schema, name, inn, findings, extra)
            if use_agent:
                payload, meta = await _ask_agent_json(
                    system, user, api_model, idx, label, os.path.dirname(path), name, inn)
                usage = None
                cost += float(meta.get("cost_usd") or 0.0)
                sub = meta.get("subagents") or {}
                print(f"    [{idx}] [{tag}:{label}] субагенты: "
                      f"scout={sub.get('scout', 0)}, critic={sub.get('critic', 0)}, "
                      f"verifier={sub.get('verifier', 0)}")
                web = meta.get("web_tools") or {}
                print(f"    [{idx}] [{tag}:{label}] прямой веб: "
                      f"search={web.get('LeadSearch', 0)}, fetch={web.get('LeadFetch', 0)}, "
                      f"crawl={web.get('LeadCrawl', 0)}")
            else:
                payload, usage = await _ask_json(
                    client, api_model, system + SYSTEM_TAIL, user, idx, label)
            total_tokens += _tokens(usage)
            # Рендер ждёт org_name: модель иногда кладёт company/название в другое поле.
            payload.setdefault("org_name", name)
            if label == "роли":
                # Обогащение — «улучшайзер» payload; его сбой НЕ должен ронять документ:
                # роли всё равно отрендерятся из находок движка (мягкая деградация).
                try:
                    apply_research_enrichment(payload, enrichment)
                except Exception as exc:                 # noqa: BLE001
                    print(f"    [{idx}] enrichment-таблицы не применены "
                          f"({type(exc).__name__}: {str(exc)[:120]}); документ ролей — без них")
            await asyncio.to_thread(render, dict(payload), path)
            mode = "agent" if use_agent else "legacy-http"
            print(f"    [{idx}] → {tag}:{label} сохранён ({api_model}, {mode})")
    finally:
        if client is not None:
            await client.close()

    if use_agent:
        price = f", ~${cost:.2f}" if cost else ""
        print(f"    [{idx}] [{tag}] агентный писатель завершён ({api_model}{price})")
    else:
        print(f"    [{idx}] [{tag}] legacy HTTP: {total_tokens} токенов ({api_model})")
    return cost          # kimi: 0.0 — gpllmkeeper цену за вызов не возвращает
