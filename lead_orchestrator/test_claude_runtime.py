# -*- coding: utf-8 -*-
"""Офлайн-контракт ветки claude-sdk: генератор по умолчанию на Claude Agent SDK.

Проверяет БЕЗ сети и LLM: дефолты runtime, диспатч текущего pipeline на model='claude',
резолв моделей стадий, готовность файлов агентных подпроцессов и Kimi-совместимый
адаптер (спеки yaml/md + маппинг сообщений). Kimi-режим этих же файлов покрывает
test_kimi_only.py (ORQ_KIMI_ONLY=1).
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
KIMI = HERE.parent / "lead_orchestrator_kimi"
sys.path.insert(0, str(HERE))

# Чистые дефолты ветки: без наследованных переключателей из консоли/лаунчера.
for name in ("ORQ_KIMI_ONLY", "ORQ_LLM_RUNTIME", "DR_LLM_PROVIDER",
             "ORQ_WRITER_MODEL", "ORQ_ENRICH_MODEL", "ORQ_ONEPAGER_MODEL",
             "ORQ_CONTROLLER_MODEL"):
    os.environ.pop(name, None)

import kimi_config as KC  # noqa: E402


def _load_adapter():
    spec = importlib.util.spec_from_file_location(
        "claude_kimi_adapter_under_test", KIMI / "claude_kimi_adapter.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> int:
    # --- дефолты ветки: runtime claude, kimi — только явным переключателем ---
    assert KC.kimi_only() is False
    assert KC.runtime() == "claude"
    assert KC.default_model_flag() == "claude"
    KC.ensure_env()
    assert os.environ["DR_LLM_PROVIDER"] == "claude"
    assert os.environ["ORQ_LLM_RUNTIME"] == "claude"
    assert KC.claude_model("writer") == "opus"
    assert KC.claude_model("enrich") == "sonnet"
    assert KC.claude_model("onepager") == "opus"
    assert KC.claude_model("controller") == "sonnet"
    os.environ["ORQ_WRITER_MODEL"] = "sonnet"
    try:
        assert KC.claude_model("writer") == "sonnet"
    finally:
        os.environ.pop("ORQ_WRITER_MODEL", None)

    # claude-runtime не требует ключа Kimi ни в ensure_env, ни в child_env.
    saved = {name: os.environ.pop(name, None) for name in ("KIMI_API_KEY", "GPLLM_API_KEY")}
    try:
        KC.ensure_env(require_key=True)
        child = KC.child_env(require_key=True)
        assert child["ORQ_LLM_RUNTIME"] == "claude"
        assert child["ORQ_KIMI_ONLY"] == "0"
        assert child["DR_LLM_PROVIDER"] == "claude"
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value

    # --- parent-мост писателя: интерпретатор, модели, файлы, окружение агента ---
    import writer_kimi as WK
    assert WK.is_claude("claude") and WK.is_claude("Claude-Writer")
    assert not WK.is_claude("kimi") and not WK.is_claude("opus")
    assert WK.kimi_model("claude") == "opus"
    assert WK._enrich_model("claude") == "sonnet"
    assert WK._agent_python() == pathlib.Path(sys.executable)
    assert WK._agent_files_missing(WK.KIMI_AGENT_CLI) == []
    assert WK._agent_files_missing(WK.KIMI_RESEARCH_CLI) == []
    assert WK._agent_enabled() is True          # HTTP-фолбэка у claude нет
    env = WK._agent_env("opus", share_dir="X")
    assert env["ORQ_LLM_RUNTIME"] == "claude"
    assert env["ORQ_RESEARCH_TOOL"].endswith("kimi_research_cli.py")
    for secret in ("YANDEX_DISK_TOKEN", "DADATA_TOKEN", "CHECKO_TOKEN", "OFDATA_API_KEY"):
        assert secret not in env

    # --- диспатч orchestrator: model='claude' идёт в ТЕКУЩИЙ pipeline, не в legacy ---
    import orchestrator as ORQ
    original = ORQ._research_one_kimi

    async def fake_current_pipeline(*args, **kwargs):
        return 0.0, "current-pipeline"

    ORQ._research_one_kimi = fake_current_pipeline
    try:
        assert asyncio.run(ORQ._research_one(
            {}, 0, "process.docx", "roles.docx", "claude", person_enrich=False
        )) == (0.0, "current-pipeline")
    finally:
        ORQ._research_one_kimi = original
    ok_pp, why_pp = ORQ._presentation_prereqs()
    assert ok_pp, f"one-pager claude-runtime должен быть готов: {why_pp}"

    # --- NL-контроллер: план собирает --model claude, SDK не грузится статически ---
    import orchestrator_agent as OA
    plan = OA._normalise_plan({
        "action": "run", "industries": "mining", "count_per_industry": 5, "dry_run": True,
    })
    cmd, error = OA._build_cmd(plan)
    assert error is None and cmd is not None
    assert cmd[cmd.index("--model") + 1] == "claude"

    # --- адаптер: спеки тех же yaml/md и kimi-форма сообщений ---
    adapter = _load_adapter()
    assert adapter.selftest() == 0
    writer_spec = adapter.load_spec(KIMI / "kimi_agent" / "writer.yaml")
    assert set(writer_spec["subagents"]) == {"scout", "critic", "verifier"}

    # --- селфтесты агентных скриптов в claude-режиме (основной python) ---
    agent_env = dict(os.environ, ORQ_LLM_RUNTIME="claude", PYTHONIOENCODING="utf-8")
    probe = subprocess.run(
        [sys.executable, str(KIMI / "writer_kimi_agent.py"), "--selftest"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(KIMI), env=agent_env, timeout=120)
    assert probe.returncode == 0, probe.stderr or probe.stdout

    # Веб отдаёт оба runtime, дефолт — claude.
    web_runs = (HERE.parent / "web" / "api" / "runs.py").read_text(encoding="utf-8")
    assert '_default_model_flag()' in web_runs and '"--model", model' in web_runs

    print("test_claude_runtime: OK — генератор ветки по умолчанию на Claude Agent SDK "
          f"(writer={KC.claude_model('writer')}, roles={KC.claude_model('enrich')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
