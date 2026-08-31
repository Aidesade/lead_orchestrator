# -*- coding: utf-8 -*-
"""Пути и настройки веб-слоя.

Резолв путей ПОВТОРЯЕТ orchestrator.py (_tmp_root/_work_base, :524-544, и выбор
папки лидов, :789-795) — веб обязан смотреть ровно в те же папки, что и CLI,
иначе таблица лидов и кэш находок «не увидят» результаты прогона.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent          # <repo>/web
REPO_ROOT = Path(os.environ.get("ORQ_REPO_ROOT") or WEB_DIR.parent)
# Код пайплайна живёт в lead_orchestrator/app (рядом с ним tests/ и bootstrap/).
ORCH_DIR = REPO_ROOT / "lead_orchestrator" / "app"
ORCH_PY = ORCH_DIR / "orchestrator.py"
RUSPROFILE_PY = ORCH_DIR / "rusprofile_session.py"

# Интерпретатор для подпроцесса оркестратора. По умолчанию — тот же, что крутит API
# (значит, API надо запускать из venv проекта, где стоят зависимости оркестратора).
PYTHON = os.environ.get("ORQ_PYTHON") or sys.executable


def _data_root() -> Path:
    """ORQ_DATA_ROOT -> D:\\ -> %TEMP% (как в orchestrator._work_base)."""
    v = (os.environ.get("ORQ_DATA_ROOT") or "").strip()
    if v:
        return Path(v)
    return Path("D:\\") if os.path.isdir("D:\\") else Path(tempfile.gettempdir())


# Пакет оркестратора должен быть импортируем из веба: отсюда берутся ЧИСТЫЕ функции
# (disk_organize — правила имён на Диске, writer_kimi — имя модели). Тяжёлые модули
# (source_rusprofile с undetected_chromedriver) сюда НЕ импортируем — только подпроцессом.
if str(ORCH_DIR) not in sys.path:
    sys.path.insert(0, str(ORCH_DIR))

DATA_ROOT = _data_root()
TMP_DIR = DATA_ROOT / "orq_tmp"           # логи прогонов run_<ts>.log
CACHE_DIR = DATA_ROOT / "orq_cache"       # findings_<ИНН>.md
OUTBOX_DIR = DATA_ROOT / "orq_outbox"     # недолитые деливераблы (режим Диска)
# Хранилище готовых деливераблов сайта (режим ORQ_STORE=local): deliverables/<ключ>/<файлы>.
# Тот же путь пишет пайплайн (orchestrator._store_root); отсюда веб отдаёт файлы на скачивание.
DELIVERABLES_DIR = Path(os.environ.get("ORQ_DELIVERABLES_DIR") or (DATA_ROOT / "deliverables"))


def _leads_dir() -> Path:
    v = (os.environ.get("ORQ_LEADS_DIR") or "").strip()
    if v:
        return Path(v)
    return Path(r"D:\лиды") if os.name == "nt" else Path("/data/leads")


LEADS_DIR = _leads_dir()

# Состояние веба (журнал прогонов). Отдельно от данных пайплайна: снести можно безболезненно.
JOBS_DIR = Path(os.environ.get("ORQ_WEB_JOBS_DIR") or (DATA_ROOT / "web_jobs"))

DISK_BASE = os.environ.get("ORQ_DISK_BASE") or "disk:/Лиды"

# Фронт (собранный Vite) — если лежит, отдаём его же процессом FastAPI.
UI_DIST = WEB_DIR / "ui" / "dist"

# CORS для dev-режима (vite на 5173 ходит в API на 8000).
DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


def ensure_dirs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    DELIVERABLES_DIR.mkdir(parents=True, exist_ok=True)
