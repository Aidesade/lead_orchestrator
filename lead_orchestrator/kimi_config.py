# -*- coding: utf-8 -*-
"""Единый runtime-конфиг LLM-стадий лидген-пайплайна.

Штатный runtime генератора — **Kimi** (кодовый дефолт `ORQ_LLM_RUNTIME=kimi`).
Claude Agent SDK остаётся явным откатом через `ORQ_LLM_RUNTIME=claude`.
`ORQ_KIMI_ONLY=1` дополнительно запрещает Claude и замену модели Kimi K2.7.
Pipeline обоих runtime один и тот же — меняется только SDK агентных стадий.

Kimi Agent SDK остаётся в соседнем изолированном venv; этот модуль не импортирует SDK.
Claude Agent SDK живёт в ОСНОВНОМ окружении (как в legacy-ветке) и авторизуется
логином Claude Code / ANTHROPIC_API_KEY — ключ Kimi для claude-runtime не нужен.
"""
from __future__ import annotations

import os


DEFAULT_BASE_URL = "https://gpllmkeeper.dtc.tatar/v1"
DEFAULT_MODEL = "kimi-k2.7-code"
DEFAULT_RUNTIME = "kimi"

# Модели Claude по стадиям (алиасы CLI: sonnet/opus/haiku либо полный ID модели).
# Философия та же, что в legacy-ветке: тяжёлое чтение — на sonnet, синтез — на opus.
CLAUDE_STAGE_MODELS = {
    "writer": ("ORQ_WRITER_MODEL", "opus"),        # главный писатель одного .docx
    "enrich": ("ORQ_ENRICH_MODEL", "sonnet"),      # пять enrichment-ролей
    "onepager": ("ORQ_ONEPAGER_MODEL", "opus"),    # HTML one-pager (вёрстка канона)
    "controller": ("ORQ_CONTROLLER_MODEL", "sonnet"),  # NL-план запуска
}


def api_key() -> str:
    """Рабочий ключ Kimi: явный KIMI_API_KEY, затем установленный на машине GPLLM_API_KEY."""
    return (os.environ.get("KIMI_API_KEY") or os.environ.get("GPLLM_API_KEY") or "").strip()


def base_url() -> str:
    return (os.environ.get("KIMI_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")


def model_name() -> str:
    """Единственный ID Kimi-модели для controller/research/writer/one-pager (kimi-runtime)."""
    configured = (os.environ.get("KIMI_MODEL_NAME") or DEFAULT_MODEL).strip()
    if kimi_only() and configured != DEFAULT_MODEL:
        raise RuntimeError(
            f"Kimi-only runtime закреплён за {DEFAULT_MODEL}; "
            f"KIMI_MODEL_NAME={configured!r} запрещён")
    return configured


def kimi_only() -> bool:
    """Жёсткий Kimi-only режим, запрещающий Claude и замену модели K2.7."""
    return (os.environ.get("ORQ_KIMI_ONLY", "0").strip().lower()
            in ("1", "true", "yes", "on", "да"))


def runtime() -> str:
    """Активный LLM-runtime: 'kimi' (дефолт) или явный 'claude'."""
    if kimi_only():
        return "kimi"
    value = (os.environ.get("ORQ_LLM_RUNTIME") or DEFAULT_RUNTIME).strip().lower()
    return "kimi" if value.startswith("kimi") else "claude"


def claude_model(stage: str) -> str:
    """Модель Claude для стадии ('writer'|'enrich'|'onepager'|'controller')."""
    env_name, default = CLAUDE_STAGE_MODELS[stage]
    return (os.environ.get(env_name) or default).strip() or default


def default_model_flag() -> str:
    """Дефолт --model у orchestrator.py: псевдоним активного runtime."""
    return "kimi" if runtime() == "kimi" else "claude"


def ensure_env(*, require_key: bool = False) -> None:
    """Нормализовать окружение до импорта модулей deep research/агентных мостов.

    require_key требует ключ АКТИВНОГО runtime: для kimi это KIMI_API_KEY/GPLLM_API_KEY;
    для claude env-ключ не обязателен (авторизация — логин Claude Code или ANTHROPIC_API_KEY).
    """
    active = runtime()
    selected_model = model_name()
    key = api_key()
    if require_key and active == "kimi" and not key:
        raise RuntimeError("не задан KIMI_API_KEY или GPLLM_API_KEY")
    if key:
        os.environ["KIMI_API_KEY"] = key
    os.environ.setdefault("KIMI_BASE_URL", DEFAULT_BASE_URL)
    os.environ["KIMI_MODEL_NAME"] = selected_model
    if kimi_only():
        os.environ["DR_LLM_PROVIDER"] = "kimi"      # аварийный режим перекрывает env жёстко
    else:
        os.environ["ORQ_LLM_RUNTIME"] = active
        os.environ.setdefault("DR_LLM_PROVIDER", active)   # значения совпадают: kimi|claude


def child_env(source: dict[str, str] | None = None, *, require_key: bool = True) -> dict[str, str]:
    """Копия окружения для собственного доверенного подпроцесса оркестратора."""
    active = runtime()
    env = dict(os.environ if source is None else source)
    key = api_key()
    if require_key and active == "kimi" and not key:
        raise RuntimeError("не задан KIMI_API_KEY или GPLLM_API_KEY")
    if key:
        env["KIMI_API_KEY"] = key
    env["KIMI_BASE_URL"] = base_url()
    env["KIMI_MODEL_NAME"] = model_name()
    env["ORQ_KIMI_ONLY"] = "1" if kimi_only() else "0"
    env["ORQ_LLM_RUNTIME"] = active
    env["DR_LLM_PROVIDER"] = active                  # значения совпадают: kimi|claude
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env
