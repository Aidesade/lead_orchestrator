# -*- coding: utf-8 -*-
r"""
Оркестратор второй фазы: по каждой собранной компании — глубокий ресёрч и
ДВА документа (досье + стратегия коммуникации), которые ПЕРЕЗАПИСЫВАЮТ заготовки
в папке компании на Яндекс Диске (папки уже создал первый агент / disk_organize).

Архитектура (детерминированный Python-оркестратор, НЕ LLM-оркестратор):
  leads.json (тот же, что у агента 1)
    └─ по каждой компании, пул из --workers параллельно:
         1 ресёрч-сессия (opus + WebSearch, ОДИН раз) рендерит ОБА .docx во temp
         → upload во ВЖЕ существующую папку disk:/Лиды/<отрасль>/<полнота>/<компания>/
           перезаписывая досье_компании_<имя>.docx и стратегия_коммуникации_<имя>.docx

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


# ----------------------------- рендер .docx -----------------------------

def _strategy_doc(payload, path):
    """Стратегия коммуникации -> компактный .docx (≤1 стр.). python-docx."""
    from docx import Document
    from docx.shared import Pt, Cm, RGBColor

    GRAY = RGBColor(0x5A, 0x5A, 0x5A)
    doc = Document()
    sec = doc.sections[0]
    for m in ("top_margin", "bottom_margin", "left_margin", "right_margin"):
        setattr(sec, m, Cm(1.2))
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(9.5)
    normal.paragraph_format.space_after = Pt(2)
    normal.paragraph_format.line_spacing = 1.0

    def heading(text):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(5)
        p.paragraph_format.space_after = Pt(2)
        r = p.add_run(text)
        r.bold = True
        r.font.size = Pt(11)

    def bullet(lead, rest=""):
        p = doc.add_paragraph(style="List Bullet")
        p.paragraph_format.space_after = Pt(1)
        p.add_run(lead).bold = True
        if rest:
            p.add_run(" — " + rest)

    title = doc.add_paragraph()
    title.paragraph_format.space_after = Pt(1)
    tr = title.add_run(payload.get("company") or "Стратегия коммуникации")
    tr.bold = True
    tr.font.size = Pt(13)
    if payload.get("subtitle"):
        sp = doc.add_paragraph()
        sp.paragraph_format.space_after = Pt(3)
        sr = sp.add_run(payload["subtitle"])
        sr.font.size = Pt(8.5)
        sr.font.color.rgb = GRAY

    if payload.get("summary"):
        heading("Сводка")
        doc.add_paragraph(payload["summary"])
    if payload.get("channel"):
        heading("Канал захода и ЛПР")
        doc.add_paragraph(payload["channel"])
    if payload.get("first_touch"):
        heading("Первое касание")
        doc.add_paragraph(payload["first_touch"])
    if payload.get("script"):
        heading("Сценарий разговора")
        for b in payload["script"]:
            doc.add_paragraph(b, style="List Bullet").paragraph_format.space_after = Pt(1)
    if payload.get("offer_fit"):
        heading("Оффер под боли (on-prem LLM + RAG)")
        for f in payload["offer_fit"]:
            bullet((f.get("pain") or "").rstrip(":"), f.get("solution", ""))
    if payload.get("objections"):
        heading("Возражения и ответы")
        for o in payload["objections"]:
            bullet((o.get("q") or "").rstrip(":"), o.get("a", ""))
    if payload.get("next_step"):
        heading("Следующий шаг")
        doc.add_paragraph(payload["next_step"])
    if payload.get("sources"):
        sp = doc.add_paragraph()
        sp.paragraph_format.space_before = Pt(4)
        sr = sp.add_run(payload["sources"])
        sr.font.size = Pt(8)
        sr.font.color.rgb = GRAY
    doc.save(path)


# --------------------- схемы инструментов для агента ---------------------

DOSSIER_SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string", "description": "Заголовок: «Досье компании — ...»"},
        "subtitle": {"type": "string", "description": "ИНН · ОГРН · ОКВЭД · город · 'для on-premise LLM + RAG'"},
        "profile": {"type": "string", "description": "Раздел 1: профиль деятельности (абзац)"},
        "scale": {"type": "array", "items": {"type": "string"},
                  "description": "Раздел 2: Численность: ...; Выручка: ...; Госзаказ/риски: ..."},
        "owner_lpr": {"type": "array", "items": {"type": "string"},
                      "description": "Раздел 3: Собственник / Гендиректор / расхождения / Контакты"},
        "pains": {"type": "array", "items": {
            "type": "object",
            "properties": {"label": {"type": "string"}, "text": {"type": "string"}},
            "required": ["label", "text"]},
            "description": "Раздел 4: боль -> решение через LLM/RAG (акцент 152-ФЗ/on-prem)"},
        "mentions": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "url": {"type": "string"}, "date": {"type": "string"}},
            "required": ["title", "url"]},
            "description": "Раздел 5: СМИ — ТОЛЬКО проверенные WebFetch'ем ссылки"},
        "sources": {"type": "string", "description": "Строка 'Источники: ...'"},
    },
    "required": ["company", "profile"],
}

STRATEGY_SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string", "description": "Заголовок"},
        "subtitle": {"type": "string", "description": "ИНН · отрасль · ЛПР"},
        "summary": {"type": "string", "description": "1–2 предложения: кто это и почему интересен под оффер"},
        "channel": {"type": "string", "description": "Через кого и как заходить к ЛПР (канал: email/звонок/тендерная площадка)"},
        "first_touch": {"type": "string", "description": "Текст первого касания (короткое сообщение)"},
        "script": {"type": "array", "items": {"type": "string"}, "description": "Тезисы сценария разговора"},
        "offer_fit": {"type": "array", "items": {
            "type": "object",
            "properties": {"pain": {"type": "string"}, "solution": {"type": "string"}},
            "required": ["pain", "solution"]},
            "description": "Связка: боль компании -> что закрывает on-prem LLM / RAG"},
        "objections": {"type": "array", "items": {
            "type": "object",
            "properties": {"q": {"type": "string"}, "a": {"type": "string"}},
            "required": ["q", "a"]},
            "description": "Возможные возражения и ответы"},
        "next_step": {"type": "string", "description": "Следующий шаг и срок"},
        "sources": {"type": "string", "description": "Источники (опц.)"},
    },
    "required": ["company", "offer_fit"],
}

COMBINED_SYSTEM = (
    "Ты — аналитик B2B-продаж для вендора, который продаёт: (1) LLM на ЛОКАЛЬНЫХ "
    "серверах клиента — данные не уходят в облако, снимает риски 152-ФЗ; "
    "(2) RAG-вопрос-ответ по внутренней документации/нормативке. Тебе дают ОДНУ компанию.\n"
    "ПОРЯДОК (ресёрч делаешь ОДИН раз, оба документа — из одних и тех же находок):\n"
    "1) deep_research(company_name, inn) — официальная база: выручка (ГИР БО), карточка "
    "ЕГРЮЛ/ЛПР/ОКВЭД/адрес (Dadata), контакты (Checko).\n"
    "2) WebSearch + WebFetch: профиль, ЧИСЛЕННОСТЬ, собственник/бенефициар, актуальное "
    "руководство, госзакупки/тендеры/суды/ФАС, упоминания в СМИ за 5 лет. КАЖДУЮ ссылку "
    "раздела «СМИ» открой WebFetch'ем и убедись, что страница реальна и про эту компанию; "
    "выдуманные/битые НЕ включай.\n"
    "3) Вызови save_dossier_docx: 5 разделов (профиль; масштаб и показатели; собственник/ЛПР/"
    "контакты; боли отрасли -> что закрываем LLM/RAG с акцентом 152-ФЗ/on-prem; СМИ + Источники).\n"
    "4) На ТЕХ ЖЕ данных вызови save_strategy_docx: план коммуникации (summary; channel — через "
    "кого и как заходить к ЛПР; first_touch — текст первого касания; script — тезисы разговора; "
    "offer_fit — связки боль->решение on-prem LLM/RAG; objections — возражения и ответы; next_step).\n"
    "ТРЕБОВАНИЯ: каждый документ — СТРОГО ≤1 страница, по делу, цифры со ссылкой/источником. "
    "Бухгалтерскую выручку и оборот по счёту НЕ путать. Заверши кратким резюме."
)

ALLOWED = [
    "mcp__research__deep_research",
    "mcp__research__save_dossier_docx",
    "mcp__research__save_strategy_docx",
    "WebSearch", "WebFetch",
]


def _handle(lead):
    return {
        "company_name": lead.get("name") or "",
        "inn": str(lead.get("_inn") or "").strip(),
        "aspects": "профиль, численность, ЛПР, боли под on-prem LLM/RAG, СМИ за 5 лет",
    }


async def _research_one(lead, idx, d_tmp, s_tmp, model):
    """Один ресёрч-проход: рендерит оба .docx во временные пути. Возвращает $-стоимость."""
    import anyio  # noqa: F401  (нужен косвенно SDK/CRA)
    import company_research_agent as CRA
    from claude_agent_sdk import (
        tool, create_sdk_mcp_server, ClaudeAgentOptions, ClaudeSDKClient,
        ResultMessage, AssistantMessage, TextBlock, ToolUseBlock,
    )

    # Инструменты сохранения — замыкания на временные пути ЭТОЙ компании (потокобезопасно).
    @tool("save_dossier_docx", "Сохранить готовое досье (≤1 стр.). Ссылки в mentions — только проверенные.", DOSSIER_SCHEMA)
    async def _save_dossier(args):
        a = dict(args)
        a["filename"] = f"_orq_d_{idx}.docx"          # уникальное имя -> кладём в «Загрузки», затем переносим
        dl = await asyncio.to_thread(CRA._write_dossier_docx, a)
        await asyncio.to_thread(shutil.move, dl, d_tmp)
        return {"content": [{"type": "text", "text": "досье сохранено"}]}

    @tool("save_strategy_docx", "Сохранить план коммуникации (≤1 стр.) на основе тех же находок.", STRATEGY_SCHEMA)
    async def _save_strategy(args):
        await asyncio.to_thread(_strategy_doc, dict(args), s_tmp)
        return {"content": [{"type": "text", "text": "стратегия сохранена"}]}

    server = create_sdk_mcp_server(
        name="research", version="3.0.0",
        tools=[CRA.deep_research, _save_dossier, _save_strategy],
    )
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=COMBINED_SYSTEM,
        mcp_servers={"research": server},
        allowed_tools=ALLOWED,
        disallowed_tools=["Bash", "Edit", "Write", "NotebookEdit"],
        permission_mode="bypassPermissions",
        setting_sources=[],
        max_turns=60,
    )
    h = _handle(lead)
    handoff = (
        "Подготовь ДОСЬЕ и СТРАТЕГИЮ коммуникации, сохрани оба .docx. Значения для инструментов:\n"
        f"  company_name = {h['company_name']!r}\n"
        f"  inn          = {h['inn']!r}\n"
        f"  aspects      = {h['aspects']!r}\n"
        "Сначала deep_research, потом веб-ресёрч (СМИ — только проверенные ссылки), "
        "затем save_dossier_docx и save_strategy_docx на одних и тех же данных."
    )
    cost = 0.0
    async with ClaudeSDKClient(options=options) as client:
        await client.query(handoff)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        print(f"    [{idx}] → {getattr(block, 'name', '')}")
            elif isinstance(message, ResultMessage):
                if getattr(message, "total_cost_usd", None):
                    cost = message.total_cost_usd

    # подстраховка: если агент не сохранил — кладём заготовку, чтобы upload не упал
    if not os.path.exists(d_tmp):
        await asyncio.to_thread(DO.generate_dossier, lead, d_tmp)
    if not os.path.exists(s_tmp):
        await asyncio.to_thread(DO.generate_strategy, lead, s_tmp)
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
        description="Полная цепочка: сбор (RusProfile) + ресёрч (досье/стратегия) в папки Яндекс Диска")
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

    print("\n=== ФАЗА 2: ресёрч (досье + стратегия) ===")
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
            s_tmp = os.path.join(tmp, f"{idx}_s.docx")
            cost = 0.0
            if a.dry_run:
                try:
                    await asyncio.to_thread(DO.generate_dossier, lead, d_tmp)
                    await asyncio.to_thread(DO.generate_strategy, lead, s_tmp)
                except Exception as e:
                    print(f"  [!] {lead.get('name')}: {e}")
                    return {"name": lead.get("name"), "ok": False, "cost": cost}
            else:
                # ресёрч-агент изредка падает аварийно (0xC0000409 = OOM Node при нехватке
                # RAM / конкуренции claude CLI) — ретраим до 3 раз с паузой
                err = None
                for attempt in range(3):
                    try:
                        cost = await _research_one(lead, idx, d_tmp, s_tmp, a.model)
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
                    await asyncio.to_thread(DO._upload, s_tmp, f"{comp_dir}/стратегия_коммуникации_{dn}.docx", a.account, True)
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
