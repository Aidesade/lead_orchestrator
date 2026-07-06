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
доступ к Диску — env YANDEX_DISK_TOKEN (python-коннектор connectors/yadisk_client).
dry-run не требует ничего сверх stdlib.
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
import time
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


async def _research_one(lead, idx, d_tmp, s_tmp, model, person_enrich=True):
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
    # официалка), у строк — source URL. Результат кэшируется на диске: ретрай писателя
    # и повторный прогон не гоняют (и не оплачивают) deep-research заново.
    ttl_h = float(os.environ.get("ORQ_FINDINGS_TTL_H", "72"))
    findings = _cached_findings(h["company_name"], h["inn"], ttl_h=ttl_h)
    if findings:
        print(f"    [{idx}] deep_research: находки из кэша (моложе {ttl_h:g} ч) — движок пропущен")
    else:
        print(f"    [{idx}] deep_research (движок) ...")
        try:
            findings = await DRE.deep_research(h["company_name"], h["inn"], h["aspects"])
        except Exception as e:
            print(f"    [{idx}] deep_research engine error: {str(e)[:90]}")
            findings = ""
        cp = _findings_cache_path(h["company_name"], h["inn"])
        if cp and findings and len(findings) > 200:
            try:
                os.makedirs(os.path.dirname(cp), exist_ok=True)
                open(cp, "w", encoding="utf-8").write(findings)
            except OSError:
                pass

    # Обогащение ЛПР прямыми контактами — детерминированно, БЕЗ вложенной SDK-сессии
    # (как пред-запуск движка). Легитимные источники (Dadata/Checko/сайт/MX/SMTP);
    # «пробив»/утечки исключены в самом person_enrich (DENY_SOURCES). SMTP-проверка email
    # и соц-поиск — под env (PERSON_VERIFY_EMAIL / PERSON_SOCIAL), по умолчанию выключены,
    # чтобы 200-прогон был быстрым и не долбил чужие серверы.
    if person_enrich and lead.get("contact_person") and lead.get("_inn"):
        try:
            import person_enrich as PEN
            _pv = os.environ.get("PERSON_VERIFY_EMAIL", "").strip().lower() in ("1", "true", "yes", "on", "да")
            _ps = os.environ.get("PERSON_SOCIAL", "").strip().lower() in ("1", "true", "yes", "on", "да")
            pe = await asyncio.to_thread(
                PEN.enrich_person, lead["contact_person"], lead["_inn"],
                domain=lead.get("website"), verify_email=_pv, social=_ps)
            findings = (findings or "") + "\n\n" + PEN.format_findings_block(pe)
            print(f"    [{idx}] person_enrich: email {len(pe['contacts']['work_emails'])}, "
                  f"тел {len(pe['contacts']['work_phones'])}"
                  + ("" if pe.get("fio_confirmed") else " (ФИО ЛПР не подтв. ЕГРЮЛ)"))
        except Exception as e:
            print(f"    [{idx}] person_enrich пропущен: {str(e)[:80]}")

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
        "официальные контакты ИЗ находок — не пиши «не подтверждено» там, где данные есть. "
        "Если в находках есть блок «ПРЯМЫЕ КОНТАКТЫ ЛПР» — перенеси прямой email/телефон ЛПР "
        "в карту ролей с указанием источника и уровня доверия.\n"
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


def _collect(industries, count, min_revenue, region, headless, offscreen, base, account, json_out):
    """ФАЗА 1 (первый агент): RusProfile -> контакты -> отбор -> JSON -> папки+заготовки на Диске.
    Блокирующий (открывает Chrome; offscreen=True -> окно за экраном, не видно). Возвращает picked[]."""
    import math
    import source_rusprofile as RP
    import rusprofile_session as RPS
    import pipeline

    inds = [s.strip() for s in industries.split(",") if s.strip() in RP.INDUSTRY]
    if not inds:
        raise SystemExit("не распознаны отрасли. Доступно: " + ", ".join(sorted(RP.INDUSTRY)))
    if not os.path.exists(RPS.COOKIES_FILE):
        raise SystemExit("нет cookie RusProfile — один раз: py rusprofile_session.py --login")
    per_ind = math.ceil(count / max(1, len(inds)))

    print(f"[1/2] RusProfile: {inds} | порог >{min_revenue / 1e9:g} млрд"
          + (f" | регион {region}" if region else ""))
    leads = []
    for attempt in (1, 2):     # антибот/Chrome сбоят — вторая попытка с чистой сессией
        try:
            leads = RP.harvest(inds, min_revenue=min_revenue, per_industry=per_ind,
                               region=region, headless=headless, out_path=json_out,
                               offscreen=offscreen)
        except Exception as e:
            print(f"[1/2] сбор упал: {str(e)[:120]}")
            leads = []
        if leads:
            break
        if attempt == 1:
            print("[1/2] пусто/сбой — повтор через 15с (новая Chrome-сессия)")
            time.sleep(15)
    if not leads:
        raise SystemExit("RusProfile ничего не вернул (2 попытки) — проверь коды ОКВЭД/доступ/антибот.")
    res = {}
    for attempt in (1, 2):     # контакты с платного аккаунта; прогресс — в JSON каждые 20 карточек
        try:
            with RPS.RusProfileAuth(headless=headless, offscreen=offscreen) as rs:
                res = rs.enrich_leads(leads, only_missing=True, log=print,
                                      checkpoint=lambda: RP._save(leads, json_out))
            break
        except Exception as e:
            print(f"[1/2] сессия контактов упала: {str(e)[:120]}"
                  + (" — повтор через 10с" if attempt == 1 else " — продолжаю БЕЗ контактов RusProfile"))
            if attempt == 1:
                time.sleep(10)
    if res.get("locked"):
        raise SystemExit("Контакты RusProfile закрыты — платная сессия протухла. Один раз: "
                         f"py rusprofile_session.py --login (сырой список уже сохранён: {json_out})")
    picked = pipeline._select(leads, count, inds)
    pipeline._save(picked, json_out)
    print(f"[1/2] собрано {len(picked)} | JSON: {json_out}")
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


def _rm(*paths):
    """Удалить файлы, молча игнорируя отсутствие/ошибку ОС — чистка частичных
    результатов перед повторной попыткой."""
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def _fatal_exc(e):
    """«Смертельные» исключения, которые надо пробрасывать, а не ретраить: Ctrl+C/выход/
    отмена — в том числе ВНУТРИ BaseExceptionGroup (краху CLI-подпроцесса SDK anyio
    оборачивает исключения в группу, и голый `except Exception` её пропускал бы наверх,
    убивая весь прогон)."""
    fatal = (KeyboardInterrupt, SystemExit, asyncio.CancelledError)
    if isinstance(e, fatal):
        return True
    if isinstance(e, BaseExceptionGroup):
        return e.subgroup(fatal) is not None
    return False


async def _attempt(coro, timeout, tag):
    """Одна попытка тяжёлой SDK-стадии под таймаутом. Успех -> результат корутины
    (кортеж (стоимость, ...)); НЕсмертельный сбой/таймаут -> печатает причину и отдаёт
    None; смертельное (Ctrl+C/выход, в т.ч. в BaseExceptionGroup от anyio при крахе
    CLI) — пробрасывает."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except BaseException as e:
        if _fatal_exc(e):
            raise
        why = f"таймаут {int(timeout)}с" if isinstance(e, asyncio.TimeoutError) else str(e)[:70]
        print(f"{tag}: {why}")
        return None


async def _wait_ram(min_gb, tag, max_wait=300):
    """Бэк-прешер по памяти вместо 0xC0000409: перед тяжёлой стадией подождать, пока
    свободная RAM не поднимется до min_gb (но не дольше max_wait сек — затем едем
    дальше с предупреждением)."""
    waited = 0
    while True:
        free = _free_ram_gb()
        if free is None or free >= min_gb:
            return
        if waited == 0:
            print(f"    {tag} [ОЗУ] свободно {free:.1f} ГБ < {min_gb:g} — жду высвобождения (до {max_wait}с)")
        if waited >= max_wait:
            print(f"    {tag} [ОЗУ] так и не освободилось ({free:.1f} ГБ) — продолжаю осторожно")
            return
        await asyncio.sleep(20)
        waited += 20


def _tmp_root():
    """Базовая папка временных файлов/логов прогона: D:\\orq_tmp (C: тесный), иначе %TEMP%."""
    if os.path.isdir("D:\\"):
        d = os.path.join("D:\\", "orq_tmp")
        os.makedirs(d, exist_ok=True)
        return d
    return tempfile.gettempdir()


def _work_base(subdir):
    """Путь к постоянной папке данных прогона рядом с temp: D:\\<subdir> (на C: мало
    места) или %TEMP%\\<subdir>. Саму папку НЕ создаёт."""
    return os.path.join("D:\\" if os.path.isdir("D:\\") else tempfile.gettempdir(), subdir)


class _Tee:
    """Дублирование потока в файл: лог прогона переживает закрытую консоль и жёсткий крах."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
            except Exception:
                pass
        self.flush()

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass


def _findings_cache_path(name, inn):
    """Файл кэша находок движка (переживает прогоны). Ключ — ИНН, фолбэк — имя."""
    key = str(inn or "").strip() or DO._safe(name)[:60].replace(" ", "_")
    if not key:
        return ""
    return os.path.join(_work_base("orq_cache"), f"findings_{key}.md")


def _cached_findings(name, inn, ttl_h=None):
    """Прочитать кэш находок, если он есть, содержателен (>200 симв.) и свеж.
    ttl_h=None — возраст не проверять (например, боль для слайда 3 не протухает)."""
    p = _findings_cache_path(name, inn)
    try:
        if p and os.path.isfile(p) and os.path.getsize(p) > 200:
            if ttl_h is None or time.time() - os.path.getmtime(p) < ttl_h * 3600:
                return open(p, encoding="utf-8", errors="replace").read()
    except OSError:
        pass
    return ""


REAL_PPTX_MIN = 60000   # заготовка _make_pptx ≈ 28 КБ; реальный дек с картинками — сотни КБ


def _remote_state(comp_dir, names):
    """Резюм: какие деливераблы уже лежат на Диске. Возвращает (docx_ok, pptx_ok).
    Любая ошибка (нет папки/сеть/токен) -> (False, False): резюм просто не срабатывает,
    компания честно переделывается."""
    try:
        sizes = DO._disk_client().list_file_sizes(comp_dir)
    except Exception:
        return False, False
    bp, rc, pp = names
    docx_ok = sizes.get(bp, 0) > 5000 and sizes.get(rc, 0) > 5000
    pptx_ok = sizes.get(pp, 0) > REAL_PPTX_MIN
    return docx_ok, pptx_ok


def _outbox_dir():
    """Папка отложенной заливки (переживает прогоны): сюда падают ГОТОВЫЕ файлы,
    которые не удалось загрузить на Диск, — следующий прогон их доливает."""
    base = _work_base("orq_outbox")
    os.makedirs(base, exist_ok=True)
    return base


def _outbox_defer(name, comp_dir, pairs):
    """Сбой заливки: спрятать готовые файлы в outbox вместо удаления (деньги уже
    потрачены). pairs=[(локальный путь, имя на Диске)]. Возвращает путь записи или ''."""
    try:
        entry = os.path.join(_outbox_dir(), f"{DO._safe(name)[:60]}_{int(time.time())}")
        os.makedirs(entry, exist_ok=True)
        files = []
        for lp, rname in pairs:
            if os.path.isfile(lp) and os.path.getsize(lp) > 5000:
                shutil.move(lp, os.path.join(entry, rname))
                files.append(rname)
        if not files:
            shutil.rmtree(entry, ignore_errors=True)
            return ""
        json.dump({"comp_dir": comp_dir, "files": files, "name": name},
                  open(os.path.join(entry, "meta.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        return entry
    except Exception:
        return ""


def _flush_outbox(account=None):
    """Долить на Диск всё, что предыдущие прогоны отложили в outbox.
    Возвращает (залито_записей, осталось_записей)."""
    base = _outbox_dir()
    ok = fail = 0
    for entry in sorted(os.listdir(base)):
        d = os.path.join(base, entry)
        mp = os.path.join(d, "meta.json")
        if not os.path.isfile(mp):
            continue
        try:
            meta = json.load(open(mp, encoding="utf-8"))
            comp_dir = meta["comp_dir"]
            DO.ensure_dir(comp_dir, account, set())
            for rname in meta.get("files") or []:
                lp = os.path.join(d, rname)
                if os.path.isfile(lp):
                    DO._upload(lp, f"{comp_dir}/{rname}", account, True)
            shutil.rmtree(d, ignore_errors=True)
            ok += 1
            print(f"[outbox] долито: {meta.get('name') or entry} -> {comp_dir}")
        except Exception as e:
            fail += 1
            print(f"[outbox] не долилось {entry}: {str(e)[:90]}")
    return ok, fail


async def _drain_outbox(account, label):
    """Долить отложенное в outbox и отчитаться одной строкой (label — момент прогона:
    «долив с прошлых прогонов» на старте или «финальный долив» в конце)."""
    n_up, n_left = await asyncio.to_thread(_flush_outbox, account)
    if n_up or n_left:
        print(f"[outbox] {label}: {n_up} ок" + (f", {n_left} осталось" if n_left else ""))


async def main():
    ap = argparse.ArgumentParser(
        description="Полная цепочка: сбор (RusProfile) + ресёрч (2 пресейл-документа) в папки Яндекс Диска")
    ap.add_argument("leads", nargs="?", default=None,
                    help="готовый JSON лидов (если БЕЗ --industries)")
    # --- ФАЗА 1: сбор (первый агент). Задаёшь --industries -> оркестратор сам соберёт лиды и создаст папки ---
    ap.add_argument("--industries", default=None,
                    help="ЗАПУСТИТЬ СБОР: отрасли через запятую (mining,construction,energy,...)")
    ap.add_argument("--count", type=int, default=200,
                    help="сколько лидов собрать ВСЕГО, суммарно по отраслям (с --industries)")
    ap.add_argument("--per-industry", dest="per_industry", type=int, default=None,
                    help="сколько лидов НА КАЖДУЮ отрасль (перекрывает --count: итог = N × число отраслей)")
    ap.add_argument("--min-revenue", type=float, default=1e9, help="порог выручки, ₽ (с --industries)")
    ap.add_argument("--region", default=None, help="регион названием/аббревиатурой (с --industries)")
    ap.add_argument("--show-browser", dest="show_browser", action="store_true",
                    help="показать окно Chrome при сборе (по умолчанию СКРЫТО/headless)")
    ap.add_argument("--headless", action="store_true",
                    help=argparse.SUPPRESS)  # deprecated: headless теперь по умолчанию (флаг оставлен для совместимости)
    ap.add_argument("--out", default=None,
                    help="путь к .json лидов (с --industries); Excel больше не создаётся")
    # --- общее + ФАЗА 2: ресёрч ---
    ap.add_argument("--base", default="disk:/Лиды")
    ap.add_argument("--account", default=None)
    ap.add_argument("--workers", type=int, default=2,
                    help="параллельных ресёрч-агентов (claude CLI). При нехватке RAM авто-снижается до 1")
    ap.add_argument("--model", default="opus", help="opus (качество) | sonnet (дешевле)")
    ap.add_argument("--dry-run", action="store_true", help="ресёрч без LLM — заготовки (бесплатно)")
    ap.add_argument("--no-upload", action="store_true", help="ресёрч-файлы не грузить на Диск")
    ap.add_argument("--redo", action="store_true",
                    help="переделать даже компании, у которых на Диске уже лежат реальные "
                         "документы (по умолчанию резюм их пропускает)")
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
    _pe_default = (os.environ.get("PERSON_ENRICH", "1").strip().lower()
                   not in ("0", "false", "no", "off", "нет"))
    ap.add_argument("--person-enrich", dest="person_enrich", action="store_true", default=_pe_default,
                    help="обогащать ЛПР прямыми контактами (Dadata/Checko/сайт) — ВКЛ по умолчанию; "
                         "SMTP-проверка email — env PERSON_VERIFY_EMAIL=1, соцсети — PERSON_SOCIAL=1")
    ap.add_argument("--no-person-enrich", dest="person_enrich", action="store_false",
                    help="не обогащать ЛПР прямыми контактами")
    a = ap.parse_args()
    # Копия ВСЕГО вывода (stdout+stderr, включая трейсбеки) в файл: диагноз упавшего
    # прогона не должен зависеть от того, сохранил ли кто-то консоль.
    try:
        log_path = os.path.join(_tmp_root(), time.strftime("run_%Y%m%d_%H%M%S.log"))
        _log_fh = open(log_path, "a", encoding="utf-8", errors="replace")
        sys.stdout = _Tee(sys.stdout, _log_fh)
        sys.stderr = _Tee(sys.stderr, _log_fh)
        print(f"[лог] копия вывода: {log_path}")
    except OSError:
        pass
    # Удобство: ОТРАСЛЬ можно указать позиционно (py orchestrator.py mining) — приравниваем
    # к --industries, если позиционный аргумент — не существующий путь, а ключ(и) отрасли.
    if not a.industries and a.leads and not os.path.exists(a.leads):
        import source_rusprofile as RP
        _keys = [s.strip() for s in a.leads.split(",") if s.strip()]
        if _keys and all(k in RP.INDUSTRY for k in _keys):
            a.industries, a.leads = ",".join(_keys), None
    # «N на отрасль» перекрывает --count: итог = N × число валидных отраслей
    if a.industries and a.per_industry:
        import source_rusprofile as RP
        _n = len([s for s in a.industries.split(",") if s.strip() in RP.INDUSTRY]) or 1
        a.count = a.per_industry * _n
        print(f"[объём] {a.per_industry} на отрасль × {_n} отраслей = {a.count} компаний всего")
    # режим окна сбора: по умолчанию headed, но ЗА ЭКРАНОМ (антибот RusProfile проходит,
    # окна не видно). --show-browser => видимое окно; --headless => без окна (может НЕ пройти антибот).
    headless = bool(a.headless)
    offscreen = (not a.show_browser) and (not headless)

    if a.industries:                                  # ФАЗА 1 — сбор сам (блокирующий Chrome -> в поток)
        mode = ("без окна (headless)" if headless
                else "окно скрыто за экраном" if offscreen else "окно Chrome видно")
        print(f"=== ФАЗА 1: сбор лидов (RusProfile — {mode}) ===")
        # --out терпимо принимает и старый .xlsx-путь: расширение всё равно станет .json
        json_out = (os.path.splitext(a.out)[0] + ".json" if a.out
                    else os.path.join(r"D:\лиды", "leads_" + a.industries.replace(",", "_") + ".json"))
        leads = await asyncio.to_thread(
            _collect, a.industries, a.count, a.min_revenue, a.region,
            headless, offscreen, a.base, a.account, json_out)
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
    tmp = tempfile.mkdtemp(prefix="orq_", dir=_tmp_root())

    disk_cache = set()                      # кэш созданных путей Диска — общий на прогон
    if not a.no_upload and not a.dry_run:   # сперва долить отложенное прошлыми прогонами
        await _drain_outbox(a.account, "долив с прошлых прогонов")
    # верхние уровни (отрасль/категория) у первого агента уже есть; mkdir идемпотентный.
    if not a.no_upload:
        try:
            await asyncio.to_thread(DO.ensure_dir, a.base, a.account, disk_cache)
            for lead in sel:
                ind = DO._safe(DO.industry_folder(lead))
                cat = DO._safe(DO.category_for(lead))
                await asyncio.to_thread(DO.ensure_dir, f"{a.base}/{ind}/{cat}", a.account, disk_cache)
        except Exception as e:
            # сеть/Диск чихнули — НЕ валим прогон: недостающие уровни создадутся по-компанейски
            print(f"[!] пред-создание папок на Диске: {str(e)[:90]} — продолжаю, создам по ходу")

    sem = asyncio.Semaphore(max(1, a.workers))
    pptx_sem = asyncio.Semaphore(1)   # .pptx-стадия (LibreOffice+node+CLI) — строго по одной
    min_ram = float(os.environ.get("ORQ_MIN_RAM_GB", "2.5"))
    research_timeout = float(os.environ.get("ORQ_RESEARCH_TIMEOUT", "1800"))  # сек на попытку ресёрча
    pptx_timeout = float(os.environ.get("ORQ_PPTX_TIMEOUT", "1200"))          # сек на попытку .pptx

    async def process(idx, lead):
        async with sem:
            comp_tmp = os.path.join(tmp, str(idx))       # СВОЯ папка на компанию: изоляция .pptx-рендера
            os.makedirs(comp_tmp, exist_ok=True)         # (slide-*.jpg/pdf не пересекаются между воркерами)
            d_tmp = os.path.join(comp_tmp, "d.docx")
            s_tmp = os.path.join(comp_tmp, "s.docx")
            p_tmp = os.path.join(comp_tmp, "p.pptx")     # презентация (третий деливерабл)
            cost = 0.0
            findings = ""
            have_pptx = False            # готова ли реальная .pptx у этой компании
            skip_docx = False            # оба .docx уже на Диске (резюм) — доделываем только .pptx
            keep_tmp = False             # файлы не удалось ни залить, ни отложить — temp не удалять
            comp_dir = _company_dir(lead, a.base, dup_names)
            dn = DO._safe(lead.get("name"))
            bp_name, rc_name, pptx_name = _doc_names(dn)
            try:
                # ---- РЕЗЮМ: не переделывать (и не переоплачивать) уже готовое на Диске ----
                if not a.dry_run and not a.no_upload and not a.redo:
                    docx_done, pptx_done = await asyncio.to_thread(
                        _remote_state, comp_dir, (bp_name, rc_name, pptx_name))
                    if docx_done and (pptx_done or not gen_pptx):
                        print(f"  ↷ [{idx}] {lead.get('name')[:40]}: уже на Диске — пропуск (--redo, чтобы переделать)")
                        return {"name": lead.get("name"), "ok": True, "cost": 0.0,
                                "dir": comp_dir, "resumed": True, "pptx": pptx_done, "files": 0}
                    if docx_done:
                        skip_docx = True    # догоняем только презентацию
                        findings = _cached_findings(lead.get("name"), lead.get("_inn"))
                        print(f"  ↷ [{idx}] {lead.get('name')[:40]}: .docx уже на Диске — делаю только .pptx")

                if a.dry_run:
                    try:
                        await asyncio.to_thread(DO.generate_dossier, lead, d_tmp)
                        await asyncio.to_thread(DO.generate_strategy, lead, s_tmp)
                        if gen_pptx:        # паритет с .docx: в dry-run кладём валидную болванку .pptx
                            await asyncio.to_thread(DO.generate_presentation, lead, p_tmp)
                            have_pptx = os.path.exists(p_tmp) and os.path.getsize(p_tmp) > 5000
                    except Exception as e:
                        print(f"  [!] {lead.get('name')}: {e}")
                        return {"name": lead.get("name"), "ok": False, "cost": cost, "why": "dry-run заглушки"}
                else:
                    # ресёрч+писатель: ретраим до 3 раз ПО ФАКТУ отсутствия двух .docx — ловим И
                    # исключения (0xC0000409 OOM / сеть / таймаут зависшей сессии), И «тихие» сбои,
                    # когда сессия вернулась без исключения, но писатель ничего не сохранил.
                    # Новая попытка = свежая сессия; частичные файлы чистим, чтобы стартовать начисто.
                    if not skip_docx:
                        ok_docx = False
                        for attempt in range(3):
                            await _wait_ram(min_ram, f"[{idx}]")
                            res = await _attempt(
                                _research_one(lead, idx, d_tmp, s_tmp, a.model, a.person_enrich),
                                research_timeout, f"  [retry {attempt + 1}/3] {lead.get('name')}")
                            if res is not None:
                                c1, findings = res
                                cost += c1
                            if (os.path.exists(d_tmp) and os.path.getsize(d_tmp) > 5000
                                    and os.path.exists(s_tmp) and os.path.getsize(s_tmp) > 5000):
                                ok_docx = True
                                break
                            if attempt < 2:
                                bp = os.path.getsize(d_tmp) if os.path.exists(d_tmp) else 0
                                rc = os.path.getsize(s_tmp) if os.path.exists(s_tmp) else 0
                                print(f"  [retry {attempt + 1}/3] {lead.get('name')}: писатель не сохранил "
                                      f"оба .docx (bp={bp}б, rc={rc}б) — повтор")
                                _rm(d_tmp, s_tmp)
                                await asyncio.sleep(4)
                        if not ok_docx:
                            print(f"  [!] {lead.get('name')}: писатель не сохранил оба документа за 3 попытки — пропуск")
                            return {"name": lead.get("name"), "ok": False, "cost": cost,
                                    "why": "писатель не сохранил .docx"}

                    # Третья стадия (опц.). Сбой НЕ валит компанию — .docx уже готовы/на Диске.
                    # Ретраим ПО ФАКТУ отсутствия .pptx (>5КБ): ловим и исключения (0xC0000409,
                    # таймаут), и «тихие» сбои. Стадия сериализована pptx_sem: LibreOffice+node+CLI —
                    # самая прожорливая по RAM связка, две параллельно машина не тянет.
                    if gen_pptx:
                        remark = ""
                        async with pptx_sem:
                            for attempt in range(3):
                                await _wait_ram(min_ram, f"[{idx}] [pptx]")
                                res = await _attempt(
                                    _presentation_one(lead, idx, p_tmp, a.model, findings),
                                    pptx_timeout, f"    [{idx}] [presentation retry {attempt + 1}/3]")
                                if res is not None:
                                    p_cost, remark = res
                                    cost += p_cost
                                if os.path.exists(p_tmp) and os.path.getsize(p_tmp) > 5000:
                                    break                       # дек готов
                                if attempt < 2:
                                    print(f"    [{idx}] [presentation retry {attempt + 1}/3]: .pptx не получена — повтор")
                                    _rm(p_tmp)                  # убрать недописанный перед повтором
                                    await asyncio.sleep(4)
                        have_pptx = os.path.exists(p_tmp) and os.path.getsize(p_tmp) > 5000
                        # «замечание» по Булату печатаем ТОЛЬКО при реальном деке (иначе это текст ошибки API)
                        if have_pptx and remark and "API Error" not in remark and "error result" not in remark:
                            print(f"    [{idx}] [presentation] замечание: {remark[:300]}")
                        if not have_pptx:
                            print(f"    [{idx}] [presentation] .pptx не получена (>5КБ) за 3 попытки — "
                                  + (".docx уже на Диске" if skip_docx else "зальём только два .docx"))

                pairs = [] if skip_docx else [(d_tmp, bp_name), (s_tmp, rc_name)]
                if have_pptx:           # презентация — в ту же папку компании (если получилась)
                    pairs.append((p_tmp, pptx_name))
                if not a.no_upload and pairs:
                    try:
                        await asyncio.to_thread(DO.ensure_dir, comp_dir, a.account, disk_cache)
                        for lp, rname in pairs:
                            await asyncio.to_thread(DO._upload, lp, f"{comp_dir}/{rname}", a.account, True)
                    except Exception as e:
                        print(f"  [!] upload {lead.get('name')}: {e}")
                        # деньги уже потрачены: файлы НЕ удаляем, а откладываем в outbox на долив
                        saved = await asyncio.to_thread(_outbox_defer, lead.get("name"), comp_dir, pairs)
                        if saved:
                            print(f"  [outbox] готовые файлы отложены: {saved} — будут долиты следующим прогоном")
                            return {"name": lead.get("name"), "ok": False, "cost": cost, "why": "заливка на Диск (файлы в outbox)"}
                        keep_tmp = True
                        print(f"  [outbox] отложить не удалось — файлы остаются в {comp_tmp}")
                        return {"name": lead.get("name"), "ok": False, "cost": cost,
                                "why": "заливка на Диск", "kept": comp_tmp}
                tag = "  +pptx" if have_pptx else ""
                if skip_docx:
                    tag += ("  (докинута только .pptx)" if have_pptx
                            else "  (.pptx не вышла — на Диске прежние .docx)")
                print(f"  ✓ [{idx}] {lead.get('name')[:40]} -> {comp_dir}" + tag
                      + (f"  (${cost:.2f})" if cost else ""))
                return {"name": lead.get("name"), "ok": True, "cost": cost, "dir": comp_dir,
                        "pptx": have_pptx, "files": len(pairs)}
            finally:
                # транзит компании чистим СРАЗУ после заливки/отложки (не копим все 200 до конца —
                # минимальный локальный след). При --no-upload оставляем: файлы смотрят локально.
                if not a.no_upload and not keep_tmp:
                    shutil.rmtree(comp_tmp, ignore_errors=True)

    raw = await asyncio.gather(*(process(i, l) for i, l in enumerate(sel)),
                               return_exceptions=True)   # сбой одной компании не валит прогон
    results = []
    for i, r in enumerate(raw):
        if isinstance(r, BaseException):
            if _fatal_exc(r):
                raise r
            print(f"  [!] {sel[i].get('name')}: необработанный сбой компании: {str(r)[:120]}")
            r = {"name": sel[i].get("name"), "ok": False, "cost": 0.0, "why": "сбой процесса"}
        results.append(r)
    ok = [r for r in results if r and r.get("ok")]
    fails = [r for r in results if not (r and r.get("ok"))]
    resumed = sum(1 for r in ok if r.get("resumed"))
    total = sum(r.get("cost") or 0 for r in results if r)
    n_pptx = sum(1 for r in ok if r.get("pptx"))
    n_files = sum(r.get("files") or 0 for r in ok)   # реально сделанных/залитых в ЭТОТ прогон
    print(f"\n[ГОТОВО] компаний: {len(ok)}/{len(sel)}"
          + (f" (из них {resumed} по резюму, без затрат)" if resumed else "")
          + f" | файлов за прогон: {n_files}"
          + (f" (в т.ч. {n_pptx} презентаций)" if n_pptx else "") + " "
          + ("(локально, без Диска) " if a.no_upload else f"в {a.base} ")
          + (f"| стоимость ~${total:.2f}" if total else "| $0"))
    if fails:
        print(f"[ВНИМАНИЕ] {len(fails)} компаний остались БЕЗ свежих документов "
              "(на Диске у них лежат заготовки ФАЗЫ 1):")
        for r in fails:
            print(f"  - {(r or {}).get('name')}: {(r or {}).get('why') or 'сбой'}")
        print("  Повтори ту же команду: резюм пропустит готовые компании и доделает только эти.")
    if not a.no_upload and not a.dry_run:
        await _drain_outbox(a.account, "финальный долив")
    kept = [r.get("kept") for r in results if r and isinstance(r, dict) and r.get("kept")]
    if a.no_upload:
        print(f"[локально] .docx/.pptx во временной папке: {tmp}")
    elif kept:
        print(f"[!] файлы {len(kept)} компаний не спасены в outbox — temp сохранён: {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    if sel and not ok and not a.dry_run:
        raise SystemExit(3)   # системный провал: ни одной компании за весь прогон


def _no_sleep(on=True):
    """Не давать Windows уснуть, пока идёт прогон (SetThreadExecutionState).
    Блокируется только сон/гибернация СИСТЕМЫ — дисплей гаснуть может. Флаг
    ES_CONTINUOUS действует до снятия или до выхода процесса, так что даже
    аварийное завершение ничего не «залочит». Не Windows / нет прав — no-op."""
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:
        pass


if __name__ == "__main__":
    _no_sleep(True)      # ночной прогон не должен обрываться уходом машины в сон
    try:
        asyncio.run(main())
    finally:
        _no_sleep(False)
