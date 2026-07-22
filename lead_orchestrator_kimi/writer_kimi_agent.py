# -*- coding: utf-8 -*-
"""Изолированный Kimi Agent-писатель одного пресейл-документа.

Запускается только python из .venv_kimi. Получает промпт/схему через JSON-файл,
вызывает нативные Kimi Task-субагенты (scout/critic/verifier), возвращает JSON
родительскому процессу. Рендер .docx остаётся в основном venv.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone


HERE = pathlib.Path(__file__).resolve().parent
AGENT_FILE = HERE / "kimi_agent" / "writer.yaml"


def _safe_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*]+', "_", str(value or "").strip())
    return re.sub(r"\s+", " ", value).strip(" .")[:100] or "company"


def _tool_log_path(request: dict) -> pathlib.Path | None:
    root = os.environ.get("KIMI_TOOL_LOG_DIR", "").strip()
    if not root:
        return None
    path = pathlib.Path(root)
    path.mkdir(parents=True, exist_ok=True)
    company = _safe_filename(request.get("company") or request.get("inn"))
    label = _safe_filename(request.get("label") or "writer")
    return path / f"{company}__{label}__{os.getpid()}.jsonl"


def _append_tool_log(path: pathlib.Path | None, event: dict) -> None:
    if path is None:
        return
    event = {"timestamp": datetime.now(timezone.utc).isoformat(), **event}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


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
        decoder = json.JSONDecoder()
        value, _ = decoder.raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("финальный JSON должен быть объектом")
    return value


def _task_role(call) -> str:
    try:
        args = json.loads(call.function.arguments or "{}")
    except (json.JSONDecodeError, TypeError):
        return "?"
    return str(args.get("subagent_name") or "?")


def _tool_subject(call) -> str:
    try:
        args = json.loads(call.function.arguments or "{}")
    except (json.JSONDecodeError, TypeError):
        return "?"
    for key in ("query", "url", "domain"):
        value = str(args.get(key) or "").strip()
        if value:
            return " ".join(value.split())[:180]
    return "?"


async def run_agent(request: dict) -> dict:
    try:
        from kimi_agent_sdk import prompt
    except ImportError as exc:
        raise RuntimeError("writer_kimi_agent надо запускать python из .venv_kimi") from exc

    model = request.get("model") or os.environ.get("KIMI_MODEL_NAME")
    full_input = (
        f"{request['system']}\n\n"
        "===== РЕЖИМ KIMI AGENT =====\n"
        "Инструкции выше могли называть Claude-инструмент Agent и save_*_docx. Здесь их нет. "
        "Вместо Agent используй Task с ролями scout/critic/verifier. Вместо save_* финальным "
        "деливераблом является один JSON-объект: его отрендерит родительский процесс. "
        "Сам решай, сколько scout нужно и как разделить направления; независимые задачи запускай "
        "параллельно. До финала обязательно вызови critic по черновому JSON. Для спорных фактов "
        "и каждого заявленного ЛПР вызывай verifier.\n\n"
        "===== ЗАДАНИЕ И ДАННЫЕ =====\n\n"
        f"{request['user']}"
    )

    counts = {"scout": 0, "critic": 0, "verifier": 0}
    web_counts = {"LeadSearch": 0, "LeadFetch": 0, "LeadCrawl": 0}
    parallel_scout = False
    final_text = ""
    max_steps_raw = os.environ.get("KIMI_WRITER_MAX_STEPS", "").strip()
    max_steps_value = int(max_steps_raw or "0")
    max_steps = max_steps_value if max_steps_value > 0 else None
    tool_log = _tool_log_path(request)

    async for message in prompt(
        full_input,
        model=model,
        thinking=os.environ.get("KIMI_WRITER_THINKING", "").lower() in ("1", "true", "yes"),
        yolo=True,
        final_message_only=False,
        agent_file=AGENT_FILE,
        max_steps_per_turn=max_steps,
    ):
        roles_in_step = []
        for call in message.tool_calls or []:
            tool_name = call.function.name
            _append_tool_log(tool_log, {
                "event": "tool_call", "company": request.get("company") or "",
                "inn": request.get("inn") or "", "document": request.get("label") or "",
                "tool": tool_name, "call_id": getattr(call, "id", None),
                "arguments": call.function.arguments or "{}",
            })
            if tool_name in web_counts:
                web_counts[tool_name] += 1
                print(f"[kimi-agent] web -> {tool_name}: {_tool_subject(call)}", flush=True)
                continue
            if tool_name != "Task":
                continue
            role = _task_role(call)
            roles_in_step.append(role)
            if role in counts:
                counts[role] += 1
            print(f"[kimi-agent] Task -> {role}", flush=True)
        if roles_in_step.count("scout") >= 2:
            parallel_scout = True
        text = message.extract_text().strip()
        if message.role == "tool":
            _append_tool_log(tool_log, {
                "event": "tool_result", "company": request.get("company") or "",
                "inn": request.get("inn") or "", "document": request.get("label") or "",
                "call_id": getattr(message, "tool_call_id", None), "result": text,
            })
        if message.role == "assistant" and text and not message.tool_calls:
            final_text = text

    if counts["critic"] < 1:
        raise RuntimeError("Kimi-писатель не вызвал critic перед финалом")
    if not final_text:
        raise RuntimeError("Kimi-писатель не вернул финальный текст")

    return {
        "payload": _extract_json(final_text),
        "subagents": counts,
        "web_tools": web_counts,
        "parallel_scout": parallel_scout,
        "model": model or "",
    }


def selftest() -> int:
    from kimi_cli.agentspec import load_agent_spec

    spec = load_agent_spec(AGENT_FILE)
    assert {
        "kimi_cli.tools.multiagent:Task",
        "leadgen_tools:LeadSearch",
        "leadgen_tools:LeadFetch",
        "leadgen_tools:LeadCrawl",
    } <= set(spec.tools)
    assert {"scout", "critic", "verifier"} <= set(spec.subagents)
    for role, sub in spec.subagents.items():
        child = load_agent_spec(sub.path)
        if role in ("scout", "verifier"):
            assert "leadgen_tools:LeadSearch" in child.tools
            assert "leadgen_tools:LeadFetch" in child.tools
        else:
            assert child.tools == []
    from leadgen_tools import LeadCrawl, LeadFetch, LeadSearch
    assert LeadSearch.name == "LeadSearch" and LeadFetch.name == "LeadFetch"
    assert LeadCrawl.name == "LeadCrawl"
    assert _extract_json('до ```json\n{"ok": true}\n``` после') == {"ok": True}
    print("selftest passed: writer + scout/critic/verifier specs")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Kimi Agent writer (один JSON-документ)")
    ap.add_argument("request", nargs="?", help="входной JSON")
    ap.add_argument("result", nargs="?", help="выходной JSON")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.request or not args.result:
        ap.error("нужны request и result")

    request = json.loads(pathlib.Path(args.request).read_text(encoding="utf-8"))
    result = asyncio.run(run_agent(request))
    pathlib.Path(args.result).write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8"
    )
    print(
        "[kimi-agent] готово: "
        + ", ".join(f"{k}={v}" for k, v in result["subagents"].items())
        + ("; scout parallel" if result["parallel_scout"] else "; scout sequential"),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
