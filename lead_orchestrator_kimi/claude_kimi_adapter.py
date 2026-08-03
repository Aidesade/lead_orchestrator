# -*- coding: utf-8 -*-
"""Kimi-совместимый ``prompt()`` поверх Claude Agent SDK.

Агентные скрипты этой папки (writer_kimi_agent, research_enrichment_agent,
onepager_kimi) написаны против интерфейса ``kimi_agent_sdk.prompt()``:
async-генератор сообщений с ``.role`` / ``.tool_calls`` / ``.tool_call_id`` /
``.extract_text()``. Этот модуль отдаёт ТОТ ЖЕ интерфейс поверх claude_agent_sdk,
поэтому граф ролей, валидация контрактов, evidence trace и checkpoint работают
без изменений — под ними меняется только SDK.

Запускать ТОЛЬКО python'ом ОСНОВНОГО окружения (там claude-agent-sdk и PyYAML);
в .venv_kimi модуль не импортировать — pydantic-core несовместим. Спеки агентов
читаются из ТЕХ ЖЕ kimi_agent/<role>.yaml + <role>.md: один источник правды для
обоих runtime, prompt_hash графа ролей считается по тем же файлам.

Соответствие сущностей:
  kimi tools leadgen_tools:Lead*     -> in-process MCP-сервер "lead" (mcp__lead__Lead*),
                                        внутри тот же subprocess-мост kimi_research_cli.py
  kimi Task + subagents из yaml      -> нативные субагенты SDK (тул "Agent"),
                                        в сообщениях наружу нормализуется обратно в
                                        Task/subagent_name — счётчики писателя не меняются
  сообщение "tool" + tool_call_id    -> ToolResultBlock из UserMessage
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import signal
import subprocess
import sys

SERVER = "lead"
_MCP_PREFIX = f"mcp__{SERVER}__"
HERE = pathlib.Path(__file__).resolve().parent
_DEFAULT_TOOL = HERE.parent / "lead_orchestrator" / "kimi_research_cli.py"
_TIMEOUT = float(os.environ.get("ORQ_KIMI_TOOL_TIMEOUT", "180"))

# Побочные эффекты запрещены по построению — у kimi-агентов не было ни файловых,
# ни встроенных веб-инструментов; весь веб идёт через мост Lead* (SSRF-guard движка).
_DISALLOWED_BUILTINS = [
    "Bash", "Edit", "Write", "NotebookEdit", "Read", "Glob", "Grep",
    "WebSearch", "WebFetch", "TodoWrite", "Skill",
]

# Модели субагентов писателя: та же философия, что в legacy-ветке оркестратора —
# тяжёлое чтение на sonnet, opus остаётся на главном писателе.
_SUBAGENT_MODEL_ENV = {
    "scout": "ORQ_SCOUT_MODEL",
    "critic": "ORQ_CRITIC_MODEL",
    "verifier": "ORQ_VERIFIER_MODEL",
}

# Стоимость последней завершённой сессии (USD). Писатель читает её после цикла;
# у enrichment роли идут параллельно, там поле не используется.
LAST_COST = {"usd": 0.0}


class _Fn:
    __slots__ = ("name", "arguments")

    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _Call:
    __slots__ = ("id", "function")

    def __init__(self, call_id: str, function: _Fn):
        self.id = call_id
        self.function = function


class _Msg:
    """Форма сообщения kimi_agent_sdk, которой ждут потребители prompt()."""

    __slots__ = ("role", "tool_calls", "tool_call_id", "_text")

    def __init__(self, role: str, text: str = "", *, calls=None, call_id: str = ""):
        self.role = role
        self.tool_calls = list(calls or [])
        self.tool_call_id = call_id or None
        self._text = text

    def extract_text(self) -> str:
        return self._text


def load_spec(agent_file) -> dict:
    """Прочитать kimi_agent/<role>.yaml: системный промпт (md), тулы, субагенты."""
    import yaml

    path = pathlib.Path(agent_file)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    agent = raw.get("agent") or {}
    system_path = (path.parent / str(agent.get("system_prompt_path") or "")).resolve()
    tools = [str(t) for t in (agent.get("tools") or [])]
    subagents = {}
    for name, sub in (agent.get("subagents") or {}).items():
        sub_path = (path.parent / str((sub or {}).get("path") or f"{name}.yaml")).resolve()
        subagents[name] = {
            "path": sub_path,
            "description": str((sub or {}).get("description") or ""),
        }
    return {
        "name": str(agent.get("name") or path.stem),
        "system_prompt": system_path.read_text(encoding="utf-8"),
        "lead_tools": [t.split(":", 1)[1] for t in tools if t.startswith("leadgen_tools:")],
        "wants_subagents": any(t.endswith(":Task") for t in tools),
        "subagents": subagents,
    }


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Убить мост вместе с Crawl4AI/Playwright-потомками на Windows и Linux."""
    if proc.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(proc.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
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


async def _bridge(*args: str) -> tuple[bool, str]:
    """Вызов read-only ресёрч-моста в основном venv. Тексты ошибок совпадают с
    kimi-версией (leadgen_tools): на них завязан _tool_success у графа ролей."""
    python = os.environ.get("ORQ_MAIN_PY") or sys.executable
    script = os.environ.get("ORQ_RESEARCH_TOOL") or str(_DEFAULT_TOOL)
    proc: asyncio.subprocess.Process | None = None
    try:
        spawn = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                 if os.name == "nt" else {"start_new_session": True})
        proc = await asyncio.create_subprocess_exec(
            python, script, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **spawn,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
    except TimeoutError:
        if proc is not None:
            await _kill_tree(proc)
        return False, f"Ресёрч-инструмент превысил таймаут {_TIMEOUT:g} с"
    except asyncio.CancelledError:
        if proc is not None:
            await _kill_tree(proc)
        raise
    except Exception as exc:  # noqa: BLE001 — ошибка должна стать результатом тула
        return False, f"Не удалось запустить ресёрч-инструмент: {exc}"

    out = stdout.decode("utf-8", "replace").strip()
    err = stderr.decode("utf-8", "replace").strip()
    if proc.returncode:
        return False, err or out or f"процесс завершился с кодом {proc.returncode}"
    return True, out or "Инструмент не вернул текста."


def _tool_result(ok: bool, text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": not ok}


def _build_lead_server():
    """In-process MCP-сервер с теми же тремя инструментами, что у kimi-агентов."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool("LeadSearch",
          "Поиск по открытому вебу через мульти-бэкенд движок лидгена. Возвращает URL, "
          "заголовки и сниппеты. Используй для обнаружения источников; факт подтверждай "
          "через LeadFetch.",
          {"type": "object",
           "properties": {
               "query": {"type": "string",
                         "description": "Поисковый запрос с названием компании/ИНН и искомым фактом"},
               "limit": {"type": "integer", "minimum": 1, "maximum": 12,
                         "description": "Число результатов (по умолчанию 6)"}},
           "required": ["query"]})
    async def lead_search(args):
        limit = max(1, min(int(args.get("limit") or 6), 12))
        ok, text = await _bridge("search", "--query", str(args.get("query") or ""),
                                 "--limit", str(limit))
        return _tool_result(ok, text)

    @tool("LeadFetch",
          "Открыть конкретную страницу и вернуть очищенный текст с URL. Не подменяй "
          "открытие страницы поисковым сниппетом: существенные факты проверяй этим "
          "инструментом.",
          {"type": "object",
           "properties": {"url": {"type": "string",
                                  "description": "Полный http(s)-URL конкретной страницы"}},
           "required": ["url"]})
    async def lead_fetch(args):
        ok, text = await _bridge("fetch", "--url", str(args.get("url") or ""))
        return _tool_result(ok, text)

    @tool("LeadCrawl",
          "Безопасный обход разделов публичного сайта (HTTP-only, private/loopback URL "
          "блокируются). Используй, когда нужен раздел сайта целиком.",
          {"type": "object",
           "properties": {
               "domain": {"type": "string",
                          "description": "Домен или URL сайта компании/ведомства"},
               "keywords": {"type": "array", "items": {"type": "string"},
                            "description": "Слова направления для ранжирования обхода"},
               "max_pages": {"type": "integer", "minimum": 1, "maximum": 15,
                             "description": "Максимум страниц (по умолчанию 8)"}},
           "required": ["domain"]})
    async def lead_crawl(args):
        keywords = [str(k) for k in (args.get("keywords") or []) if str(k).strip()]
        pages = max(1, min(int(args.get("max_pages") or 8), 15))
        ok, text = await _bridge(
            "crawl", "--domain", str(args.get("domain") or ""),
            "--keywords", json.dumps(keywords, ensure_ascii=False),
            "--max-pages", str(pages))
        return _tool_result(ok, text)

    return create_sdk_mcp_server(name=SERVER, version="1.0.0",
                                 tools=[lead_search, lead_fetch, lead_crawl])


def _subagent_definitions(spec: dict, sdk):
    """Субагенты писателя из тех же yaml/md; модели — как в legacy-ветке (sonnet)."""
    agents = {}
    for name, sub in spec["subagents"].items():
        child = load_spec(sub["path"])
        child_tools = [_MCP_PREFIX + t for t in child["lead_tools"]]
        agents[name] = sdk.AgentDefinition(
            description=sub["description"] or f"Субагент {name}",
            prompt=child["system_prompt"],
            tools=child_tools,
            mcpServers=[SERVER] if child_tools else None,
            model=(os.environ.get(_SUBAGENT_MODEL_ENV.get(name, "")) or "sonnet"),
        )
    return agents


def _convert_call(block) -> _Call:
    """ToolUseBlock -> вызов в форме kimi: имена без MCP-префикса, Agent -> Task."""
    name = block.name or ""
    payload = dict(block.input or {})
    if name.startswith(_MCP_PREFIX):
        name = name[len(_MCP_PREFIX):]
    elif name in ("Agent", "Task"):
        name = "Task"
        payload.setdefault(
            "subagent_name", payload.get("subagent_type") or payload.get("agent") or "?")
    return _Call(str(block.id or ""), _Fn(name, json.dumps(payload, ensure_ascii=False)))


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "") for part in content
            if isinstance(part, dict) and part.get("type") == "text")
    return ""


async def prompt(user_input: str, *, model=None, thinking=False, yolo=True,
                 final_message_only=False, agent_file=None,
                 max_steps_per_turn=None, **_ignored):
    """Асинхронный генератор kimi-образных сообщений поверх Claude Agent SDK.

    Сигнатура повторяет kimi_agent_sdk.prompt(): агентные скрипты вызывают её,
    не зная, какой SDK внизу. yolo игнорируется (permission_mode=bypassPermissions
    и есть авто-аппрув), thinking отдан на усмотрение модели.
    """
    del thinking, yolo
    try:
        import claude_agent_sdk as sdk
    except ImportError as exc:
        raise RuntimeError(
            "claude-agent-sdk не установлен в этом интерпретаторе; "
            "claude-runtime запускается python'ом основного окружения") from exc

    if agent_file is None:
        raise RuntimeError("claude_kimi_adapter.prompt: нужен agent_file со спекой роли")
    spec = load_spec(agent_file)

    with_subagents = bool(spec["wants_subagents"] and spec["subagents"])
    allowed = [_MCP_PREFIX + t for t in spec["lead_tools"]]
    disallowed = list(_DISALLOWED_BUILTINS)
    agents = None
    if with_subagents:
        agents = _subagent_definitions(spec, sdk)
        allowed.append("Agent")
    else:
        disallowed.append("Agent")
    # У субагентов свои Lead*-тулы -> сервер нужен, даже если у главного агента их нет.
    mcp_servers = ({SERVER: _build_lead_server()}
                   if spec["lead_tools"] or with_subagents else {})

    turns = None
    if isinstance(max_steps_per_turn, int) and max_steps_per_turn > 0:
        turns = max_steps_per_turn

    options = sdk.ClaudeAgentOptions(
        model=model or None,
        system_prompt=spec["system_prompt"],
        mcp_servers=mcp_servers,
        allowed_tools=allowed,
        disallowed_tools=disallowed,
        agents=agents,
        permission_mode="bypassPermissions",
        setting_sources=[],          # не подхватывать CLAUDE.md/настройки/MCP репозитория
        strict_mcp_config=True,
        max_turns=turns,
    )

    final_from_result = ""
    failure = ""
    last_plain: _Msg | None = None      # запасной финал, если SDK не отдал result

    async for message in sdk.query(prompt=user_input, options=options):
        if isinstance(message, sdk.AssistantMessage):
            # Внутренние сообщения субагентов не отдаём: kimi-счётчики и evidence
            # trace ждут только верхний уровень (прямые вызовы главного агента).
            if getattr(message, "parent_tool_use_id", None):
                continue
            text_parts, calls = [], []
            for block in message.content:
                if isinstance(block, sdk.TextBlock):
                    text_parts.append(block.text)
                elif isinstance(block, sdk.ToolUseBlock):
                    calls.append(_convert_call(block))
            msg = _Msg("assistant", "".join(text_parts).strip(), calls=calls)
            if not final_message_only:
                yield msg
            elif msg.extract_text() and not msg.tool_calls:
                last_plain = msg
        elif isinstance(message, sdk.UserMessage):
            if final_message_only or getattr(message, "parent_tool_use_id", None):
                continue
            blocks = message.content if isinstance(message.content, list) else []
            for block in blocks:
                if isinstance(block, sdk.ToolResultBlock):
                    yield _Msg("tool", _result_text(block.content),
                               call_id=str(block.tool_use_id or ""))
        elif isinstance(message, sdk.ResultMessage):
            LAST_COST["usd"] = float(message.total_cost_usd or 0.0)
            if message.is_error:
                failure = str(message.result or message.subtype or "session error")
            elif message.result:
                final_from_result = message.result.strip()

    if failure:
        raise RuntimeError(f"Claude-сессия завершилась ошибкой: {failure[:500]}")
    if final_from_result:
        # Канонический финал SDK — гарантируем потребителю «assistant без tool_calls».
        yield _Msg("assistant", final_from_result)
    elif last_plain is not None:
        yield last_plain


def selftest() -> int:
    """Офлайн-проверка: спеки читаются, маппинг сообщений соответствует kimi-форме."""
    from types import SimpleNamespace

    writer = load_spec(HERE / "kimi_agent" / "writer.yaml")
    assert writer["lead_tools"] == ["LeadSearch", "LeadFetch", "LeadCrawl"]
    assert writer["wants_subagents"] and set(writer["subagents"]) == {
        "scout", "critic", "verifier"}
    assert "писатель" in writer["system_prompt"]
    for role in ("official_sources", "corporate_contour", "secondary_sources",
                 "role_candidates", "candidate_contacts"):
        spec = load_spec(HERE / "kimi_agent" / f"{role}.yaml")
        assert spec["lead_tools"] == ["LeadSearch", "LeadFetch", "LeadCrawl"], role
        assert not spec["wants_subagents"], role
        assert spec["system_prompt"].strip(), role
    onepager = load_spec(HERE / "kimi_agent" / "onepager.yaml")
    assert onepager["lead_tools"] == [] and not onepager["wants_subagents"]

    call = _convert_call(SimpleNamespace(
        id="c1", name=_MCP_PREFIX + "LeadFetch", input={"url": "https://example.test"}))
    assert call.function.name == "LeadFetch"
    assert json.loads(call.function.arguments) == {"url": "https://example.test"}
    task = _convert_call(SimpleNamespace(
        id="c2", name="Agent", input={"subagent_type": "scout", "prompt": "x"}))
    assert task.function.name == "Task"
    assert json.loads(task.function.arguments)["subagent_name"] == "scout"
    assert _result_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"
    print("selftest passed: adapter specs + kimi-shaped message mapping")
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
