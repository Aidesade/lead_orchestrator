# -*- coding: utf-8 -*-
"""Офлайн-контракт: штатный маршрут не может случайно уйти с Kimi K2.7."""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import sys


HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

for name in ("ORQ_KIMI_ONLY", "ORQ_LLM_RUNTIME", "DR_LLM_PROVIDER"):
    os.environ.pop(name, None)

import kimi_config as KC  # noqa: E402

CLEAN_DEFAULT_RUNTIME = KC.runtime()
CLEAN_DEFAULT_MODEL_FLAG = KC.default_model_flag()

os.environ["ORQ_KIMI_ONLY"] = "1"
os.environ.pop("DR_LLM_PROVIDER", None)

KC.ensure_env()

import deep_research_engine as DRE  # noqa: E402
import orchestrator as ORQ  # noqa: E402
import orchestrator_agent as OA  # noqa: E402
import writer_kimi as WK  # noqa: E402


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def main() -> int:
    assert CLEAN_DEFAULT_RUNTIME == "kimi"
    assert CLEAN_DEFAULT_MODEL_FLAG == "kimi"
    assert KC.kimi_only() is True
    assert KC.model_name() == "kimi-k2.7-code"
    saved_model = os.environ.get("KIMI_MODEL_NAME")
    os.environ["KIMI_MODEL_NAME"] = "kimi-other"
    try:
        try:
            KC.model_name()
        except RuntimeError:
            pass
        else:
            raise AssertionError("Kimi-only должен запрещать замену K2.7 другой моделью")
    finally:
        if saved_model is None:
            os.environ.pop("KIMI_MODEL_NAME", None)
        else:
            os.environ["KIMI_MODEL_NAME"] = saved_model
    assert os.environ["DR_LLM_PROVIDER"] == "kimi"
    assert DRE.DR_LLM_PROVIDER == "kimi"
    assert WK.kimi_model("kimi") == KC.model_name()
    assert WK.kimi_base_url() == KC.base_url()
    assert not any(name == "claude_agent_sdk" or name.startswith("claude_agent_sdk.")
                   for name in sys.modules)

    # Общие схемы/рендереры DOCX должны импортироваться без Claude SDK.
    import company_research_agent  # noqa: F401
    assert not any(name == "claude_agent_sdk" or name.startswith("claude_agent_sdk.")
                   for name in sys.modules)
    try:
        company_research_agent._claude_sdk()
    except RuntimeError:
        pass
    else:
        raise AssertionError("legacy Claude SDK не должен открываться в Kimi-only режиме")

    # И сам entrypoint Kimi должен вернуть управление до lazy legacy-импортов.
    original = ORQ._research_one_kimi

    async def fake_kimi(*args, **kwargs):
        return 0.0, "kimi"

    ORQ._research_one_kimi = fake_kimi
    try:
        assert asyncio.run(ORQ._research_one(
            {}, 0, "process.docx", "roles.docx", "kimi", person_enrich=False
        )) == (0.0, "kimi")
    finally:
        ORQ._research_one_kimi = original
    assert not any(name == "claude_agent_sdk" or name.startswith("claude_agent_sdk.")
                   for name in sys.modules)

    plan = OA._normalise_plan({
        "action": "run", "industries": "mining", "count_per_industry": 5,
        "dry_run": True,
    })
    cmd, error = OA._build_cmd(plan)
    assert error is None and cmd is not None
    assert cmd[cmd.index("--model") + 1] == "kimi"

    imports = _imports(HERE / "orchestrator_agent.py")
    assert not any(name == "anthropic" or name.startswith("claude_agent_sdk") for name in imports)

    orchestrator_source = (HERE / "orchestrator.py").read_text(encoding="utf-8")
    assert 'ap.add_argument("--model", default=KC.default_model_flag()' in orchestrator_source
    assert "if KC.kimi_only():" in orchestrator_source
    assert "return await _research_one_kimi(" in orchestrator_source

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "ORQ_KIMI_ONLY" in compose and "DR_LLM_PROVIDER: kimi" in compose
    assert "ANTHROPIC_API_KEY:" not in compose
    assert "env_file:" in compose and "./env/.env" in compose
    assert "KIMI_API_KEY:" not in compose
    assert "OFDATA_API_KEY:" not in compose

    launcher = (HERE / "run_kimi_orchestrator.cmd").read_text(encoding="ascii")
    assert 'if not defined ORQ_LLM_RUNTIME set "ORQ_LLM_RUNTIME=kimi"' in launcher
    assert 'set "LEAD_SOURCE=rusprofile"' in launcher
    assert 'set "RUSPROFILE_BROWSER=playwright"' in launcher
    assert "rusprofile_cookies.json" in launcher
    assert "from project_env import load_project_env" in launcher
    assert "r'%~dp0.'" in launcher  # %~dp0 ends with \ and is invalid as a raw string
    assert "orchestrator_agent.py" in launcher

    canonical = (HERE / "run_orchestrator.cmd").read_text(encoding="ascii")
    assert "run_kimi_orchestrator.cmd" in canonical
    web_runs = (ROOT / "web" / "api" / "runs.py").read_text(encoding="utf-8")
    assert 'return "kimi"' in web_runs

    print("test_kimi_only: OK — controller/research/writer/web runtime закреплены за Kimi K2.7")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
