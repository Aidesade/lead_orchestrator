# -*- coding: utf-8 -*-
"""Офлайн-контракт третьего runtime — GLM через корпоративный шлюз.

Проверяет БЕЗ сети и LLM: ORQ_LLM_RUNTIME=glm ведёт в ТОТ ЖЕ шлюзовой pipeline,
что kimi (OpenAI-совместимый путь + агентные стадии в .venv_kimi), но с ключом и
моделью из GLM_*. Дети получают значения прежним env-каналом KIMI_*, «--model glm»
не утекает на шлюз литералом, а ORQ_KIMI_ONLY=1 запирает glm так же, как claude.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
APP = HERE.parent / "app"            # код пайплайна
BOOT = HERE.parent / "bootstrap"     # лаунчеры
ROOT = HERE.parents[1]               # корень репозитория
sys.path.insert(0, str(APP))

# Чистый glm-runtime: без наследованных переключателей из консоли/лаунчера.
for name in ("ORQ_KIMI_ONLY", "ORQ_LLM_RUNTIME", "DR_LLM_PROVIDER",
             "GLM_API_KEY", "GLM_BASE_URL", "GLM_MODEL_NAME",
             "KIMI_API_KEY", "KIMI_BASE_URL", "KIMI_MODEL_NAME", "GPLLM_API_KEY"):
    os.environ.pop(name, None)
os.environ["ORQ_LLM_RUNTIME"] = "glm"

import kimi_config as KC  # noqa: E402


def main() -> int:
    # --- выбор runtime и резолв модели/ключа/endpoint ---
    assert KC.kimi_only() is False
    assert KC.runtime() == "glm"
    assert KC.default_model_flag() == "glm"
    assert KC.model_name() == KC.DEFAULT_GLM_MODEL == KC.glm_model_name()
    assert KC.base_url() == KC.DEFAULT_BASE_URL      # тот же шлюз, что у kimi

    # KIMI_MODEL_NAME НЕ подменяет модель glm: иначе glm молча поехал бы на K2.7.
    os.environ["KIMI_MODEL_NAME"] = "kimi-k2.7-code"
    assert KC.model_name() == KC.DEFAULT_GLM_MODEL
    os.environ["GLM_MODEL_NAME"] = "glm-4.6-gateway"
    assert KC.model_name() == "glm-4.6-gateway"
    os.environ["GLM_BASE_URL"] = "https://example.dtc.tatar/v1/"
    assert KC.base_url() == "https://example.dtc.tatar/v1"
    os.environ.pop("GLM_BASE_URL", None)

    # Ключ: GLM_API_KEY главнее, но общие ключи шлюза работают фолбэком (endpoint один).
    os.environ["GPLLM_API_KEY"] = "gp-key"
    assert KC.api_key() == "gp-key"
    os.environ["KIMI_API_KEY"] = "kimi-key"
    assert KC.api_key() == "kimi-key"
    os.environ["GLM_API_KEY"] = "glm-key"
    assert KC.api_key() == "glm-key"

    # --- ensure_env/child_env: дети получают glm-значения прежним каналом KIMI_* ---
    KC.ensure_env(require_key=True)
    assert os.environ["ORQ_LLM_RUNTIME"] == "glm"
    assert os.environ["DR_LLM_PROVIDER"] == "glm"
    assert os.environ["KIMI_MODEL_NAME"] == "glm-4.6-gateway"   # канал venv-агентов
    assert os.environ["GLM_MODEL_NAME"] == "glm-4.6-gateway"    # и явный резолв для детей
    child = KC.child_env(require_key=True)
    assert child["ORQ_LLM_RUNTIME"] == "glm"
    assert child["ORQ_KIMI_ONLY"] == "0"
    assert child["DR_LLM_PROVIDER"] == "glm"
    assert child["KIMI_API_KEY"] == "glm-key"
    assert child["KIMI_MODEL_NAME"] == "glm-4.6-gateway"
    assert child["GLM_API_KEY"] == "glm-key"

    # Без единого ключа шлюза glm-runtime обязан отказать — с внятными именами env.
    for name in ("GLM_API_KEY", "KIMI_API_KEY", "GPLLM_API_KEY"):
        os.environ.pop(name, None)
    try:
        KC.ensure_env(require_key=True)
        raise AssertionError("glm без ключа шлюза должен падать")
    except RuntimeError as exc:
        assert "GLM_API_KEY" in str(exc)
    os.environ["GLM_API_KEY"] = "glm-key"

    # --- ORQ_KIMI_ONLY=1 запирает glm так же, как claude ---
    os.environ["ORQ_KIMI_ONLY"] = "1"
    try:
        assert KC.runtime() == "kimi"
        assert KC.default_model_flag() == "kimi"
        # Канал KIMI_MODEL_NAME ещё несёт glm-значение от ensure_env выше: kimi-only
        # обязан отказать ГРОМКО (fail-closed), а не тихо поехать на чужой модели.
        try:
            KC.model_name()
            raise AssertionError("kimi-only с glm-моделью в канале должен падать")
        except RuntimeError as exc:
            assert KC.DEFAULT_MODEL in str(exc)
        os.environ["KIMI_MODEL_NAME"] = KC.DEFAULT_MODEL   # чистый kimi-only запуск
        assert KC.model_name() == KC.DEFAULT_MODEL
    finally:
        os.environ.pop("ORQ_KIMI_ONLY", None)
    assert KC.runtime() == "glm"

    # --- parent-мост писателя: псевдоним, модель, интерпретатор venv Kimi ---
    import writer_kimi as WK
    assert WK.is_glm("glm") and WK.is_glm("GLM-Writer")
    assert not WK.is_glm("kimi") and not WK.is_glm("claude")
    assert not WK.is_kimi("glm") and not WK.is_claude("glm")
    # Псевдоним резолвится в реальный ID — литерал "glm" на шлюз не уезжает.
    assert WK.kimi_model("glm") == "glm-4.6-gateway"
    assert WK._enrich_model("glm") == "glm-4.6-gateway"
    assert WK._agent_python() == WK.KIMI_PY          # агентные стадии — в .venv_kimi
    assert WK._agent_files_missing(WK.KIMI_AGENT_CLI) == []
    env = WK._agent_env("glm-4.6-gateway", share_dir="X")
    assert env["ORQ_LLM_RUNTIME"] == "glm"
    assert env["KIMI_MODEL_NAME"] == "glm-4.6-gateway"
    for secret in ("YANDEX_DISK_TOKEN", "DADATA_TOKEN", "CHECKO_TOKEN", "OFDATA_API_KEY"):
        assert secret not in env

    # --- диспатч orchestrator: model='glm' идёт в ТЕКУЩИЙ pipeline, не в legacy ---
    import orchestrator as ORQ
    original = ORQ._research_one_kimi

    async def fake_current_pipeline(*args, **kwargs):
        return 0.0, "current-pipeline"

    ORQ._research_one_kimi = fake_current_pipeline
    try:
        assert asyncio.run(ORQ._research_one(
            {}, 0, "process.docx", "roles.docx", "glm", person_enrich=False
        )) == (0.0, "current-pipeline")
    finally:
        ORQ._research_one_kimi = original
    ok_pp, why_pp = ORQ._presentation_prereqs()
    assert ok_pp, f"one-pager glm-runtime должен быть готов (venv Kimi + ключ): {why_pp}"

    # --model glm нормализуется в runtime glm и своего провайдера движка.
    orq_source = (APP / "orchestrator.py").read_text(encoding="utf-8")
    assert '"glm" if _model_flag.startswith("glm")' in orq_source
    assert "WK.is_glm(model)" in orq_source

    # --- NL-контроллер: план собирает --model glm, метка честная ---
    import orchestrator_agent as OA
    plan = OA._normalise_plan({
        "action": "run", "industries": "mining", "count_per_industry": 5, "dry_run": True,
    })
    cmd, error = OA._build_cmd(plan)
    assert error is None and cmd is not None
    assert cmd[cmd.index("--model") + 1] == "glm"
    assert OA._runtime_label().startswith("GLM ")

    # --- движок: провайдер glm идёт OpenAI-путём шлюза, а не в claude-ветку ---
    dre_source = (APP / "deep_research_engine.py").read_text(encoding="utf-8")
    assert 'DR_LLM_PROVIDER in ("kimi", "glm")' in dre_source

    # --- письмо outreach: glm честно отправляет к claude-runtime, а не падает молча ---
    letter_source = (APP / "outreach_letter.py").read_text(encoding="utf-8")
    assert "для kimi/glm запусти" in letter_source

    # --- веб: glm принимается и отдаётся списком моделей ---
    web_runs = (ROOT / "web" / "api" / "runs.py").read_text(encoding="utf-8")
    assert '("kimi", "glm", "claude")' in web_runs
    web_main = (ROOT / "web" / "api" / "main.py").read_text(encoding="utf-8")
    assert '"id": "glm"' in web_main

    # --- лаунчер: glm проходит тот же key-гейт шлюза, что kimi ---
    launcher = (BOOT / "run_kimi_orchestrator.cmd").read_text(encoding="ascii")
    assert 'if /I "%ORQ_LLM_RUNTIME%"=="glm" goto gateway_key_check' in launcher
    assert 'set "DR_LLM_PROVIDER=glm"' in launcher

    # --- build-gate: этот тест обязан гоняться при сборке образа ---
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "test_glm_runtime.py" in dockerfile

    print("test_glm_runtime: OK — glm-runtime шлюза подключён "
          f"(модель={KC.glm_model_name()}, канал детей — KIMI_*)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
