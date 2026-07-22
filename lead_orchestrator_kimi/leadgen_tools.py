# -*- coding: utf-8 -*-
"""Read-only тулы Kimi CLI для scout/verifier.

Сами веб-функции живут в основном проекте. Здесь только тонкий subprocess-мост:
Kimi-venv не импортирует основной venv и не смешивает несовместимые pydantic-core.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import signal
import subprocess
import sys
from typing import override

from kosong.tooling import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field


_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DEFAULT_TOOL = _ROOT / "lead_orchestrator" / "kimi_research_cli.py"
_TIMEOUT = float(os.environ.get("ORQ_KIMI_TOOL_TIMEOUT", "180"))


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Убить тул вместе с Crawl4AI/Playwright-потомками на Windows и Linux."""
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


async def _call(*args: str) -> ToolReturnValue:
    python = os.environ.get("ORQ_MAIN_PY") or ("py" if os.name == "nt" else "/opt/venv/bin/python")
    script = os.environ.get("ORQ_RESEARCH_TOOL") or str(_DEFAULT_TOOL)
    proc: asyncio.subprocess.Process | None = None
    try:
        spawn = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                 if os.name == "nt" else {"start_new_session": True})
        proc = await asyncio.create_subprocess_exec(
            python, script, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **spawn,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
    except TimeoutError:
        if proc is not None:
            await _kill_tree(proc)
        return ToolError(message=f"Ресёрч-инструмент превысил таймаут {_TIMEOUT:g} с",
                         brief="Research timeout")
    except asyncio.CancelledError:
        if proc is not None:
            await _kill_tree(proc)
        raise
    except Exception as exc:  # noqa: BLE001 — ошибка должна стать результатом тула
        return ToolError(message=f"Не удалось запустить ресёрч-инструмент: {exc}",
                         brief="Research tool failed")

    out = stdout.decode("utf-8", "replace").strip()
    err = stderr.decode("utf-8", "replace").strip()
    if proc.returncode:
        return ToolError(message=err or out or f"процесс завершился с кодом {proc.returncode}",
                         brief="Research tool failed")
    return ToolOk(output=out or "Инструмент не вернул текста.")


class SearchParams(BaseModel):
    query: str = Field(description="Поисковый запрос с названием компании/ИНН и искомым фактом")
    limit: int = Field(default=6, ge=1, le=12, description="Число результатов")


class LeadSearch(CallableTool2[SearchParams]):
    name: str = "LeadSearch"
    description: str = (
        "Поиск по открытому вебу через мульти-бэкенд движок лидгена. Возвращает URL, заголовки "
        "и сниппеты. Используй для обнаружения источников; факт подтверждай через LeadFetch."
    )
    params: type[SearchParams] = SearchParams

    @override
    async def __call__(self, params: SearchParams) -> ToolReturnValue:
        return await _call("search", "--query", params.query, "--limit", str(params.limit))


class FetchParams(BaseModel):
    url: str = Field(description="Полный http(s)-URL конкретной страницы")


class LeadFetch(CallableTool2[FetchParams]):
    name: str = "LeadFetch"
    description: str = (
        "Открыть конкретную страницу и вернуть очищенный текст с URL. Не подменяй открытие "
        "страницы поисковым сниппетом: существенные факты проверяй этим инструментом."
    )
    params: type[FetchParams] = FetchParams

    @override
    async def __call__(self, params: FetchParams) -> ToolReturnValue:
        return await _call("fetch", "--url", params.url)


class CrawlParams(BaseModel):
    domain: str = Field(description="Домен или URL сайта компании/ведомства")
    keywords: list[str] = Field(default_factory=list,
                                description="Слова направления для ранжирования обхода")
    max_pages: int = Field(default=8, ge=1, le=15, description="Максимум страниц")


class LeadCrawl(CallableTool2[CrawlParams]):
    name: str = "LeadCrawl"
    description: str = (
        "Безопасный обход разделов публичного сайта через SiteCrawler. По умолчанию используется "
        "только HTTP-режим с блокировкой private/loopback/link-local/multicast URL; browser crawl "
        "для model-controlled адресов запрещён. Используй, когда нужен раздел сайта целиком."
    )
    params: type[CrawlParams] = CrawlParams

    @override
    async def __call__(self, params: CrawlParams) -> ToolReturnValue:
        return await _call(
            "crawl", "--domain", params.domain,
            "--keywords", json.dumps(params.keywords, ensure_ascii=False),
            "--max-pages", str(params.max_pages),
        )


if __name__ == "__main__":
    print("leadgen_tools: import OK", file=sys.stderr)
