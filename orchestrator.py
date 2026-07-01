# -*- coding: utf-8 -*-
r"""
Оркестратор второй фазы: по каждой собранной компании — глубокий ресёрч и
ДВА пресейл-документа, которые ПЕРЕЗАПИСЫВАЮТ заготовки в папке компании на
Яндекс Диске (папки уже создал первый агент / disk_organize):
  - <компания>_карта_бизнес-процессов.docx
  - <компания>_карта_ролей_и_контактов_пресейл.docx
Формат документов и логика ресёрча описаны в company_research_agent.py (CRA);
оркестратор переиспользует его рендереры, схемы и системный промпт.

Архитектура (детерминированный Python-оркестратор, НЕ LLM-оркестратор):
  leads.json (тот же, что у агента 1)
    └─ по каждой компании, пул из --workers параллельно:
         1 ресёрч-сессия (opus + WebSearch, ОДИН раз) рендерит ОБА .docx во temp
         → upload во УЖЕ существующую папку disk:/Лиды/<отрасль>/<полнота>/<компания>/

Путь на Диске считается ровно теми же функциями disk_organize, что и у агента 1,
поэтому файлы ложатся в его папки (mkdir идемпотентный — папка уже есть).

ПОЛНАЯ ЦЕПОЧКА одной командой (сам запускает оба этапа; число компаний = предписание
первого агента, по умолчанию 200 в отрасли — если не задано другое):
  py orchestrator.py --industries mining               # собрать 200 mining и по ВСЕМ сделать оба документа
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
import glob
import json
import os
import shutil
import sys
import tempfile
import warnings

# Косметический RequestsDependencyWarning (chardet 7.x вне диапазона requests; ставится Crawl4AI,
# на работу не влияет) — глушим ДО первого импорта requests. Фильтр по тексту, без импорта requests.
warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)

import disk_organize as DO  # пути на Диске + upload + заглушки (stdlib, без сети при импорте)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# Имена выходных файлов в папке компании на Диске.
def _doc_names(dn):
    return (f"{dn}_карта_бизнес-процессов.docx",
            f"{dn}_карта_ролей_и_контактов_пресейл.docx",
            f"{dn}_презентация_Telepath.pptx")


# Инструменты ресёрч-агента: оба документа (формат/схемы/промпт — из CRA).
ALLOWED = [
    "mcp__research__deep_research",
    "mcp__research__save_process_map_docx",
    "mcp__research__save_roles_contacts_docx",
    "WebSearch", "WebFetch",
]

# --- Стадия презентации (третий деливерабл, .pptx через официальный скилл pptx) ---
ASSETS_DIR = os.path.join(SCRIPTS, "assets")
LOGO_PNG = os.path.join(ASSETS_DIR, "citrt_logo.png")        # логотип АО «ЦИТ РТ»
PHOTO_PNG = os.path.join(ASSETS_DIR, "bulat_zamaliev.png")   # фото Булата Замалиева

# Портативные инструменты скилла pptx на D: (LibreOffice/Poppler поставлены туда — C: переполнен).
# Гейт и агентная сессия находят soffice/pdftoppm и здесь, а не только в системном PATH.
_LO_DIRS = [r"D:\Apps\LibreOffice\program"]
_POPPLER_DIRS = glob.glob(r"D:\Apps\poppler\poppler-*\Library\bin") or [r"D:\Apps\poppler"]


def _which_tool(name, extra_dirs):
    """Найти CLI-инструмент (soffice/pdftoppm): сначала в PATH, потом в портативных папках на D:."""
    p = shutil.which(name) or shutil.which(name + ".exe")
    if p:
        return p
    for d in extra_dirs:
        cand = os.path.join(d, name + ".exe")
        if os.path.exists(cand):
            return cand
    return None


def _handle(lead):
    aspects = "профиль, процессы as-is, точки внедрения ИИ, оргструктура, ЛПР, контакты, СМИ за 5 лет"
    # website из ФАЗЫ 1 -> подсказка-домен движку deep_research (сайт-коллектор без зависимости от поиска)
    site = (lead.get("website") or "").strip()
    if site:
        aspects += f"; сайт: {site}"
    return {
        "company_name": lead.get("name") or "",
        "inn": str(lead.get("_inn") or "").strip(),
        "aspects": aspects,
    }


async def _research_one(lead, idx, d_tmp, s_tmp, model):
    """Один ресёрч-проход. ПРЕД-ЗАПУСК движка deep_research ДО сессии писателя (без
    вложенных SDK-сессий внутри тула — именно вложенность сбивала писателя), затем
    писатель форматирует находки и СОХРАНЯЕТ оба .docx. Возвращает (стоимость, находки):
    находки нужны стадии презентации для формулировки боли заказчика (слайд 3).
    d_tmp = карта бизнес-процессов, s_tmp = карта ролей и контактов."""
    import anyio  # noqa: F401  (нужен косвенно SDK/CRA)
    import company_research_agent as CRA
    import deep_research_engine as DRE
    from claude_agent_sdk import (
        tool, create_sdk_mcp_server, ClaudeAgentOptions, ClaudeSDKClient,
        ResultMessage, AssistantMessage, TextBlock, ToolUseBlock,
    )

    # Инструменты сохранения — замыкания на временные пути ЭТОЙ компании (потокобезопасно).
    @tool("save_process_map_docx",
          "Сохранить КАРТУ БИЗНЕС-ПРОЦЕССОВ. Ссылки/контакты — только проверенные.",
          CRA.PROCESS_MAP_SCHEMA)
    async def _save_process_map(args):
        await asyncio.to_thread(CRA._write_process_map_docx, dict(args), d_tmp)
        return {"content": [{"type": "text", "text": "карта бизнес-процессов сохранена"}]}

    @tool("save_roles_contacts_docx",
          "Сохранить КАРТУ РОЛЕЙ И КОНТАКТОВ · ПРЕСЕЙЛ. Только официальные публичные контакты.",
          CRA.ROLES_CONTACTS_SCHEMA)
    async def _save_roles_contacts(args):
        await asyncio.to_thread(CRA._write_roles_contacts_docx, dict(args), s_tmp)
        return {"content": [{"type": "text", "text": "карта ролей и контактов сохранена"}]}

    server = create_sdk_mcp_server(
        name="research", version="5.0.0",
        tools=[_save_process_map, _save_roles_contacts],   # БЕЗ deep_research — он выполнен заранее
    )
    h = _handle(lead)

    # ПРЕД-ЗАПУСК движка: на верхнем уровне, НЕ внутри сессии писателя -> без вложенных
    # SDK-вызовов. Движок отдаёт готовые находки (филиалы+телефоны, соцсети, контакты,
    # официалка), у строк — source URL.
    print(f"    [{idx}] deep_research (движок) ...")
    try:
        findings = await DRE.deep_research(h["company_name"], h["inn"], h["aspects"])
    except Exception as e:
        print(f"    [{idx}] deep_research engine error: {str(e)[:90]}")
        findings = ""

    options = ClaudeAgentOptions(
        model=model,
        system_prompt=CRA.PRESALE_SYSTEM,
        mcp_servers={"research": server},
        allowed_tools=[
            "mcp__research__save_process_map_docx",
            "mcp__research__save_roles_contacts_docx",
            "WebSearch", "WebFetch",
        ],
        disallowed_tools=["Bash", "Edit", "Write", "NotebookEdit"],
        permission_mode="bypassPermissions",
        setting_sources=[],
        max_turns=80,
    )
    handoff = (
        "deep_research УЖЕ ВЫПОЛНЕН отдельным движком — НЕ запускай его заново. Вот "
        "собранные находки (у строк есть source URL):\n\n"
        f"{findings}\n\n"
        "ЗАДАЧА: на основе ЭТИХ находок (плюс при необходимости точечная доверка через "
        "WebFetch/WebSearch по оставшимся пробелам — ИТ/тендерный контакт) заполни схемы и "
        "СОХРАНИ ОБА документа: вызови save_process_map_docx И save_roles_contacts_docx. "
        "В карту ролей ОБЯЗАТЕЛЬНО перенеси таблицу филиалов (директор+телефон), соцсети и "
        "официальные контакты ИЗ находок — не пиши «не подтверждено» там, где данные есть.\n"
        f"  company_name = {h['company_name']!r}\n"
        f"  inn          = {h['inn']!r}\n"
        "ВАЖНО: работа НЕ выполнена, пока ты не вызвал ОБА инструмента save_*_docx. "
        "Текстовый ответ результатом НЕ является."
    )

    cost = 0.0

    async def _drive(msg):
        nonlocal cost
        await client.query(msg)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        print(f"    [{idx}] → {getattr(block, 'name', '')}")
            elif isinstance(message, ResultMessage):
                if getattr(message, "total_cost_usd", None):
                    cost += message.total_cost_usd

    async with ClaudeSDKClient(options=options) as client:
        await _drive(handoff)
        # нудж-ретрай: если какой-то документ не сохранён — потребовать сохранить
        for _ in range(2):
            missing = []
            if not os.path.exists(d_tmp):
                missing.append("save_process_map_docx (карта бизнес-процессов)")
            if not os.path.exists(s_tmp):
                missing.append("save_roles_contacts_docx (карта ролей и контактов)")
            if not missing:
                break
            await _drive("Ты НЕ сохранил: " + "; ".join(missing) + ". Немедленно вызови "
                         "недостающий инструмент save_*_docx с заполненными данными из "
                         "находок. Больше ничего не делай.")
    return cost, findings


def _presentation_prereqs():
    """Готовность стадии презентации. Возвращает (ok: bool, reason: str).
    reason — человекочитаемая русская причина пропуска (пусто при ok=True).
    Деградируем мягко: НИКОГДА не валим компанию — просто пропускаем стадию.
      - оба ассета-PNG (логотип + фото) на месте;
      - на PATH есть soffice (LibreOffice) — скилл pptx им рендерит слайды для самопроверки;
      - на PATH есть node — скилл pptx генерит .pptx через pptxgenjs;
      - скилл pptx реально установлен (~/.claude/skills/pptx/SKILL.md или проектный .claude/skills)."""
    if not os.path.exists(LOGO_PNG):
        return False, "нет assets/citrt_logo.png"
    if not os.path.exists(PHOTO_PNG):
        return False, "нет assets/bulat_zamaliev.png"
    if not _which_tool("soffice", _LO_DIRS):
        return False, "не найден soffice/LibreOffice (поставь LibreOffice или положи в D:\\Apps\\LibreOffice)"
    if not _which_tool("pdftoppm", _POPPLER_DIRS):
        return False, "не найден pdftoppm/Poppler (поставь Poppler или положи в D:\\Apps\\poppler)"
    if not (shutil.which("node") or shutil.which("node.exe")):
        return False, "не найден node на PATH (нужен Node.js + npm-пакет pptxgenjs для скилла pptx)"
    # скилл pptx должен быть обнаружим: пользовательский каталог или проектный .claude/skills
    user_skill = os.path.join(os.path.expanduser("~"), ".claude", "skills", "pptx", "SKILL.md")
    proj_skill = os.path.join(SCRIPTS, ".claude", "skills", "pptx", "SKILL.md")
    if not (os.path.exists(user_skill) or os.path.exists(proj_skill)):
        return False, ("не установлен официальный скилл pptx "
                       "(положи его в ~/.claude/skills/pptx или в .claude/skills/pptx проекта; "
                       "источник: github.com/anthropics/skills/tree/main/skills/pptx)")
    return True, ""


def _main_pain(findings):
    """Грубо вытащить «главную боль» из находок движка для слайда 3 (оффер
    адаптируется под заказчика). Берём первые непустые строки с маркерами боли;
    если не нашли — отдаём пусто, агент сформулирует боль сам по отрасли."""
    if not findings:
        return ""
    import re
    hits = []
    for line in str(findings).splitlines():
        s = line.strip(" -•*#\t")
        if not s:
            continue
        if re.search(r"бол[ьи]|узк|вручную|разрозн|дорог|рутин|задержк|ошибк|неэффект", s, re.I):
            hits.append(s)
        if len(hits) >= 4:
            break
    return "\n".join(hits)[:1200]


async def _presentation_one(lead, idx, p_tmp, model, findings=""):
    """Третий деливерабл: 3-слайдовая брендированная .pptx через ОФИЦИАЛЬНЫЙ скилл pptx.
    Это ОТДЕЛЬНАЯ агентная сессия (code-execution/Bash + скилл pptx), НЕ python-рендерер.
    Слайды 1–2 фиксированы, слайд 3 (оффер) адаптируется под боль заказчика из находок.
    Пишет итог в p_tmp. Возвращает (cost, remark): remark — фактическое «замечание»
    про Булата/ЦИТ РТ, которое требует промпт (его НЕ глушим — печатаем в лог)."""
    import company_research_agent as CRA
    from claude_agent_sdk import (
        ClaudeAgentOptions, ClaudeSDKClient,
        ResultMessage, AssistantMessage, TextBlock, ToolUseBlock,
    )

    h = _handle(lead)
    industry = DO.industry_folder(lead) if hasattr(DO, "industry_folder") else (lead.get("niche") or "")
    pain = _main_pain(findings)

    # Скилл pptx зовёт python/soffice/pdftoppm по имени из своих скриптов. На этой машине:
    #   - soffice/pdftoppm лежат на D: (вне системного PATH);
    #   - `python` в PATH — это Store-заглушка WindowsApps, а не настоящий интерпретатор.
    # Прокидываем КАТАЛОГ НАСТОЯЩЕГО python (sys.executable) + папки D: в начало PATH процесса;
    # дочерняя claude-сессия наследует это окружение, поэтому скилл найдёт рабочие бинарники.
    _pydir = os.path.dirname(sys.executable)
    _extra = [d for d in ([_pydir] + _LO_DIRS + _POPPLER_DIRS) if d and os.path.isdir(d)]
    if _extra:
        os.environ["PATH"] = os.pathsep.join(_extra) + os.pathsep + os.environ.get("PATH", "")

    # Опции сессии. setting_sources/skills/cwd/add_dirs — поля современного SDK; строим
    # защитно: если установлен старый SDK без какого-то kwargs — стадию мягко пропустим
    # (не валим компанию). allowed_tools включает Bash + ФС-инструменты, нужные скиллу
    # pptx (python/node/soffice, чтение ассетов, запись .pptx, рендер для самопроверки).
    opt_kwargs = dict(
        model=model,
        system_prompt=CRA.PRESENTATION_SYSTEM,
        allowed_tools=["Bash", "Read", "Write", "Edit", "Glob"],
        permission_mode="bypassPermissions",   # headless: без зависаний на аппруве
        setting_sources=["user", "project"],   # ОПТ-ИН в обнаружение скиллов (.claude/skills)
        skills=["pptx"],                        # включить именно официальный скилл pptx
        cwd=os.path.dirname(p_tmp) or SCRIPTS,  # рабочая папка = temp компании (туда же пишет вывод)
        add_dirs=[ASSETS_DIR, os.path.dirname(p_tmp) or SCRIPTS],  # доступ к ассетам и temp
        max_turns=80,
    )
    try:
        options = ClaudeAgentOptions(**opt_kwargs)
    except TypeError as e:
        # старый claude-agent-sdk без skills/setting_sources/add_dirs -> пропустить стадию
        print(f"    [{idx}] [presentation] пропущено: SDK не поддерживает опции скиллов ({str(e)[:80]})")
        return 0.0, ""

    user_msg = (
        "Сделай 3-слайдовую презентацию .pptx строго по системному промпту. "
        "ВХОДНЫЕ ДАННЫЕ:\n"
        f"- НАЗВАНИЕ КОМПАНИИ-ЗАКАЗЧИКА: {h['company_name']}\n"
        f"- ЧЕМ ЗАНИМАЕТСЯ / ОТРАСЛЬ: {industry or '(уточни по названию)'}\n"
        f"- ГЛАВНАЯ БОЛЬ (если знаю): {pain or '(сформулируй сам по отрасли заказчика)'}\n\n"
        "ВЛОЖЕНИЯ (используй ИМЕННО эти файлы, абсолютные пути):\n"
        f"- Логотип ЦИТ РТ (PNG): {LOGO_PNG}\n"
        f"- Фото Булата Замалиева (PNG): {PHOTO_PNG}\n\n"
        f"Итоговый файл .pptx сохрани СТРОГО по пути: {p_tmp}\n"
        "Слайды 1–2 — фиксированы; слайд 3 (оффер) — адаптируй под заказчика и его боль. "
        "В конце ОБЯЗАТЕЛЬНО приведи отдельным блоком «ЗАМЕЧАНИЕ:» — расхождение по должности "
        "Булата Замалиева (ЦИТ РТ vs Минцифры РТ) и проверяемую альтернативу."
    )

    cost = 0.0
    remark = ""
    summary = ""
    async with ClaudeSDKClient(options=options) as client:
        await client.query(user_msg)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        summary += block.text
                    elif isinstance(block, ToolUseBlock):
                        print(f"    [{idx}] → {getattr(block, 'name', '')}")
            elif isinstance(message, ResultMessage):
                if message.result:
                    summary = message.result
                if getattr(message, "total_cost_usd", None):
                    cost += message.total_cost_usd

    # вытащить фактическое «замечание» (его НЕ подавляем — это требование промпта)
    if summary:
        import re
        m = re.search(r"замечани[ея][:\s].*", summary, re.I | re.DOTALL)
        remark = (m.group(0) if m else summary).strip()[:1500]
    return cost, remark


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
        description="Полная цепочка: сбор (RusProfile) + ресёрч (2 пресейл-документа) в папки Яндекс Диска")
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
    # 3-я стадия (one-page .pptx через скилл pptx) ВКЛючена ПО УМОЛЧАНИЮ. Отключить:
    # --no-presentation или GEN_PRESENTATION=0. Если предусловия (assets/*.png + soffice +
    # node + установленный скилл pptx) не выполнены — стадия мягко пропускается, два .docx
    # при этом делаются как обычно.
    _pp_default = (os.environ.get("GEN_PRESENTATION", "1").strip().lower()
                   not in ("0", "false", "no", "off", "нет"))
    ap.add_argument("--presentation", dest="presentation", action="store_true",
                    default=_pp_default,
                    help="3-я стадия .pptx через скилл pptx — ВКЛючена по умолчанию")
    ap.add_argument("--no-presentation", dest="presentation", action="store_false",
                    help="ОТКЛЮЧИТЬ 3-ю стадию (.pptx-презентацию)")
    a = ap.parse_args()
    # Удобство: ОТРАСЛЬ можно указать позиционно (py orchestrator.py mining) — приравниваем
    # к --industries, если позиционный аргумент — не существующий путь, а ключ(и) отрасли.
    if not a.industries and a.leads and not os.path.exists(a.leads):
        import source_rusprofile as RP
        _keys = [s.strip() for s in a.leads.split(",") if s.strip()]
        if _keys and all(k in RP.INDUSTRY for k in _keys):
            a.industries, a.leads = ",".join(_keys), None
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

    print("\n=== ФАЗА 2: ресёрч (карта бизнес-процессов + карта ролей и контактов) ===")
    if not a.dry_run:
        _lo, _hi, _what = ((3 * len(sel), int(4.5 * len(sel)), "2 .docx + .pptx")
                           if a.presentation else (len(sel), 2 * len(sel), "2 .docx"))
        print(f"[оценка] {len(sel)} компаний = ~${_lo}–${_hi} ({a.model}, {_what} на компанию). "
              "Число = --count (по умолч. 200).")
        free = _free_ram_gb()                         # каждый ресёрч = свой claude CLI (Node, сотни МБ)
        if free is not None and free < 3.0 and a.workers > 1:
            print(f"[ОЗУ] свободно ~{free:.1f} ГБ — снижаю параллелизм ресёрча до 1 "
                  "(несколько claude CLI при нехватке памяти падают 0xC0000409). "
                  "Освободи RAM или задай --workers вручную.")
            a.workers = 1

    # Стадия презентации опциональна и включается флагом --presentation / GEN_PRESENTATION.
    # Готовность проверяем ОДИН раз заранее — иначе один и тот же скип спамил бы по компаниям.
    gen_pptx = bool(a.presentation)
    if gen_pptx:
        ok_pp, why_pp = _presentation_prereqs()
        if not ok_pp:
            print(f"[presentation] стадия отключена: {why_pp}. Два .docx делаются как обычно.")
            gen_pptx = False
        else:
            print("[presentation] стадия включена: по каждой компании будет 3-слайдовая .pptx.")
    # Транзитная рабочая папка. Файлы здесь ВРЕМЕННЫЕ: после заливки на Я.Диск папка удаляется
    # (см. конец) — на компьютере ничего не остаётся. Предпочитаем D: (на C: мало места, а стадия
    # .pptx пишет сюда же pdf+jpg на каждую компанию); если D: нет — системный %TEMP%.
    _tmp_dir = None
    if os.path.isdir("D:\\"):
        _tmp_dir = os.path.join("D:\\", "orq_tmp")
        os.makedirs(_tmp_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="orq_", dir=_tmp_dir)

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
            comp_tmp = os.path.join(tmp, str(idx))       # СВОЯ папка на компанию: изоляция .pptx-рендера
            os.makedirs(comp_tmp, exist_ok=True)         # (slide-*.jpg/pdf не пересекаются между воркерами)
            d_tmp = os.path.join(comp_tmp, "d.docx")
            s_tmp = os.path.join(comp_tmp, "s.docx")
            p_tmp = os.path.join(comp_tmp, "p.pptx")     # презентация (третий деливерабл)
            cost = 0.0
            findings = ""
            have_pptx = False           # готова ли реальная .pptx у этой компании
            try:
                if a.dry_run:
                    try:
                        await asyncio.to_thread(DO.generate_dossier, lead, d_tmp)
                        await asyncio.to_thread(DO.generate_strategy, lead, s_tmp)
                        if gen_pptx:        # паритет с .docx: в dry-run кладём валидную болванку .pptx
                            await asyncio.to_thread(DO.generate_presentation, lead, p_tmp)
                            have_pptx = os.path.exists(p_tmp) and os.path.getsize(p_tmp) > 5000
                    except Exception as e:
                        print(f"  [!] {lead.get('name')}: {e}")
                        return {"name": lead.get("name"), "ok": False, "cost": cost}
                else:
                    # ресёрч+писатель: ретраим до 3 раз ПО ФАКТУ отсутствия двух .docx — ловим И
                    # исключения (0xC0000409 OOM / сеть), И «тихие» сбои, когда сессия вернулась БЕЗ
                    # исключения, но писатель ничего не сохранил (idle-timeout стрима / "error result").
                    # Новая попытка = свежая сессия; частичные файлы чистим, чтобы стартовать начисто.
                    ok_docx = False
                    for attempt in range(3):
                        try:
                            cost, findings = await _research_one(lead, idx, d_tmp, s_tmp, a.model)
                        except Exception as e:
                            print(f"  [retry {attempt + 1}/3] {lead.get('name')}: {str(e)[:70]}")
                        if (os.path.exists(d_tmp) and os.path.getsize(d_tmp) > 5000
                                and os.path.exists(s_tmp) and os.path.getsize(s_tmp) > 5000):
                            ok_docx = True
                            break
                        if attempt < 2:
                            bp = os.path.getsize(d_tmp) if os.path.exists(d_tmp) else 0
                            rc = os.path.getsize(s_tmp) if os.path.exists(s_tmp) else 0
                            print(f"  [retry {attempt + 1}/3] {lead.get('name')}: писатель не сохранил "
                                  f"оба .docx (bp={bp}б, rc={rc}б) — повтор")
                            for _f in (d_tmp, s_tmp):
                                if os.path.exists(_f):
                                    try:
                                        os.remove(_f)
                                    except OSError:
                                        pass
                            await asyncio.sleep(4)
                    if not ok_docx:
                        print(f"  [!] {lead.get('name')}: писатель не сохранил оба документа за 3 попытки — пропуск")
                        return {"name": lead.get("name"), "ok": False, "cost": cost}

                    # Третья стадия (опц.). Сбой НЕ валит компанию — два .docx уже готовы.
                    # Ретраим ПО ФАКТУ отсутствия .pptx (>5КБ): ловим и исключения (0xC0000409),
                    # и «тихие» сбои — idle-timeout стрима / "error result" приходят ТЕКСТОМ, а не
                    # исключением. Есть валидная .pptx после попытки — берём, не перегенерируем.
                    if gen_pptx:
                        remark = ""
                        for attempt in range(3):
                            try:
                                p_cost, remark = await _presentation_one(lead, idx, p_tmp, a.model, findings)
                                cost += p_cost
                            except Exception as e:
                                print(f"    [{idx}] [presentation retry {attempt + 1}/3]: {str(e)[:70]}")
                            if os.path.exists(p_tmp) and os.path.getsize(p_tmp) > 5000:
                                break                       # дек готов
                            if attempt < 2:
                                print(f"    [{idx}] [presentation retry {attempt + 1}/3]: .pptx не получена — повтор")
                                if os.path.exists(p_tmp):   # убрать недописанный перед повтором
                                    try:
                                        os.remove(p_tmp)
                                    except OSError:
                                        pass
                                await asyncio.sleep(4)
                        have_pptx = os.path.exists(p_tmp) and os.path.getsize(p_tmp) > 5000
                        # «замечание» по Булату печатаем ТОЛЬКО при реальном деке (иначе это текст ошибки API)
                        if have_pptx and remark and "API Error" not in remark and "error result" not in remark:
                            print(f"    [{idx}] [presentation] замечание: {remark[:300]}")
                        if not have_pptx:
                            print(f"    [{idx}] [presentation] .pptx не получена (>5КБ) за 3 попытки — зальём только два .docx")

                comp_dir = _company_dir(lead, a.base, dup_names)
                dn = DO._safe(lead.get("name"))
                bp_name, rc_name, pptx_name = _doc_names(dn)
                if not a.no_upload:
                    try:
                        await asyncio.to_thread(DO._mkdir, comp_dir, a.account)  # папка обычно уже есть
                        await asyncio.to_thread(DO._upload, d_tmp, f"{comp_dir}/{bp_name}", a.account, True)
                        await asyncio.to_thread(DO._upload, s_tmp, f"{comp_dir}/{rc_name}", a.account, True)
                        if have_pptx:       # презентацию грузим в ту же папку компании (если получилась)
                            await asyncio.to_thread(DO._upload, p_tmp, f"{comp_dir}/{pptx_name}", a.account, True)
                    except Exception as e:
                        print(f"  [!] upload {lead.get('name')}: {e}")
                        return {"name": lead.get("name"), "ok": False, "cost": cost}
                print(f"  ✓ [{idx}] {lead.get('name')[:40]} -> {comp_dir}"
                      + ("  +pptx" if have_pptx else "")
                      + (f"  (${cost:.2f})" if cost else ""))
                return {"name": lead.get("name"), "ok": True, "cost": cost, "dir": comp_dir,
                        "pptx": have_pptx}
            finally:
                # транзит компании чистим СРАЗУ после заливки (не копим все 200 до конца —
                # минимальный локальный след). При --no-upload оставляем: файлы смотрят локально.
                if not a.no_upload:
                    shutil.rmtree(comp_tmp, ignore_errors=True)

    results = await asyncio.gather(*(process(i, l) for i, l in enumerate(sel)))
    ok = [r for r in results if r and r.get("ok")]
    total = sum(r.get("cost") or 0 for r in results if r)
    n_pptx = sum(1 for r in ok if r.get("pptx"))
    n_files = 2 * len(ok) + n_pptx           # два .docx на компанию + презентация там, где получилась
    print(f"\n[ГОТОВО] компаний: {len(ok)}/{len(sel)} | файлов: {n_files}"
          + (f" (в т.ч. {n_pptx} презентаций)" if n_pptx else "") + " "
          + ("(локально, без Диска) " if a.no_upload else f"в {a.base} ")
          + (f"| стоимость ~${total:.2f}" if total else "| dry-run, $0"))
    if a.no_upload:
        print(f"[локально] .docx/.pptx во временной папке: {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
