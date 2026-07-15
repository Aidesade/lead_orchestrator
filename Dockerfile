# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm

ARG DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    ORQ_DATA_ROOT=/data \
    ORQ_LEADS_DIR=/data/leads \
    RUSPROFILE_PROFILE_DIR=/data/rusprofile/profile \
    RUSPROFILE_COOKIES_FILE=/data/rusprofile/cookies.json \
    KIMI_DIR=/app/lead_orchestrator_kimi \
    KIMI_PY=/opt/kimi-venv/bin/python \
    PATH=/opt/venv/bin:$PATH

# ⚠️ ЗДЕСЬ БОЛЬШЕ НЕТ НИ GOOGLE CHROME, НИ XVFB — и это осознанно.
# Они стояли ради ОДНОГО: Фаза 1 скребла RusProfile через undetected-chromedriver, а тот
# требует НАСТОЯЩИЙ Chrome в headed-режиме (отсюда и Xvfb — виртуальный экран под него).
# Источник лидов заменён на Checko API (lead_orchestrator/source_checko.py): обычные HTTPS-
# запросы — ни браузера, ни Cloudflare, ни кук, ни зависимости от репутации IP. Вместе с ними
# из образа ушло ~1,5 ГБ и главный операционный риск деплоя.
#
# Chromium в образе ОСТАЁТСЯ — но headless и через Playwright: им краулит Crawl4AI в движке
# дипресёрча и рендерит PDF стадия one-pager. Headless-браузеру виртуальный экран не нужен,
# поэтому xvfb-run убран и из ENTRYPOINT.
#
# ⚠️ СЛЕДСТВИЕ: source_rusprofile в КОНТЕЙНЕРЕ НЕРАБОТОСПОСОБЕН (нет бинаря Chrome). Пакет
# undetected-chromedriver в requirements оставлен намеренно — он импортируется и без браузера,
# и локальный RusProfile-путь на Windows продолжает работать. В Docker источник — Checko.
#
# Шрифты нужны chromium'у: без них кириллица в PDF one-pager рендерится квадратами.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl git procps fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY lead_orchestrator/requirements.txt /tmp/requirements.txt

# pywin32 неприменим в Linux. Остальной lock устанавливается в основном venv.
RUN python -m venv /opt/venv \
    && python - <<'PY'
from pathlib import Path
src = Path('/tmp/requirements.txt').read_text(encoding='utf-8')
lines = [line for line in src.splitlines() if not line.lstrip().lower().startswith('pywin32==')]
Path('/tmp/requirements-linux.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
PY
RUN pip install --upgrade pip setuptools wheel \
    && pip install -r /tmp/requirements-linux.txt \
    && python -m playwright install --with-deps chromium

# Kimi SDK изолирован из-за конфликта pydantic-core с claude-agent-sdk.
#
# Версии — из lead_orchestrator_kimi/requirements.txt: пара kimi-agent-sdk 0.0.5 + kimi-cli
# 1.12.0, которую SDK сам объявляет совместимой (kimi-cli>=1.12.0,<1.13.0). БЕЗ пина резолвер
# ставил сюда 1.12.0, а патч ниже накатывался вслепую под 1.4x -> KimiCLI.create() получал
# несуществующий skills_dirs= и стадия падала TypeError на ПЕРВОМ ЖЕ вызове модели. Сборка это
# не ловила (смоук дёргал только build_user_content), а оркестратор мягко деградирует — то есть
# образ молча никогда не делал one-pager. Не снимать пин.
#
# Патчи — тем же скриптом, что и при локальной установке (раньше патч №2 жил только здесь, и
# README с Dockerfile расходились). Скрипт сам сверяет сигнатуру установленного kimi-cli.
# Windows-only пакеты отсечены маркерами в самом requirements — sed-костыль не нужен.
COPY lead_orchestrator_kimi/requirements.txt /tmp/requirements-kimi.txt
COPY lead_orchestrator_kimi/patches/apply_patches.py /tmp/apply_patches.py
RUN python -m venv /opt/kimi-venv \
    && /opt/kimi-venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/kimi-venv/bin/pip install -r /tmp/requirements-kimi.txt \
    && /opt/kimi-venv/bin/python /tmp/apply_patches.py

COPY . /app
RUN mkdir -p /data/leads /data/rusprofile/profile /data/orq_tmp /data/orq_cache /data/orq_outbox \
    && python -m py_compile \
       lead_orchestrator/orchestrator.py \
       lead_orchestrator/rusprofile_session.py \
       lead_orchestrator/deep_research_engine.py \
       lead_orchestrator/company_research_agent.py \
       lead_orchestrator_kimi/onepager_kimi.py \
       lead_orchestrator_kimi/html_to_pdf.py \
    && DR_USE_LLM=0 python lead_orchestrator/test_deep_research.py \
    && /opt/kimi-venv/bin/python lead_orchestrator_kimi/patches/apply_patches.py --check \
    && /opt/kimi-venv/bin/python -c "import sys; sys.path.insert(0, '/app/lead_orchestrator_kimi'); import onepager_kimi; assert onepager_kimi.build_user_content('Тест')"

VOLUME ["/data"]
# Дефолты именно ДЛЯ КОНТЕЙНЕРА (в конце — чтобы не инвалидировать дорогие слои apt/pip):
#   LEAD_SOURCE=checko    — сбор ФАЗЫ 1 через Checko API (в образе нет Chrome под RusProfile);
#   DR_LLM_PROVIDER=kimi  — LLM-экстракт движка ресёрча через Kimi -> весь пайплайн без Anthropic.
# Локальный Windows-путь этим не затрагивается (там переменные не заданы, код по умолчанию
# rusprofile/claude). Перекрыть можно через -e / docker-compose.
ENV LEAD_SOURCE=checko \
    DR_LLM_PROVIDER=kimi
# Без xvfb-run: виртуальный экран был нужен только headed-Chrome под RusProfile. Оставшиеся
# браузеры (Crawl4AI, рендер PDF) работают headless и запускаются напрямую.
ENTRYPOINT ["python", "/app/lead_orchestrator/orchestrator.py"]
CMD ["--help"]
