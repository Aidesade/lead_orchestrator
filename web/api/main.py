# -*- coding: utf-8 -*-
"""HTTP API лидген-оркестратора.

Запуск (из venv, где стоят зависимости lead_orchestrator):
    py -m uvicorn web.api.main:app --port 8000 --reload
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import catalog, config, leads as leads_store, regions as regions_ref, runs

config.ensure_dirs()

@asynccontextmanager
async def lifespan(_: FastAPI):
    runs.manager.load_history()          # журнал прошлых прогонов переживает рестарт сервера
    yield


app = FastAPI(title="Lead Orchestrator API", version="1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=config.DEV_ORIGINS,
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


# --- справочники -------------------------------------------------------------
@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "orchestrator": str(config.ORCH_PY),
        "orchestrator_found": config.ORCH_PY.exists(),
        "python": str(config.PYTHON),
        "leads_dir": str(config.LEADS_DIR),
        "data_root": str(config.DATA_ROOT),
        "disk_base": config.DISK_BASE,
        "active_run": runs.manager.active,
    }


@app.get("/api/industries")
async def industries() -> List[Dict[str, Any]]:
    try:
        return await catalog.industries()
    except Exception as e:                       # noqa: BLE001
        raise HTTPException(500, f"каталог отраслей недоступен: {e}") from e


@app.get("/api/models")
async def models() -> List[Dict[str, Any]]:
    """Модели ПИСАТЕЛЯ двух .docx (флаг --model).

    `kimi` — псевдоним: конкретное имя модели берётся из KIMI_WRITER_MODEL/KIMI_MODEL_NAME.
    У Kimi другой биллинг: цену за вызов шлюз наружу не отдаёт, поэтому вилку «$N–$2N»
    (она верна только для Claude-сессий) UI на нём не показывает.
    """
    import writer_kimi as _wk                  # лежит в пакете оркестратора (sys.path уже добавлен в leads.py)
    return [
        {"id": "kimi", "label": f"Kimi ({_wk.kimi_model('kimi')})",
         "billing": "provider", "default": True},
        {"id": "opus", "label": "Claude Opus (качество)", "billing": "claude"},
        {"id": "sonnet", "label": "Claude Sonnet (дешевле)", "billing": "claude"},
    ]


@app.get("/api/regions")
async def regions() -> List[Dict[str, Any]]:
    """Субъекты РФ для подсказок. Токен подобран так, чтобы матчиться подстрокой
    по выдаче RusProfile (фильтр по региону — клиентский)."""
    return regions_ref.all_regions()


@app.post("/api/rusprofile/check")
async def rusprofile_check() -> Dict[str, Any]:
    """Проверка сессии RusProfile — ТОЛЬКО по явной кнопке (поднимает живой Chrome)."""
    active = runs.manager.active
    busy = bool(active and runs.manager.runs[active].status == "running")
    return await catalog.rusprofile_status(busy=busy)


@app.get("/api/rusprofile")
async def rusprofile() -> Dict[str, Any]:
    """Последний известный результат, без запуска Chrome."""
    cached = catalog.rusprofile_cached()
    return cached or {"ok": None, "reason": "не проверялось"}


# --- лиды --------------------------------------------------------------------
@app.get("/api/leads")
async def list_leads(
    q: str = "", industry: str = "", region: str = "",
    min_revenue: float = 0.0, contacts: str = "",
    sort: str = "revenue", limit: int = Query(100, le=1000), offset: int = 0,
) -> Dict[str, Any]:
    return leads_store.query(q=q, industry=industry, region=region,
                             min_revenue=min_revenue, contacts=contacts,
                             sort=sort, limit=limit, offset=offset)


@app.get("/api/leads/facets")
async def lead_facets() -> Dict[str, Any]:
    return leads_store.facets()


@app.get("/api/leads/export.csv")
async def export_leads(
    q: str = "", industry: str = "", region: str = "",
    min_revenue: float = 0.0, contacts: str = "", sort: str = "revenue",
) -> PlainTextResponse:
    res = leads_store.query(q=q, industry=industry, region=region,
                            min_revenue=min_revenue, contacts=contacts,
                            sort=sort, limit=100000, offset=0)
    csv_text = leads_store.to_csv(res["items"])
    return PlainTextResponse(
        "﻿" + csv_text,                     # BOM — иначе Excel съест кириллицу как cp1251
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="leads.csv"'})


@app.get("/api/leads/{inn}")
async def lead_card(inn: str) -> Dict[str, Any]:
    card = leads_store.card(inn)
    if not card:
        raise HTTPException(404, "лид не найден")
    return card


# --- прогоны -----------------------------------------------------------------
class RunParams(BaseModel):
    industries: List[str] = Field(default_factory=list)
    leads_json: str = ""                       # ресёрч по готовому JSON (в UI не выведен)
    per_industry: int = 10
    min_revenue: float = 1e9
    regions: List[str] = Field(default_factory=list)
    exclude_regions: List[str] = Field(default_factory=list)
    model: str = "kimi"
    workers: int = 2
    out: str = ""
    base: str = ""
    show_browser: bool = False
    dry_run: bool = False
    no_upload: bool = False
    no_presentation: bool = False
    no_person_enrich: bool = False
    redo: bool = False


@app.get("/api/runs")
async def list_runs() -> List[Dict[str, Any]]:
    return runs.manager.list()


@app.post("/api/runs")
async def start_run(p: RunParams) -> Dict[str, Any]:
    try:
        run = await runs.manager.start(p.model_dump())
    except runs.RunConflict as e:
        # Параллельные прогоны дерутся за Chrome-профиль, файл лидов и outbox — 409, не гонка.
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(500, f"не запускается orchestrator.py: {e}") from e
    return run.snapshot()


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> Dict[str, Any]:
    run = runs.manager.get(run_id)
    if not run:
        raise HTTPException(404, "прогон не найден")
    return run.snapshot()


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> Dict[str, Any]:
    ok = await runs.manager.cancel(run_id)
    if not ok:
        raise HTTPException(409, "прогон не запущен или уже завершён")
    return {"ok": True}


@app.get("/api/runs/{run_id}/log")
async def run_log(run_id: str, tail: int = 4000) -> PlainTextResponse:
    """Сырой лог прогона (файл run_<ts>.log, который оркестратор пишет через _Tee)."""
    run = runs.manager.get(run_id)
    if not run:
        raise HTTPException(404, "прогон не найден")
    lines = [e["line"] for e in run.events if e.get("line")]
    if not lines:
        f = run.dir / "events.jsonl"
        if f.exists():
            lines = [json.loads(s).get("line", "")
                     for s in f.read_text(encoding="utf-8").splitlines() if s.strip()]
    return PlainTextResponse("\n".join(lines[-tail:]))


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, after: int = 0) -> StreamingResponse:
    """SSE: сначала догоняем пропущенное (after=<seq>), потом стримим живое."""
    run = runs.manager.get(run_id)
    if not run:
        raise HTTPException(404, "прогон не найден")

    async def gen():
        q = run.subscribe() if run.status == "running" else None
        try:
            for ev in [e for e in run.events if e.get("seq", 0) > after]:
                yield _sse(ev)
            if q is None:
                yield _sse({"type": "_eof", "seq": run.seq})
                return
            while True:
                if await request.is_disconnected():
                    return
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"          # держим соединение живым через прокси
                    continue
                yield _sse(ev)
                if ev.get("type") == "_eof":
                    return
        finally:
            if q is not None:
                run.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "Connection": "keep-alive",
        "X-Accel-Buffering": "no",              # nginx иначе буферизует и прогресс встаёт
    })


def _sse(ev: Dict[str, Any]) -> str:
    return f"id: {ev.get('seq', 0)}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"


# --- статика (собранный фронт) ------------------------------------------------
if config.UI_DIST.exists():
    app.mount("/assets", StaticFiles(directory=config.UI_DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    async def spa(path: str) -> FileResponse:
        f = config.UI_DIST / path
        if path and f.is_file():
            return FileResponse(f)
        return FileResponse(config.UI_DIST / "index.html")   # SPA-роутинг
