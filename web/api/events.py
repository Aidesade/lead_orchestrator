# -*- coding: utf-8 -*-
"""Разбор stdout оркестратора в типизированные события.

Оркестратор не умеет структурированных событий — он печатает русский текст
(orchestrator.py: 201-235, 379, 434, 778-835, 885-980, 1006-1013). Зато префиксы
стабильны, а _Tee флашит каждую строку (orchestrator.py:547-566), поэтому строки
можно тянуть из пайпа по одной и классифицировать здесь.

ВАЖНО: имя компании в строке успеха обрезано до 40 символов (orchestrator.py:980),
поэтому единственный надёжный ключ компании — индекс [idx]: позиция в ОТОБРАННОМ
списке, т.е. в том самом JSON, что оркестратор сохранил (pipeline._save). По нему и
матчим карточки в UI.

Если в оркестратор когда-нибудь добавят `--json-events`, этот модуль станет тонким
фолбэком: точки печати все собраны в orchestrator.py и deep_research_engine._note.
"""
from __future__ import annotations

import re
from typing import Any, Dict

# --- строки прогона ----------------------------------------------------------
RE_LOG_PATH = re.compile(r"^\[лог\] копия вывода: (.+)$")
RE_VOLUME = re.compile(r"^\[объём\].*?=\s*(\d+)\s+компаний всего")
RE_PHASE1 = re.compile(r"^=== ФАЗА 1")
RE_PHASE2 = re.compile(r"^=== ФАЗА 2")
RE_ESTIMATE = re.compile(r"^\[оценка\] (.+)$")
RE_LEADS_SAVED = re.compile(r"^\[1/2\] собрано (\d+) \| JSON: (.+)$")

# --- фаза 1: сбор ------------------------------------------------------------
RE_SEARCH_TOTAL = re.compile(r"^\s+всего по фильтру: (\d+)")
RE_SEARCH_PAGE = re.compile(r"^\s+стр\.(\d+): \+(\d+) \(итого (\d+)\)")
RE_PICKED = re.compile(r"^\s+-> отобрано (\d+)")
RE_CONTACTS = re.compile(r"^\s+RusProfile: (\d+) \|")
RE_STUBS = re.compile(r"^\s+Диск: (\d+)/(\d+) компаний")

# --- фаза 2: компании --------------------------------------------------------
RE_COMPANY_NOTE = re.compile(r"^\s+\[(\d+)\] (.+)$")
RE_COMPANY_DONE = re.compile(r"^\s*✓ \[(\d+)\] (.+?) -> (.+)$")
RE_COMPANY_SKIP = re.compile(r"^\s*↷ \[(\d+)\] (.+)$")
RE_RETRY = re.compile(r"^\s*\[retry (\d+)/(\d+)\] (.+?): (.+)$")
RE_ERROR = re.compile(r"^\s*\[!\] (.+)$")
RE_OUTBOX = re.compile(r"^\s*\[outbox\] (.+)$")
RE_ENGINE = re.compile(r"^\[deep_research\] (.+)$")
RE_ONEPAGER_STAGE = re.compile(r"^\[onepager\] стадия (включена|отключена)(.*)$")

# --- итог --------------------------------------------------------------------
RE_DONE = re.compile(r"^\[ГОТОВО\] компаний: (\d+)/(\d+)")
RE_DONE_FILES = re.compile(r"файлов за прогон: (\d+)")
RE_COST = re.compile(r"\$([\d.]+)")
RE_WARN = re.compile(r"^\[ВНИМАНИЕ\] (.+)$")

_COST_TAIL = re.compile(r"\(\$([\d.]+)\)")


def _stage_of(text: str) -> str:
    """Какая стадия компании идёт, судя по её строке."""
    if text.startswith("deep_research"):
        return "research"
    if text.startswith("person_enrich"):
        return "person"
    if text.startswith("→"):
        return "writing"
    if "onepager" in text:
        return "onepager"
    return "research"


def parse(line: str) -> Dict[str, Any]:
    """Строка лога -> событие. Всегда возвращает событие (в худшем случае type=log),
    чтобы UI мог показать сырой лог целиком, ничего не теряя."""
    raw = line.rstrip("\n")
    ev: Dict[str, Any] = {"type": "log", "line": raw}
    s = raw.strip()
    if not s:
        return ev

    if m := RE_LOG_PATH.match(s):
        return {**ev, "type": "log_path", "path": m.group(1).strip()}

    if m := RE_VOLUME.match(s):
        return {**ev, "type": "volume", "total": int(m.group(1))}

    if RE_PHASE1.match(s):
        return {**ev, "type": "phase", "phase": "collect"}

    if RE_PHASE2.match(s):
        return {**ev, "type": "phase", "phase": "research"}

    if m := RE_ESTIMATE.match(s):
        return {**ev, "type": "estimate", "text": m.group(1)}

    if m := RE_LEADS_SAVED.match(s):
        # Ключевое событие: путь к JSON отобранных лидов. По нему UI получает имена и ИНН
        # компаний и связывает их с индексами [idx] из строк фазы 2.
        return {**ev, "type": "leads_saved",
                "count": int(m.group(1)), "path": m.group(2).strip()}

    if m := RE_ONEPAGER_STAGE.match(s):
        return {**ev, "type": "onepager_stage", "enabled": m.group(1) == "включена"}

    # --- компании -------------------------------------------------------------
    if m := RE_COMPANY_DONE.match(raw):
        idx, name, rest = int(m.group(1)), m.group(2).strip(), m.group(3)
        # Путь на Диске содержит пробелы («есть все контактные данные»), а хвостовые
        # теги отбиты ДВУМЯ пробелами — режем по ним, иначе папка обрежется на первом слове.
        parts = re.split(r"\s{2,}", rest.strip())
        comp_dir = parts[0].strip()
        tail = " ".join(parts[1:])
        cost = float(cm.group(1)) if (cm := _COST_TAIL.search(tail)) else 0.0
        return {**ev, "type": "company_done", "idx": idx, "name": name,
                "dir": comp_dir, "one_pager": "+one-pager" in tail, "cost": cost}

    if m := RE_COMPANY_SKIP.match(raw):
        idx, text = int(m.group(1)), m.group(2).strip()
        # «уже на Диске — пропуск» = компания целиком по резюму; «.docx уже на Диске» =
        # переделывается только one-pager (orchestrator.py:885-891).
        return {**ev, "type": "company_skipped", "idx": idx, "text": text,
                "mode": "docx" if ".docx уже на Диске" in text else "full"}

    if m := RE_RETRY.match(raw):
        return {**ev, "type": "retry", "attempt": int(m.group(1)),
                "total": int(m.group(2)), "name": m.group(3).strip(),
                "text": m.group(4).strip()}

    if m := RE_ERROR.match(raw):
        return {**ev, "type": "error", "text": m.group(1).strip()}

    if m := RE_OUTBOX.match(raw):
        return {**ev, "type": "outbox", "text": m.group(1).strip()}

    if m := RE_COMPANY_NOTE.match(raw):
        idx, text = int(m.group(1)), m.group(2).strip()
        return {**ev, "type": "company_stage", "idx": idx,
                "stage": _stage_of(text), "text": text}

    if m := RE_ENGINE.match(s):
        return {**ev, "type": "engine", "text": m.group(1).strip()}

    # --- фаза 1 ---------------------------------------------------------------
    if m := RE_SEARCH_TOTAL.match(raw):
        return {**ev, "type": "search", "found": int(m.group(1))}
    if m := RE_SEARCH_PAGE.match(raw):
        return {**ev, "type": "search", "page": int(m.group(1)), "collected": int(m.group(3))}
    if m := RE_PICKED.match(raw):
        return {**ev, "type": "search", "picked": int(m.group(1))}
    if m := RE_CONTACTS.match(raw):
        return {**ev, "type": "contacts", "done": int(m.group(1))}
    if m := RE_STUBS.match(raw):
        return {**ev, "type": "stubs", "done": int(m.group(1)), "total": int(m.group(2))}

    # --- итог -----------------------------------------------------------------
    if m := RE_DONE.match(s):
        files = int(fm.group(1)) if (fm := RE_DONE_FILES.search(s)) else 0
        cost = float(cm.group(1)) if (cm := RE_COST.search(s)) else 0.0
        return {**ev, "type": "finished", "ok": int(m.group(1)),
                "total": int(m.group(2)), "files": files, "cost": cost}

    if m := RE_WARN.match(s):
        return {**ev, "type": "warning", "text": m.group(1).strip()}

    return ev
