# -*- coding: utf-8 -*-
r"""NL-обёртка полной цепочки лидогенерации (runtime claude|kimi).

Запрос на русском языке -> модель возвращает строгий JSON-план -> детерминированный
Python проверяет отрасли и объём -> subprocess запускает orchestrator.py. Runtime
выбирает kimi_config.runtime(): claude (штат ветки claude-sdk, Claude Agent SDK,
авторизация логином Claude Code) либо kimi при ORQ_KIMI_ONLY=1 (KIMI_API_KEY с
fallback GPLLM_API_KEY, KIMI_BASE_URL, KIMI_MODEL_NAME). Claude SDK грузится ТОЛЬКО
лениво через importlib в claude-runtime — kimi-only офлайн-контракт видит модуль
без статических импортов Anthropic.

Запуск:
  py orchestrator_agent.py
  py orchestrator_agent.py "собери по 10 компаний в нефтегазе, dry-run"
  py orchestrator_agent.py --selftest
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import warnings

warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)

from project_env import load_project_env  # noqa: E402

load_project_env()

import kimi_config as KC  # noqa: E402
import source_rusprofile as RP  # noqa: E402


ORCH = os.path.join(SCRIPTS, "orchestrator.py")
VALID = sorted(RP.INDUSTRY)
INDUSTRY_HINT = "; ".join(f"{k} — {RP.INDUSTRY[k]['label']}" for k in VALID)
COUNT_CHOICES = (5, 10, 200)
DEFAULT_PER_INDUSTRY = 200
BIG_RUN_COMPANIES = 50


SYSTEM_PROMPT = """\
Ты — контроллер полного B2B lead-gen pipeline. Твоя единственная задача —
преобразовать запрос пользователя в ОДИН JSON-план. Сам pipeline запускает Python после
проверки плана; не пиши команды, не имитируй запуск и не добавляй текст вне JSON.

Pipeline делает: сбор компаний по отрасли -> deep research -> два DOCX -> one-pager PDF.
Модельные стадии закреплены конфигурацией запуска; поля выбора модели в плане НЕТ.

Доступные отрасли (верни только ключи слева):
__INDUSTRIES__

Правила:
- industries — ключи через запятую. Подбери по смыслу русской формулировки.
- Если дан готовый leads.json, положи путь в leads_json, industries оставь пустым.
- count_per_industry — число компаний НА КАЖДУЮ отрасль. Если пользователь сказал
  «30 по трём отраслям», верни 10. Если число не названо — null (Python предложит 200).
- region передавай как есть. Исключение: «кроме Москвы» -> «НЕ Москва».
- min_revenue в рублях: «2 млрд» -> 2000000000. Если не названо — null.
- dry_run=true только по явной просьбе. В dry-run тяжёлые LLM-стадии не вызываются.
- one-pager включён по умолчанию; no_presentation=true только по явной просьбе.
- workers обычно 2, при просьбе экономить RAM — 1.
- action=clarify используй только если нельзя определить ни отрасль, ни путь JSON.

Верни объект ровно этого вида:
{
  "action": "run|clarify",
  "message": "короткий вопрос при clarify, иначе пустая строка",
  "industries": "mining,oil28 или пустая строка",
  "leads_json": "путь или пустая строка",
  "count_per_industry": 10,
  "min_revenue": 1000000000,
  "region": "",
  "show_browser": false,
  "dry_run": false,
  "no_upload": false,
  "no_presentation": false,
  "workers": 2
}
""".replace("__INDUSTRIES__", INDUSTRY_HINT)


def parse_industries(raw) -> list[str]:
    """Оставить только реальные ключи карты отраслей, без дублей."""
    values = raw if isinstance(raw, list) else str(raw or "").replace(";", ",").split(",")
    out: list[str] = []
    for value in values:
        key = str(value).strip().lower()
        if key in RP.INDUSTRY and key not in out:
            out.append(key)
    return out


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on", "да")


def _positive_int(value, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.I | re.S)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise ValueError("Kimi не вернул JSON-план") from None
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("JSON-план Kimi должен быть объектом")
    return value


def _normalise_plan(raw: dict) -> dict:
    """Недоверенный ответ модели -> узкий безопасный контракт запуска."""
    industries = parse_industries(raw.get("industries"))
    leads_json = str(raw.get("leads_json") or "").strip().strip('"')
    action = str(raw.get("action") or "run").strip().lower()
    if action not in ("run", "clarify"):
        action = "clarify"
    if industries and leads_json:
        action = "clarify"
        message = "Укажи либо отрасли для нового сбора, либо готовый leads.json — не оба сразу."
    else:
        message = str(raw.get("message") or "").strip()
    if action == "run" and not industries and not leads_json:
        action = "clarify"
        message = message or "Какую отрасль собрать или какой путь к leads.json использовать?"

    revenue = raw.get("min_revenue")
    try:
        revenue = float(revenue) if revenue not in (None, "") else None
    except (TypeError, ValueError):
        revenue = None
    count = raw.get("count_per_industry")
    count = None if count in (None, "") else _positive_int(count, DEFAULT_PER_INDUSTRY)
    workers = min(2, _positive_int(raw.get("workers"), 2))
    return {
        "action": action,
        "message": message,
        "industries": ",".join(industries),
        "leads_json": leads_json,
        "count_per_industry": count,
        "min_revenue": revenue,
        "region": str(raw.get("region") or "").strip(),
        "show_browser": _bool(raw.get("show_browser")),
        "dry_run": _bool(raw.get("dry_run")),
        "no_upload": _bool(raw.get("no_upload")),
        "no_presentation": _bool(raw.get("no_presentation")),
        "workers": workers,
    }


async def _claude_plan(prompt: str, history: list[dict] | None = None) -> tuple[dict, str]:
    """Один короткий вызов Claude Agent SDK: NL -> JSON-план. Без инструментов.

    importlib вместо статического импорта — осознанно: kimi-only офлайн-контракт
    (test_kimi_only) проверяет, что модуль не тянет claude_agent_sdk."""
    import importlib

    sdk = importlib.import_module("claude_agent_sdk")
    convo = "".join(
        f"{'Пользователь' if row.get('role') == 'user' else 'Прошлый план'}: {row.get('content')}\n"
        for row in (history or [])[-6:])
    full = (f"Предыдущий диалог:\n{convo}\n" if convo else "") + f"Запрос: {prompt}"
    options = sdk.ClaudeAgentOptions(
        model=KC.claude_model("controller"), system_prompt=SYSTEM_PROMPT,
        max_turns=1, allowed_tools=[],
        disallowed_tools=["Bash", "Edit", "Write", "NotebookEdit", "WebSearch", "WebFetch"],
        permission_mode="bypassPermissions", setting_sources=[])
    text = ""
    async for message in sdk.query(prompt=full, options=options):
        if isinstance(message, sdk.AssistantMessage):
            for block in message.content:
                if isinstance(block, sdk.TextBlock):
                    text += block.text
        elif isinstance(message, sdk.ResultMessage):
            if message.is_error:
                raise RuntimeError(f"Claude controller завершился ошибкой: {message.subtype}")
            if message.result:
                text = message.result
    return _normalise_plan(_extract_json(text)), text


async def _kimi_plan(prompt: str, history: list[dict] | None = None) -> tuple[dict, str]:
    """Один короткий вызов Kimi K2.7: NL -> JSON-план. Без tool calls и shell."""
    KC.ensure_env(require_key=True)
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url=KC.base_url(), api_key=KC.api_key(), timeout=180, max_retries=1)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend((history or [])[-6:])
    messages.append({"role": "user", "content": prompt})
    last_error: Exception | None = None
    try:
        for structured in (True, False):
            kwargs = {
                "model": KC.model_name(), "messages": messages,
                "max_tokens": 1800, "temperature": 0.0,
            }
            if structured:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                response = await client.chat.completions.create(**kwargs)
                text = response.choices[0].message.content or ""
                return _normalise_plan(_extract_json(text)), text
            except Exception as exc:  # шлюз может не поддержать response_format
                last_error = exc
                if not structured:
                    raise
    finally:
        await client.close()
    raise RuntimeError(f"Kimi controller не вернул план: {last_error}")


async def _plan(prompt: str, history: list[dict] | None = None) -> tuple[dict, str]:
    """NL -> проверенный JSON-план активным runtime."""
    if KC.runtime() == "claude":
        return await _claude_plan(prompt, history)
    return await _kimi_plan(prompt, history)


def per_industry(plan: dict) -> int:
    value = plan.get("count_per_industry")
    return _positive_int(value, DEFAULT_PER_INDUSTRY)


def _build_cmd(plan: dict) -> tuple[list[str] | None, str | None]:
    industries = parse_industries(plan.get("industries"))
    leads_json = str(plan.get("leads_json") or "").strip()
    if industries and leads_json:
        return None, "Нельзя одновременно собирать отрасли и читать готовый leads.json."
    if not industries and not leads_json:
        return None, "Нужна отрасль или путь к готовому leads.json."

    cmd = [sys.executable, ORCH]
    if leads_json:
        cmd.append(leads_json)
    if industries:
        cmd += ["--industries", ",".join(industries),
                "--per-industry", str(per_industry(plan))]
    if plan.get("min_revenue"):
        cmd += ["--min-revenue", str(float(plan["min_revenue"]))]
    if str(plan.get("region") or "").strip():
        cmd += ["--region", str(plan["region"]).strip()]
    cmd += ["--model", KC.default_model_flag(),
            "--workers", str(min(2, _positive_int(plan.get("workers"), 2)))]
    if _bool(plan.get("show_browser")):
        cmd.append("--show-browser")
    if _bool(plan.get("dry_run")):
        cmd.append("--dry-run")
    if _bool(plan.get("no_upload")):
        cmd.append("--no-upload")
    if _bool(plan.get("no_presentation")):
        cmd.append("--no-presentation")
    return cmd, None


def _runtime_label() -> str:
    """Человекочитаемая метка активного runtime для консоли."""
    if KC.runtime() == "kimi":
        return f"Kimi {KC.model_name()}"
    return f"Claude Agent SDK ({KC.claude_model('controller')})"


def _box(title: str, lines: list[str], pad: int = 1) -> str:
    width = max([len(title) + 4] + [len(line) + pad * 2 for line in lines])
    out = ["┌" + ("─ " + title + " ").ljust(width, "─") + "┐"]
    out += ["│" + (" " * pad + line).ljust(width) + "│" for line in lines]
    out.append("└" + "─" * width + "┘")
    return "\n".join(out)


async def _confirm_volume(plan: dict) -> dict | None:
    industries = parse_industries(plan.get("industries"))
    if not industries:
        return plan
    proposed = per_industry(plan)
    if not sys.stdin.isatty():
        print(f"[объём] неинтерактивно: {proposed} на отрасль")
        plan["count_per_industry"] = proposed
        return plan

    while True:
        choices = sorted(set(COUNT_CHOICES) | {proposed})
        lines = [f"Отрасли: {', '.join(industries)}", "Режим: N на КАЖДУЮ отрасль", ""]
        for index, number in enumerate(choices, 1):
            total = number * len(industries)
            mark = "  ←" if number == proposed else ""
            mode = "dry-run" if plan.get("dry_run") else "боевой Kimi-прогон"
            lines.append(f"{index}) {number:>3} на отрасль = {total:>4} комп., {mode}{mark}")
        lines += ["0) отмена"]
        print("\n" + _box("Подтверди объём", lines))
        default = choices.index(proposed) + 1
        try:
            answer = (await asyncio.to_thread(input, f"Выбор [{default}]: ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if answer in ("0", "отмена", "n", "нет"):
            return None
        if not answer:
            chosen = proposed
        elif answer.isdigit() and 1 <= int(answer) <= len(choices):
            chosen = choices[int(answer) - 1]
        elif answer.isdigit():
            chosen = max(1, int(answer))
        else:
            print("Не понял выбор.")
            continue
        total = chosen * len(industries)
        if not plan.get("dry_run") and total > BIG_RUN_COMPANIES:
            try:
                confirm = (await asyncio.to_thread(
                    input, f"Боевой Kimi-прогон: {total} компаний. Продолжить? [y/N]: ")).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return None
            if confirm not in ("y", "yes", "д", "да"):
                continue
        plan["count_per_industry"] = chosen
        print(f"[объём] {chosen} × {len(industries)} = {total} компаний")
        return plan


async def _execute(plan: dict) -> int:
    confirmed = await _confirm_volume(dict(plan))
    if confirmed is None:
        print("Запуск отменён.")
        return 2
    cmd, error = _build_cmd(confirmed)
    if cmd is None:
        print(error)
        return 2
    try:
        env = KC.child_env(require_key=not bool(confirmed.get("dry_run")))
    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        return 5
    shown = "orchestrator.py " + subprocess.list2cmdline(cmd[2:])
    print(f"[controller] {_runtime_label()} -> {shown}")

    def run() -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", env=env)

    result = await asyncio.to_thread(run)
    if result.returncode:
        print(f"Оркестратор завершился с ошибкой rc={result.returncode}.\n"
              f"{(result.stderr or '').strip()[-1500:]}")
    return result.returncode


def _selftest() -> int:
    plan = _normalise_plan({
        "action": "run", "industries": ["mining"], "count_per_industry": 10,
        "dry_run": True, "workers": 9,
    })
    cmd, error = _build_cmd(plan)
    assert error is None and cmd is not None
    assert cmd[cmd.index("--model") + 1] == KC.default_model_flag()
    assert cmd[cmd.index("--workers") + 1] == "2"
    assert os.path.basename(cmd[1]).lower() == "orchestrator.py"
    assert KC.model_name().startswith("kimi-k2.7")
    # Claude SDK допустим только лениво через importlib: kimi-only контракт
    # (test_kimi_only) проверяет отсутствие статических импортов по AST.
    source = open(__file__, encoding="utf-8").read()
    forbidden = "claude" + "_agent_sdk"
    assert f"from {forbidden}" not in source
    assert f"\nimport {forbidden}" not in source
    print(f"selftest passed: NL controller -> {_runtime_label()}")
    return 0


async def main() -> int:
    if sys.argv[1:] == ["--selftest"]:
        return _selftest()
    KC.ensure_env(require_key=True)
    cli_prompt = " ".join(sys.argv[1:]).strip()
    if cli_prompt:
        print(f"[controller] разбираю запрос: {_runtime_label()} ...")
        plan, _ = await _plan(cli_prompt)
        if plan["action"] == "clarify":
            print(plan["message"])
            return 2
        return await _execute(plan)

    print(f"Lead Orchestrator ({_runtime_label()}). Пустая строка или 'exit' — выход.")
    history: list[dict] = []
    while True:
        try:
            prompt = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not prompt or prompt.lower() in ("exit", "quit", "выход"):
            break
        try:
            plan, raw = await _plan(prompt, history)
        except Exception as exc:
            print(f"Контроллер недоступен: {exc}")
            continue
        history += [{"role": "user", "content": prompt},
                    {"role": "assistant", "content": raw}]
        if plan["action"] == "clarify":
            print(plan["message"])
            continue
        rc = await _execute(plan)
        history.clear()
        print("Готово." if rc == 0 else f"Завершено с кодом {rc}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
