# -*- coding: utf-8 -*-
r"""
Оркестратор второй фазы: по каждой собранной компании — последовательно ДВА агента и
ДВА документа (карта бизнес-процессов + контакты/точки входа), которые ПЕРЕЗАПИСЫВАЮТ
заготовки в папке компании на Яндекс Диске (папки создаёт первый этап / disk_organize).

Архитектура (детерминированный Python-оркестратор, НЕ LLM-оркестратор):
  leads.json (тот же, что на этапе сбора)
    └─ по каждой компании, пул из --workers параллельно:
         агент-1 (карта бизнес-процессов, opus+WebSearch) рендерит досье во temp и
         отдаёт payload агенту-2 (контакты, opus+WebSearch) — оба .docx во temp
         → upload во УЖЕ существующую папку disk:/Лиды/<отрасль>/<полнота>/<компания>/
           перезаписывая досье_компании_<имя>.docx и контакты_и_точки_входа_<имя>.docx

Путь на Диске считается ровно теми же функциями disk_organize, что и у агента 1,
поэтому файлы ложатся в его папки (mkdir идемпотентный — папка уже есть).

ПОЛНАЯ ЦЕПОЧКА одной командой (сам запускает оба этапа; число компаний = предписание
первого агента, по умолчанию 200 в отрасли — если не задано другое):
  py orchestrator.py --industries mining               # собрать 200 mining и по ВСЕМ сделать досье+стратегию
  py orchestrator.py --industries mining --count 10    # явно 10
Только ресёрч по уже готовому JSON (число = размер JSON):
  py orchestrator.py "D:\лиды\<leads>.json"
Дёшево проверить связку без трат на API и без следов на Диске:
  py orchestrator.py "D:\лиды\<leads>.json" --dry-run --no-upload

Chrome при сборе по умолчанию СКРЫТ (окно не открывается); показать — --show-browser.

Зависимости боевого режима: claude-agent-sdk, python-docx, ANTHROPIC_API_KEY,
рабочий disk-логин yacli (как у первого агента). dry-run не требует ничего сверх stdlib.
"""
import argparse
import asyncio
import collections
import json
import os
import shutil
import sys
import tempfile

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)

import disk_organize as DO  # пути на Диске + upload + заглушки (stdlib, без сети при импорте)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ----------------------- контекст карты процессов -----------------------

def _biz_context(dossier):
    """Сжать payload карты бизнес-процессов агента-1 в короткий текст для агента-2.

    Передаём агенту-2 профиль, домены AS-IS и точки внедрения ИИ (процесс/домен/боль),
    чтобы он приоритизировал точки входа под реально релевантные функции компании."""
    if not dossier:
        return ""
    lines = []
    if dossier.get("profile"):
        lines.append("Профиль: " + str(dossier["profile"]))
    asis = dossier.get("asis") or []
    labels = [(x.get("label") or "").rstrip(":") for x in asis if x.get("label")]
    if labels:
        lines.append("Процессы AS-IS: " + "; ".join(labels))
    points = dossier.get("points") or []
    if points:
        lines.append("Точки внедрения ИИ (процесс — домен — боль):")
        for p in points[:12]:
            seg = " — ".join(s for s in (p.get("process"), p.get("domain"), p.get("pain")) if s)
            if seg:
                lines.append("  • " + seg)
    return "\n".join(lines)


def _handle(lead):
    return {
        "company_name": lead.get("name") or "",
        "inn": str(lead.get("_inn") or "").strip(),
        "aspects": "профиль, численность, ЛПР, боли под on-prem LLM/RAG, СМИ за 5 лет",
    }


async def _run_agent(options, handoff, label):
    """Прогнать одну сессию ClaudeSDKClient, печатать вызовы инструментов, вернуть $-стоимость."""
    from claude_agent_sdk import (
        ClaudeSDKClient, ResultMessage, AssistantMessage, ToolUseBlock,
    )
    cost = 0.0
    async with ClaudeSDKClient(options=options) as client:
        await client.query(handoff)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        print(f"    [{label}] → {getattr(block, 'name', '')}")
            elif isinstance(message, ResultMessage):
                if getattr(message, "total_cost_usd", None):
                    cost = message.total_cost_usd
    return cost


async def _research_one(lead, idx, d_tmp, c_tmp, model):
    """Две стадии на компанию: агент-1 (карта бизнес-процессов) -> агент-2 (контакты).
    Рендерит оба .docx во временные пути. Возвращает суммарную $-стоимость."""
    import anyio  # noqa: F401  (нужен косвенно SDK/CRA)
    import company_research_agent as CRA
    import contact_research_agent as CCA
    from claude_agent_sdk import tool, create_sdk_mcp_server, ClaudeAgentOptions

    h = _handle(lead)
    captured = {}        # payload карты бизнес-процессов -> контекст для агента-2
    cost = 0.0
    base_opts = dict(
        disallowed_tools=["Bash", "Edit", "Write", "NotebookEdit"],
        permission_mode="bypassPermissions",
        setting_sources=[],
        max_turns=60,
    )

    # ---------- Стадия A: агент-1 — карта бизнес-процессов (AS-IS -> ИИ -> TO-BE) ----------
    # save-инструмент — замыкание на временный путь ЭТОЙ компании (потокобезопасно).
    @tool("save_dossier_docx",
          "Сохранить карту бизнес-процессов (AS-IS -> точки ИИ -> TO-BE) в .docx. "
          "Ссылки в mentions/sources — только проверенные WebFetch'ем.",
          CRA.DOSSIER_DOCX_SCHEMA)
    async def _save_dossier(args):
        a = dict(args)
        a["filename"] = f"_orq_d_{idx}.docx"          # уникальное имя -> «Загрузки», затем переносим
        dl = await asyncio.to_thread(CRA._write_dossier_docx, a)
        await asyncio.to_thread(shutil.move, dl, d_tmp)
        captured["dossier"] = a                        # отдаём payload агенту-2
        return {"content": [{"type": "text", "text": "карта бизнес-процессов сохранена"}]}

    server_a = create_sdk_mcp_server(
        name="research", version="3.0.0", tools=[CRA.deep_research, _save_dossier])
    options_a = ClaudeAgentOptions(
        model=model, system_prompt=CRA.DOSSIER_SYSTEM,
        mcp_servers={"research": server_a},
        allowed_tools=["mcp__research__deep_research", "mcp__research__save_dossier_docx",
                       "WebSearch", "WebFetch"],
        **base_opts)
    handoff_a = (
        "Построй карту бизнес-процессов (AS-IS -> точки внедрения ИИ -> TO-BE) и сохрани .docx. "
        "Значения для инструментов:\n"
        f"  company_name = {h['company_name']!r}\n"
        f"  inn          = {h['inn']!r}\n"
        f"  aspects      = {h['aspects']!r}\n"
        "Следуй порядку из системного промпта. Разделы СМИ/Источники — только проверенные ссылки."
    )
    cost += await _run_agent(options_a, handoff_a, f"{idx}/A")

    # подстраховка: агент-1 не сохранил карту -> заготовка, чтобы upload не упал
    if not os.path.exists(d_tmp):
        await asyncio.to_thread(DO.generate_dossier, lead, d_tmp)

    # ---------- Стадия B: агент-2 — контакты и точки входа (по JSON + карте процессов) ----------
    @tool("save_contacts_docx",
          "Сохранить отчёт по контактам и точкам входа (key_contacts/roles/branches/sources) "
          "в .docx. Ссылки в profiles/sources — только проверенные WebFetch'ем.",
          CCA.CONTACTS_SCHEMA)
    async def _save_contacts(args):
        a = dict(args)
        a["filename"] = f"_orq_c_{idx}.docx"
        cl = await asyncio.to_thread(CCA._write_contacts_docx, a)
        await asyncio.to_thread(shutil.move, cl, c_tmp)
        return {"content": [{"type": "text", "text": "контакты сохранены"}]}

    server_b = create_sdk_mcp_server(
        name="contacts", version="1.0.0", tools=[CRA.deep_research, _save_contacts])
    options_b = ClaudeAgentOptions(
        model=model, system_prompt=CCA.CONTACTS_SYSTEM,
        mcp_servers={"contacts": server_b},
        allowed_tools=["mcp__contacts__deep_research", "mcp__contacts__save_contacts_docx",
                       "WebSearch", "WebFetch"],
        **base_opts)
    handoff_b = CCA._build_handoff(lead, _biz_context(captured.get("dossier")))
    cost += await _run_agent(options_b, handoff_b, f"{idx}/B")

    # подстраховка: агент-2 не сохранил -> заготовка контактов
    if not os.path.exists(c_tmp):
        await asyncio.to_thread(DO.generate_contacts, lead, c_tmp)
    return cost


def _collect(industries, count, min_revenue, region, headless, offscreen, base, account, out_xlsx):
    """ФАЗА 1 (первый агент): RusProfile -> контакты -> отбор -> Excel -> папки+заготовки на Диске.
    Блокирующий (открывает Chrome; offscreen=True -> окно за экраном, не видно). Возвращает picked[]."""
    import math
    import source_rusprofile as RP
    import rusprofile_session as RPS
    import pipeline
    from build_excel import build

    inds = [s.strip() for s in industries.split(",") if s.strip() in RP.INDUSTRY]
    if not inds:
        raise SystemExit("не распознаны отрасли. Доступно: " + ", ".join(sorted(RP.INDUSTRY)))
    if not os.path.exists(RPS.COOKIES_FILE):
        raise SystemExit("нет cookie RusProfile — один раз: py rusprofile_session.py --login")
    per_ind = math.ceil(count / max(1, len(inds)))
    json_out = os.path.splitext(out_xlsx)[0] + ".json"

    print(f"[1/2] RusProfile: {inds} | порог >{min_revenue / 1e9:g} млрд"
          + (f" | регион {region}" if region else ""))
    leads = RP.harvest(inds, min_revenue=min_revenue, per_industry=per_ind,
                       region=region, headless=headless, out_path=json_out, offscreen=offscreen)
    if not leads:
        raise SystemExit("RusProfile ничего не вернул — проверь коды ОКВЭД/доступ.")
    with RPS.RusProfileAuth(headless=headless, offscreen=offscreen) as rs:  # контакты с платного аккаунта
        rs.enrich_leads(leads, only_missing=True, log=print)
    picked = pipeline._select(leads, count, inds)
    build(picked, ("Лиды: " + ", ".join(inds))[:90], out_xlsx)
    pipeline._save(picked, json_out)
    print(f"[1/2] собрано {len(picked)} | Excel: {out_xlsx}")
    print("[1/2] раскладка папок+заготовок на Диске ...")
    DO.organize_to_disk(picked, base=base, account=account, log=print)   # папки создаёт ПЕРВЫЙ агент
    return picked


def _company_dir(lead, base, dup_names):
    ind = DO._safe(DO.industry_folder(lead))
    cat = DO._safe(DO.category_for(lead))
    comp = DO._company_name(lead, dup_names)
    return f"{base}/{ind}/{cat}/{comp}"


def _free_ram_gb():
    """Свободная физическая память в ГБ (Windows, без зависимостей). None если не удалось."""
    try:
        import ctypes

        class _MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        ms = _MS()
        ms.dwLength = ctypes.sizeof(_MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            return ms.ullAvailPhys / (1024 ** 3)
    except Exception:
        pass
    return None


async def main():
    ap = argparse.ArgumentParser(
        description="Полная цепочка: сбор (RusProfile) + ресёрч (карта процессов + контакты) в папки Яндекс Диска")
    ap.add_argument("leads", nargs="?", default=None,
                    help="готовый JSON лидов (если БЕЗ --industries)")
    # --- ФАЗА 1: сбор (первый агент). Задаёшь --industries -> оркестратор сам соберёт лиды и создаст папки ---
    ap.add_argument("--industries", default=None,
                    help="ЗАПУСТИТЬ СБОР: отрасли через запятую (mining,construction,energy,...)")
    ap.add_argument("--count", type=int, default=200, help="сколько лидов собрать (с --industries)")
    ap.add_argument("--min-revenue", type=float, default=1e9, help="порог выручки, ₽ (с --industries)")
    ap.add_argument("--region", default=None, help="регион названием/аббревиатурой (с --industries)")
    ap.add_argument("--show-browser", dest="show_browser", action="store_true",
                    help="показать окно Chrome при сборе (по умолчанию СКРЫТО/headless)")
    ap.add_argument("--headless", action="store_true",
                    help=argparse.SUPPRESS)  # deprecated: headless теперь по умолчанию (флаг оставлен для совместимости)
    ap.add_argument("--out", default=None, help="путь к .xlsx (с --industries)")
    # --- общее + ФАЗА 2: ресёрч ---
    ap.add_argument("--base", default="disk:/Лиды")
    ap.add_argument("--account", default=None)
    ap.add_argument("--workers", type=int, default=2,
                    help="параллельных ресёрч-агентов (claude CLI). При нехватке RAM авто-снижается до 1")
    ap.add_argument("--model", default="opus", help="opus (качество) | sonnet (дешевле)")
    ap.add_argument("--dry-run", action="store_true", help="ресёрч без LLM — заготовки (бесплатно)")
    ap.add_argument("--no-upload", action="store_true", help="ресёрч-файлы не грузить на Диск")
    a = ap.parse_args()
    # режим окна сбора: по умолчанию headed, но ЗА ЭКРАНОМ (антибот RusProfile проходит,
    # окна не видно). --show-browser => видимое окно; --headless => без окна (может НЕ пройти антибот).
    headless = bool(a.headless)
    offscreen = (not a.show_browser) and (not headless)

    if a.industries:                                  # ФАЗА 1 — сбор сам (блокирующий Chrome -> в поток)
        mode = ("без окна (headless)" if headless
                else "окно скрыто за экраном" if offscreen else "окно Chrome видно")
        print(f"=== ФАЗА 1: сбор лидов (RusProfile — {mode}) ===")
        out_xlsx = a.out or os.path.join(r"D:\лиды", "leads_" + a.industries.replace(",", "_") + ".xlsx")
        leads = await asyncio.to_thread(
            _collect, a.industries, a.count, a.min_revenue, a.region,
            headless, offscreen, a.base, a.account, out_xlsx)
    elif a.leads:                                     # готовый JSON — только ресёрч
        leads = json.load(open(a.leads, encoding="utf-8"))
    else:
        print("Источник не задан: укажи --industries <отрасли> (сбор+ресёрч) ИЛИ путь к leads.json (только ресёрч).")
        return
    leads = [l for l in leads if l and l.get("name")]
    dup_names = {n for n, c in collections.Counter(DO._safe(l.get("name")) for l in leads).items() if c > 1}
    sel = leads          # ресёрчим ВСЕХ, кого собрал первый агент (его --count, по умолч. 200)
    if not sel:
        print("Пусто — нет лидов.")
        return

    print("\n=== ФАЗА 2: ресёрч (карта бизнес-процессов + контакты) ===")
    if not a.dry_run:
        print(f"[оценка] {len(sel)} компаний × ~$1–2 = ~${len(sel)}–${2 * len(sel)} ({a.model}). "
              "Число задаётся первым агентом (--count, по умолч. 200).")
        free = _free_ram_gb()                         # каждый ресёрч = свой claude CLI (Node, сотни МБ)
        if free is not None and free < 3.0 and a.workers > 1:
            print(f"[ОЗУ] свободно ~{free:.1f} ГБ — снижаю параллелизм ресёрча до 1 "
                  "(несколько claude CLI при нехватке памяти падают 0xC0000409). "
                  "Освободи RAM или задай --workers вручную.")
            a.workers = 1
    tmp = tempfile.mkdtemp(prefix="orq_")

    # верхние уровни (отрасль/категория) у первого агента уже есть; mkdir идемпотентный.
    if not a.no_upload:
        cache = set()
        await asyncio.to_thread(DO.ensure_dir, a.base, a.account, cache)
        for lead in sel:
            ind = DO._safe(DO.industry_folder(lead))
            cat = DO._safe(DO.category_for(lead))
            await asyncio.to_thread(DO.ensure_dir, f"{a.base}/{ind}/{cat}", a.account, cache)

    sem = asyncio.Semaphore(max(1, a.workers))

    async def process(idx, lead):
        async with sem:
            d_tmp = os.path.join(tmp, f"{idx}_d.docx")
            c_tmp = os.path.join(tmp, f"{idx}_c.docx")
            cost = 0.0
            if a.dry_run:
                try:
                    await asyncio.to_thread(DO.generate_dossier, lead, d_tmp)
                    await asyncio.to_thread(DO.generate_contacts, lead, c_tmp)
                except Exception as e:
                    print(f"  [!] {lead.get('name')}: {e}")
                    return {"name": lead.get("name"), "ok": False, "cost": cost}
            else:
                # ресёрч-агент изредка падает аварийно (0xC0000409 = OOM Node при нехватке
                # RAM / конкуренции claude CLI) — ретраим до 3 раз с паузой
                err = None
                for attempt in range(3):
                    try:
                        cost = await _research_one(lead, idx, d_tmp, c_tmp, a.model)
                        err = None
                        break
                    except Exception as e:
                        err = e
                        print(f"  [retry {attempt + 1}/3] {lead.get('name')}: {str(e)[:70]}")
                        await asyncio.sleep(4)
                if err is not None:
                    print(f"  [!] {lead.get('name')}: {err}")
                    return {"name": lead.get("name"), "ok": False, "cost": cost}

            comp_dir = _company_dir(lead, a.base, dup_names)
            dn = DO._safe(lead.get("name"))
            if not a.no_upload:
                try:
                    await asyncio.to_thread(DO._mkdir, comp_dir, a.account)  # папка обычно уже есть
                    await asyncio.to_thread(DO._upload, d_tmp, f"{comp_dir}/досье_компании_{dn}.docx", a.account, True)
                    await asyncio.to_thread(DO._upload, c_tmp, f"{comp_dir}/контакты_и_точки_входа_{dn}.docx", a.account, True)
                except Exception as e:
                    print(f"  [!] upload {lead.get('name')}: {e}")
                    return {"name": lead.get("name"), "ok": False, "cost": cost}
            print(f"  ✓ [{idx}] {lead.get('name')[:40]} -> {comp_dir}"
                  + (f"  (${cost:.2f})" if cost else ""))
            return {"name": lead.get("name"), "ok": True, "cost": cost, "dir": comp_dir}

    results = await asyncio.gather(*(process(i, l) for i, l in enumerate(sel)))
    ok = [r for r in results if r and r.get("ok")]
    total = sum(r.get("cost") or 0 for r in results if r)
    print(f"\n[ГОТОВО] компаний: {len(ok)}/{len(sel)} | файлов: {2 * len(ok)} "
          + ("(локально, без Диска) " if a.no_upload else f"в {a.base} ")
          + (f"| стоимость ~${total:.2f}" if total else "| dry-run, $0"))
    if a.no_upload:
        print(f"[локально] .docx во временной папке: {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
