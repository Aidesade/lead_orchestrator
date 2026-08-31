# -*- coding: utf-8 -*-
"""Единый runtime-конфиг LLM-стадий лидген-пайплайна.

Штатный runtime генератора — **Kimi** (кодовый дефолт `ORQ_LLM_RUNTIME=kimi`).
Claude Agent SDK остаётся явным откатом через `ORQ_LLM_RUNTIME=claude`.
Третий runtime — **GLM через тот же корпоративный шлюз** (`ORQ_LLM_RUNTIME=glm`):
это НЕ отдельный SDK, а та же OpenAI-совместимая механика, что у kimi (движок,
контроллер и агентные стадии в .venv_kimi), только ключ/endpoint/модель берутся
из GLM_API_KEY/GLM_BASE_URL/GLM_MODEL_NAME. Детям значения едут по прежнему
env-каналу KIMI_* — venv-агенты о «бренде» модели не знают.
`ORQ_KIMI_ONLY=1` дополнительно запрещает Claude И glm, и замену модели Kimi K2.7.
Pipeline всех runtime один и тот же — меняется только SDK/модель под стадиями.

Kimi Agent SDK остаётся в соседнем изолированном venv; этот модуль не импортирует SDK.
Claude Agent SDK живёт в ОСНОВНОМ окружении (как в legacy-ветке) и авторизуется
логином Claude Code / ANTHROPIC_API_KEY — ключ Kimi для claude-runtime не нужен.
"""
from __future__ import annotations

import os


DEFAULT_BASE_URL = "https://gpllmkeeper.dtc.tatar/v1"
DEFAULT_MODEL = "kimi-k2.7-code"
# ID модели GLM НА ШЛЮЗЕ может отличаться от апстримного — перекрывается GLM_MODEL_NAME.
DEFAULT_GLM_MODEL = "glm-4.6"
DEFAULT_RUNTIME = "kimi"
# Runtime'ы, ходящие через OpenAI-совместимый шлюз (в отличие от claude-agent-sdk).
GATEWAY_RUNTIMES = ("kimi", "glm")

# Модели Claude по стадиям (алиасы CLI: sonnet/opus/haiku либо полный ID модели).
# Философия та же, что в legacy-ветке: тяжёлое чтение — на sonnet, синтез — на opus.
CLAUDE_STAGE_MODELS = {
    "writer": ("ORQ_WRITER_MODEL", "opus"),        # главный писатель одного .docx
    "enrich": ("ORQ_ENRICH_MODEL", "sonnet"),      # пять enrichment-ролей
    "onepager": ("ORQ_ONEPAGER_MODEL", "opus"),    # HTML one-pager (вёрстка канона)
    "controller": ("ORQ_CONTROLLER_MODEL", "sonnet"),  # NL-план запуска
}


def api_key() -> str:
    """Рабочий ключ АКТИВНОГО шлюзового runtime: для glm сначала GLM_API_KEY, затем
    общие ключи шлюза (KIMI_API_KEY/GPLLM_API_KEY — endpoint один и тот же)."""
    keys = ["KIMI_API_KEY", "GPLLM_API_KEY"]
    if runtime() == "glm":
        keys.insert(0, "GLM_API_KEY")
    for name in keys:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def base_url() -> str:
    if runtime() == "glm":
        configured = (os.environ.get("GLM_BASE_URL") or os.environ.get("KIMI_BASE_URL")
                      or DEFAULT_BASE_URL)
        return configured.strip().rstrip("/")
    return (os.environ.get("KIMI_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")


def glm_model_name() -> str:
    """ID модели GLM независимо от активного runtime (нужен вебу для списка моделей).
    Фолбэка на KIMI_MODEL_NAME нет намеренно: иначе glm молча поехал бы на K2.7."""
    return (os.environ.get("GLM_MODEL_NAME") or DEFAULT_GLM_MODEL).strip()


def model_name() -> str:
    """Единственный ID модели активного шлюзового runtime для controller/research/
    writer/one-pager."""
    if runtime() == "glm":
        return glm_model_name()
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
    """Активный LLM-runtime: 'kimi' (дефолт), 'glm' (шлюз) или явный 'claude'."""
    if kimi_only():
        return "kimi"
    value = (os.environ.get("ORQ_LLM_RUNTIME") or DEFAULT_RUNTIME).strip().lower()
    if value.startswith("kimi"):
        return "kimi"
    if value.startswith("glm"):
        return "glm"
    return "claude"


def claude_model(stage: str) -> str:
    """Модель Claude для стадии ('writer'|'enrich'|'onepager'|'controller')."""
    env_name, default = CLAUDE_STAGE_MODELS[stage]
    return (os.environ.get(env_name) or default).strip() or default


def default_model_flag() -> str:
    """Дефолт --model у orchestrator.py: псевдоним активного runtime."""
    return runtime()


def _require_gateway_key(active: str, key: str) -> None:
    """Общий отказ шлюзовых runtime без ключа — с именами env, которые реально читаются."""
    if not key:
        names = ("GLM_API_KEY, KIMI_API_KEY или GPLLM_API_KEY" if active == "glm"
                 else "KIMI_API_KEY или GPLLM_API_KEY")
        raise RuntimeError(f"не задан {names}")


def ensure_env(*, require_key: bool = False) -> None:
    """Нормализовать окружение до импорта модулей deep research/агентных мостов.

    require_key требует ключ АКТИВНОГО runtime: для шлюзовых (kimi/glm) это ключ шлюза;
    для claude env-ключ не обязателен (авторизация — логин Claude Code или ANTHROPIC_API_KEY).
    """
    active = runtime()
    selected_model = model_name()
    key = api_key()
    if require_key and active in GATEWAY_RUNTIMES:
        _require_gateway_key(active, key)
    if key:
        os.environ["KIMI_API_KEY"] = key
    os.environ.setdefault("KIMI_BASE_URL", DEFAULT_BASE_URL)
    os.environ["KIMI_MODEL_NAME"] = selected_model
    if active == "glm":
        # Резолв фиксируется и в GLM_*: ребёнок, пересчитав акцессоры при ORQ_LLM_RUNTIME=glm,
        # обязан получить те же значения, а не дефолты.
        os.environ["GLM_MODEL_NAME"] = selected_model
        os.environ["GLM_BASE_URL"] = base_url()
        os.environ["KIMI_BASE_URL"] = base_url()
    if kimi_only():
        os.environ["DR_LLM_PROVIDER"] = "kimi"      # аварийный режим перекрывает env жёстко
    else:
        os.environ["ORQ_LLM_RUNTIME"] = active
        os.environ.setdefault("DR_LLM_PROVIDER", active)   # значения совпадают: kimi|glm|claude


def child_env(source: dict[str, str] | None = None, *, require_key: bool = True) -> dict[str, str]:
    """Копия окружения для собственного доверенного подпроцесса оркестратора."""
    active = runtime()
    env = dict(os.environ if source is None else source)
    key = api_key()
    if require_key and active in GATEWAY_RUNTIMES:
        _require_gateway_key(active, key)
    if key:
        env["KIMI_API_KEY"] = key
    env["KIMI_BASE_URL"] = base_url()
    env["KIMI_MODEL_NAME"] = model_name()
    if active == "glm":
        env["GLM_API_KEY"] = key
        env["GLM_BASE_URL"] = base_url()
        env["GLM_MODEL_NAME"] = model_name()
    env["ORQ_KIMI_ONLY"] = "1" if kimi_only() else "0"
    env["ORQ_LLM_RUNTIME"] = active
    env["DR_LLM_PROVIDER"] = active                  # значения совпадают: kimi|glm|claude
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env
