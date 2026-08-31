# -*- coding: utf-8 -*-
"""Реестр прогонов: спавн orchestrator.py, стрим stdout, состояние, отмена.

Почему подпроцесс, а не импорт: main() в orchestrator.py делает argparse, ГЛОБАЛЬНО
подменяет sys.stdout/sys.stderr на _Tee и зовёт sys.exit — вызвать его внутри
веб-процесса значит отдать ему свой stdout и получить SystemExit в воркере.
Подпроцесс — это ровно то, что делает orchestrator_agent.py:142-148 и ENTRYPOINT в Dockerfile.

Параллельные прогоны НЕ безопасны и нигде не залочены: два сбора дерутся за один
--user-data-dir Chrome, дефолтный путь лидов детерминирован (leads_<отрасли>.json)
и перезаписывается, а _flush_outbox() на старте прогона B дольёт файлы прогона A
прямо посреди его работы. Поэтому здесь ОДИН активный прогон на инстанс: второй
запуск отвечает 409, а не тихо ломает первый.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config, events

IS_WIN = sys.platform == "win32"

# Стадии компании в порядке прохождения — UI рисует по ним прогресс-цепочку.
STAGES = ["research", "person", "writing", "onepager", "upload"]


def _default_model_flag() -> str:
    """Runtime-дефолт --model из конфига оркестратора (sys.path добавляет leads.py)."""
    try:
        import kimi_config
        return kimi_config.default_model_flag()
    except Exception:                              # noqa: BLE001 — веб не должен падать из-за конфига
        return "kimi"


class RunConflict(RuntimeError):
    """Уже идёт прогон."""


class Run:
    def __init__(self, run_id: str, params: Dict[str, Any], cmd: List[str]):
        self.id = run_id
        self.params = params
        self.cmd = cmd
        self.status = "running"          # running | done | failed | cancelled | interrupted
        self.created_at = time.time()
        self.finished_at: Optional[float] = None
        self.exit_code: Optional[int] = None
        self.phase: Optional[str] = None
        self.log_path: Optional[str] = None
        self.leads_path: Optional[str] = params.get("leads_json") or None
        self.estimate: Optional[str] = None
        self.total: Optional[int] = None
        self.summary: Optional[Dict[str, Any]] = None
        self.collect: Dict[str, Any] = {}
        self.companies: Dict[int, Dict[str, Any]] = {}
        self.errors: List[str] = []
        self.seq = 0
        self.events: List[Dict[str, Any]] = []
        self.subscribers: List[asyncio.Queue] = []
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.dir = config.JOBS_DIR / run_id
        self.dir.mkdir(parents=True, exist_ok=True)

    # --- состояние -----------------------------------------------------------
    def _company(self, idx: int) -> Dict[str, Any]:
        c = self.companies.get(idx)
        if c is None:
            c = {"idx": idx, "name": None, "inn": None, "status": "pending",
                 "stage": None, "cost": 0.0, "dir": None, "one_pager": False,
                 "note": None, "retries": 0}
            self.companies[idx] = c
        return c

    def _load_leads(self, path: str) -> None:
        """Подтянуть имена/ИНН отобранных компаний: индекс [idx] из лога — это позиция
        в ЭТОМ файле (pipeline._save сохраняет ровно отобранный список)."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for i, lead in enumerate([l for l in data if l and l.get("name")]):
            c = self._company(i)
            c["name"] = lead.get("name")
            c["inn"] = lead.get("_inn")
            c["revenue"] = lead.get("_revenue")
            c["industry"] = lead.get("_industry")

    def apply(self, ev: Dict[str, Any]) -> None:
        t = ev["type"]
        if t == "log_path":
            self.log_path = ev["path"]
        elif t == "volume":
            self.total = ev["total"]
        elif t == "phase":
            self.phase = ev["phase"]
        elif t == "estimate":
            self.estimate = ev["text"]
        elif t == "leads_saved":
            self.leads_path = ev["path"]
            self._load_leads(ev["path"])
        elif t in ("search", "contacts", "stubs"):
            self.collect.update({k: v for k, v in ev.items() if k not in ("type", "line")})
        elif t == "company_stage":
            c = self._company(ev["idx"])
            c["status"] = "running"
            c["stage"] = ev["stage"]
            c["note"] = ev["text"]
        elif t == "company_done":
            c = self._company(ev["idx"])
            c.update({"status": "done", "stage": "upload", "dir": ev["dir"],
                      "one_pager": ev["one_pager"], "cost": ev["cost"]})
            if not c.get("name"):
                c["name"] = ev["name"]          # обрезано до 40 симв. — только как фолбэк
        elif t == "company_skipped":
            c = self._company(ev["idx"])
            c["note"] = ev["text"]
            if ev["mode"] == "full":
                c.update({"status": "skipped", "stage": "upload"})
            else:
                c.update({"status": "running", "stage": "onepager"})
        elif t == "retry":
            for c in self.companies.values():
                if c.get("name") == ev["name"]:
                    c["retries"] = ev["attempt"]
                    break
        elif t == "error":
            self.errors.append(ev["text"])
        elif t == "finished":
            self.summary = {k: ev[k] for k in ("ok", "total", "files", "cost")}

    def snapshot(self) -> Dict[str, Any]:
        return {
            "id": self.id, "status": self.status, "params": self.params,
            "cmd": self.cmd, "created_at": self.created_at,
            "finished_at": self.finished_at, "exit_code": self.exit_code,
            "phase": self.phase, "total": self.total, "estimate": self.estimate,
            "log_path": self.log_path, "leads_path": self.leads_path,
            "collect": self.collect, "summary": self.summary,
            "errors": self.errors[-20:], "seq": self.seq,
            "companies": [self.companies[i] for i in sorted(self.companies)],
        }

    def meta(self) -> Dict[str, Any]:
        s = self.snapshot()
        s.pop("companies", None)
        return s

    # --- события -------------------------------------------------------------
    def emit(self, ev: Dict[str, Any]) -> None:
        self.seq += 1
        ev["seq"] = self.seq
        ev["t"] = time.time()
        self.apply(ev)
        self.events.append(ev)
        for q in list(self.subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.subscribers:
            self.subscribers.remove(q)


def build_argv(p: Dict[str, Any]) -> List[str]:
    """Параметры -> argv orchestrator.py. Повторяет orchestrator_agent._build_cmd (:92-129).

    Объём задаём через --per-industry, а НЕ --count: оркестратор делит --count как
    ceil(count/K) по отраслям, и «10 на отрасль» по трём отраслям молча становилось
    четырьмя. Это ровно та причина, по которой агент-обёртка тоже не трогает --count.
    """
    cmd = [str(config.PYTHON), str(config.ORCH_PY)]
    leads_json = (p.get("leads_json") or "").strip()
    inds = [s.strip() for s in (p.get("industries") or []) if s.strip()]

    if leads_json:
        cmd.append(leads_json)                       # позиционный: ТОЛЬКО ресёрч
    if inds:
        cmd += ["--industries", ",".join(inds)]
        cmd += ["--per-industry", str(int(p.get("per_industry") or 10))]
    if p.get("min_revenue"):
        cmd += ["--min-revenue", str(float(p["min_revenue"]))]

    # Несколько регионов и исключения умещаются в ОДИН --region: коллектор разбирает эту
    # строку сам (source_rusprofile.parse_region_query) — запятые = перечисление, приставка
    # «не » = исключение. Собираем строку тут, а не во фронте: синтаксис — деталь CLI.
    # NB: «Москва» матчит и «Московскую область» (region_included), а вот в ИСКЛЮЧЕНИИ
    # области не задеваются (region_excluded) — это осознанное поведение коллектора.
    tokens = [r.strip() for r in (p.get("regions") or []) if r and r.strip()]
    tokens += [f"не {r.strip()}" for r in (p.get("exclude_regions") or []) if r and r.strip()]
    if tokens:
        cmd += ["--region", ", ".join(tokens)]
    if (p.get("out") or "").strip():
        cmd += ["--out", p["out"].strip()]
    model = str(p.get("model") or "").strip().lower()
    if model not in ("kimi", "glm", "claude"):
        model = _default_model_flag()          # пустое поле -> runtime-дефолт ветки
    cmd += ["--model", model]
    cmd += ["--workers", str(int(p.get("workers") or 2))]
    cmd += ["--base", (p.get("base") or config.DISK_BASE).strip()]
    if p.get("show_browser"):
        cmd.append("--show-browser")
    if p.get("dry_run"):
        cmd.append("--dry-run")
    if p.get("no_upload"):
        cmd.append("--no-upload")
    if p.get("no_presentation"):
        cmd.append("--no-presentation")
    if p.get("no_person_enrich"):
        cmd.append("--no-person-enrich")
    if p.get("redo"):
        cmd.append("--redo")
    return cmd


class RunManager:
    def __init__(self) -> None:
        self.runs: Dict[str, Run] = {}
        self.active: Optional[str] = None
        self._lock = asyncio.Lock()

    def get(self, run_id: str) -> Optional[Run]:
        return self.runs.get(run_id)

    def list(self) -> List[Dict[str, Any]]:
        return [r.meta() for r in sorted(self.runs.values(),
                                         key=lambda r: r.created_at, reverse=True)]

    async def start(self, params: Dict[str, Any]) -> Run:
        async with self._lock:
            if self.active and self.runs[self.active].status == "running":
                raise RunConflict("Уже идёт прогон: " + self.active)

            if not params.get("industries") and not (params.get("leads_json") or "").strip():
                raise ValueError("Нужно указать отрасли ИЛИ путь к готовому JSON лидов")

            run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
            cmd = build_argv(params)
            run = Run(run_id, params, cmd)
            self.runs[run_id] = run
            self.active = run_id

            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"        # русский stdout не должен упасть в cp1251

            kwargs: Dict[str, Any] = {}
            if IS_WIN:
                # Своя группа процессов: только так можно послать дочернему дереву
                # CTRL_BREAK (мягкая отмена). Оркестратор ловит её как KeyboardInterrupt,
                # пробрасывает CancelledError и сам убивает Kimi-подпроцесс (:367-373).
                kwargs["creationflags"] = getattr(__import__("subprocess"),
                                                  "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                kwargs["start_new_session"] = True

            run.proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(config.ORCH_DIR),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env=env, **kwargs)

            (run.dir / "meta.json").write_text(
                json.dumps({"id": run_id, "params": params, "cmd": cmd,
                            "created_at": run.created_at}, ensure_ascii=False, indent=2),
                encoding="utf-8")

            # Ресёрч по готовому JSON: имена компаний известны сразу, до первой строки лога.
            if run.leads_path:
                run._load_leads(run.leads_path)

            asyncio.create_task(self._pump(run))
            return run

    async def _pump(self, run: Run) -> None:
        """Читать stdout построчно, парсить в события, писать на диск, слать подписчикам."""
        jsonl = (run.dir / "events.jsonl").open("a", encoding="utf-8")
        try:
            assert run.proc and run.proc.stdout
            while True:
                raw = await run.proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                ev = events.parse(line)
                run.emit(ev)
                jsonl.write(json.dumps(ev, ensure_ascii=False) + "\n")
                jsonl.flush()
            rc = await run.proc.wait()
            run.exit_code = rc
            if run.status != "cancelled":
                # exit 3 = ни одна компания не сделана (orchestrator.py:1027-1028)
                run.status = "done" if rc == 0 else "failed"
        except Exception as e:                       # noqa: BLE001 — pump не должен ронять сервер
            run.status = "failed"
            run.errors.append(f"web: сбой чтения вывода: {e}")
        finally:
            run.finished_at = time.time()
            jsonl.close()
            (run.dir / "meta.json").write_text(
                json.dumps(run.meta(), ensure_ascii=False, indent=2), encoding="utf-8")
            run.emit({"type": "run_finished", "line": "", "status": run.status,
                      "exit_code": run.exit_code})
            for q in list(run.subscribers):
                q.put_nowait({"type": "_eof", "seq": run.seq})
            if self.active == run.id:
                self.active = None

    async def cancel(self, run_id: str) -> bool:
        run = self.runs.get(run_id)
        if not run or not run.proc or run.status != "running":
            return False
        run.status = "cancelled"
        try:
            if IS_WIN:
                run.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                run.proc.send_signal(signal.SIGINT)
        except (ProcessLookupError, OSError):
            return False
        # Мягкая отмена может не долететь (например, процесс висит в C-коде драйвера) —
        # добиваем через 20 с, иначе прогон останется «отменённым», но живым.
        async def _reap() -> None:
            try:
                await asyncio.wait_for(run.proc.wait(), timeout=20)
            except asyncio.TimeoutError:
                try:
                    run.proc.kill()
                except ProcessLookupError:
                    pass
        asyncio.create_task(_reap())
        return True

    def load_history(self) -> None:
        """Поднять журнал прошлых прогонов (только мета — события лежат в events.jsonl)."""
        if not config.JOBS_DIR.exists():
            return
        for d in sorted(config.JOBS_DIR.iterdir()):
            meta_f = d / "meta.json"
            if not d.is_dir() or not meta_f.exists():
                continue
            try:
                m = json.loads(meta_f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            run = Run.__new__(Run)                   # без спавна процесса
            run.__dict__.update({
                "id": m.get("id", d.name), "params": m.get("params", {}),
                "cmd": m.get("cmd", []), "created_at": m.get("created_at", 0),
                "finished_at": m.get("finished_at"), "exit_code": m.get("exit_code"),
                "phase": None, "log_path": None, "leads_path": m.get("params", {}).get("leads_json") or None,
                "estimate": None, "total": None, "summary": None,
                "collect": {}, "errors": [], "seq": 0,
                "companies": {}, "events": [], "subscribers": [], "proc": None, "dir": d,
                # Процесс не пережил рестарт сервера: «running» в мете — это прерванный прогон.
                "status": "interrupted" if m.get("status") in (None, "running")
                          else m["status"],
            })
            # Ресёрч по готовому JSON: события leads_saved не будет (сбор не запускался),
            # поэтому имена и ИНН тянем из того же файла, что скормили оркестратору.
            if run.leads_path:
                run._load_leads(run.leads_path)

            # Состояние (компании, стадии, стоимость, итог) НЕ храним в мете — переигрываем
            # сохранённые события через тот же apply(), что и живой прогон. Один источник
            # правды: иначе история и живой экран разъезжались бы по логике.
            ev_file = d / "events.jsonl"
            if ev_file.exists():
                for s in ev_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    if not s.strip():
                        continue
                    try:
                        ev = json.loads(s)
                    except ValueError:
                        continue
                    run.events.append(ev)
                    run.seq = max(run.seq, ev.get("seq", 0))
                    run.apply(ev)
            self.runs[run.id] = run


manager = RunManager()
