# -*- coding: utf-8 -*-
"""Каталог отраслей (source_rusprofile.INDUSTRY) и здоровье сессии RusProfile.

INDUSTRY тянем ПОДПРОЦЕССОМ, а не импортом: source_rusprofile на верхнем уровне
делает `import undetected_chromedriver` и патчит uc.Chrome.__del__ — веб-процессу
это не нужно (и падает, если Chrome не там). Результат кэшируем: он статичен.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any, Dict, List, Optional

from . import config

_SNIPPET = (
    "import json, source_rusprofile as RP;"
    "print(json.dumps({k: {'label': v.get('label'), 'pain': v.get('pain'),"
    " 'offer': v.get('offer'), 'okved': v.get('okved')} for k, v in RP.INDUSTRY.items()},"
    " ensure_ascii=False))"
)

_cache: Optional[List[Dict[str, Any]]] = None
_rp_cache: Dict[str, Any] = {"at": 0.0, "value": None}


async def industries(force: bool = False) -> List[Dict[str, Any]]:
    global _cache
    if _cache is not None and not force:
        return _cache
    proc = await asyncio.create_subprocess_exec(
        str(config.PYTHON), "-c", _SNIPPET, cwd=str(config.ORCH_DIR),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError((err or b"").decode("utf-8", "replace")[:400]
                           or "не удалось прочитать source_rusprofile.INDUSTRY")
    data = json.loads(out.decode("utf-8", "replace"))
    _cache = [{"key": k, **v} for k, v in data.items()]
    return _cache


def rusprofile_cached() -> Optional[Dict[str, Any]]:
    """Что показала последняя проверка. Chrome не поднимает — можно звать на каждый рендер."""
    v = _rp_cache["value"]
    return {**v, "cached": True, "checked_at": _rp_cache["at"]} if v else None


async def rusprofile_status(max_age: float = 600.0,
                            busy: bool = False) -> Dict[str, Any]:
    """Живы ли платные контакты RusProfile.

    Это блокирующее условие для фазы 1: без cookie оркестратор падает сразу
    (SystemExit «нет cookie RusProfile», orchestrator.py:395-396), а протухший платный
    тариф роняет прогон уже после сбора. Логин — ручной и интерактивный
    (`py rusprofile_session.py --login`), из веба его не сделать: нужен живой Chrome.

    ВАЖНО: проверка поднимает НАСТОЯЩИЙ Chrome на том же --user-data-dir, что и сбор
    (rusprofile_session.py:68). Поэтому она (а) никогда не делается автоматически при
    заходе на страницу — только по кнопке, (б) запрещена во время прогона: иначе два
    Chrome дерутся за один профиль и роняют фазу 1.
    """
    if busy:
        return {"ok": None, "reason": "идёт прогон — проверка займёт тот же профиль Chrome",
                "busy": True}

    now = time.time()
    if _rp_cache["value"] is not None and now - _rp_cache["at"] < max_age:
        return {**_rp_cache["value"], "cached": True}

    if not config.RUSPROFILE_PY.exists():
        return {"ok": False, "reason": "нет rusprofile_session.py"}

    proc = await asyncio.create_subprocess_exec(
        str(config.PYTHON), str(config.RUSPROFILE_PY), "--check",
        cwd=str(config.ORCH_DIR),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        value = {"ok": False, "reason": "проверка не уложилась в 180 с", "output": ""}
    else:
        text = (out or b"").decode("utf-8", "replace").strip()
        value = {"ok": proc.returncode == 0, "output": text[-800:],
                 "hint": "py rusprofile_session.py --login" if proc.returncode else None}
    _rp_cache.update({"at": time.time(), "value": value})
    return value
