# -*- coding: utf-8 -*-
"""Тихая загрузка локальных секретов из <repo>/env/.env.

Файл и вся папка env исключены из Git и Docker build context. Значения никогда
не печатаются; уже заданное окружение имеет приоритет, если override=False.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Код пайплайна лежит в lead_orchestrator/app, поэтому корень репозитория — на два
# уровня выше файла, а не на один.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = PROJECT_ROOT / "env" / ".env"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_project_env(path=None, *, override=False):
    """Загрузить простой dotenv-файл и вернуть только имена загруженных переменных."""
    configured = (os.environ.get("ORQ_ENV_FILE") or "").strip()
    env_path = Path(path or configured or DEFAULT_ENV_FILE)
    if not env_path.is_file():
        return ()

    loaded = []
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if not _NAME.fullmatch(name):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if override or name not in os.environ:
            os.environ[name] = value
            loaded.append(name)
    return tuple(loaded)
