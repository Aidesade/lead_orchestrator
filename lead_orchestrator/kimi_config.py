# -*- coding: utf-8 -*-
"""Единый runtime-конфиг Kimi для всего лидген-пайплайна.

Все LLM-стадии штатного пути используют один ключ, endpoint и точный ID модели.
Kimi Agent SDK остаётся в соседнем изолированном venv; этот модуль не импортирует SDK.
"""
from __future__ import annotations

import os


DEFAULT_BASE_URL = "https://gpllmkeeper.dtc.tatar/v1"
DEFAULT_MODEL = "kimi-k2.7-code"


def api_key() -> str:
    """Рабочий ключ: явный KIMI_API_KEY, затем установленный на машине GPLLM_API_KEY."""
    return (os.environ.get("KIMI_API_KEY") or os.environ.get("GPLLM_API_KEY") or "").strip()


def base_url() -> str:
    return (os.environ.get("KIMI_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")


def model_name() -> str:
    """Единственный ID модели для controller/research/writer/one-pager."""
    configured = (os.environ.get("KIMI_MODEL_NAME") or DEFAULT_MODEL).strip()
    if kimi_only() and configured != DEFAULT_MODEL:
        raise RuntimeError(
            f"Kimi-only runtime закреплён за {DEFAULT_MODEL}; "
            f"KIMI_MODEL_NAME={configured!r} запрещён")
    return configured


def kimi_only() -> bool:
    """Штатный режим запрещает случайный переход на Claude/Anthropic."""
    return (os.environ.get("ORQ_KIMI_ONLY", "1").strip().lower()
            not in ("0", "false", "no", "off", "нет"))


def ensure_env(*, require_key: bool = False) -> None:
    """Нормализовать окружение до импорта модулей deep research/Kimi-мостов."""
    selected_model = model_name()
    key = api_key()
    if require_key and not key:
        raise RuntimeError("не задан KIMI_API_KEY или GPLLM_API_KEY")
    if key:
        os.environ["KIMI_API_KEY"] = key
    os.environ.setdefault("KIMI_BASE_URL", DEFAULT_BASE_URL)
    os.environ["KIMI_MODEL_NAME"] = selected_model
    if kimi_only():
        os.environ["DR_LLM_PROVIDER"] = "kimi"


def child_env(source: dict[str, str] | None = None, *, require_key: bool = True) -> dict[str, str]:
    """Копия окружения для собственного доверенного подпроцесса оркестратора."""
    env = dict(os.environ if source is None else source)
    key = api_key()
    if require_key and not key:
        raise RuntimeError("не задан KIMI_API_KEY или GPLLM_API_KEY")
    if key:
        env["KIMI_API_KEY"] = key
    env["KIMI_BASE_URL"] = base_url()
    env["KIMI_MODEL_NAME"] = model_name()
    env["DR_LLM_PROVIDER"] = "kimi"
    env["ORQ_KIMI_ONLY"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env
