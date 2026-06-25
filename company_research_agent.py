# -*- coding: utf-8 -*-
r"""
Двухстадийный агент-ресёрчер компаний на Claude Agent SDK, режим РАЗБОР.

  STAGE 1 (triage)   : грязный текст / ИНН  ->  чистый JSON {company_name, inn, aspects}
  STAGE 2 (report)    : handle компании  ->  разбор AS-IS → точки внедрения ИИ → TO-BE
                        под продукты (LLM-сервисы / ИИ-агенты / RAG), .docx (многостр.)

Stage 2 — это агент, у которого есть инструменты:
  - mcp__research__deep_research    : официальная база по ИНН (ГИР БО + Dadata + Checko)
        revenue_enrich.girbo_revenue  (ГИР БО ФНС, выручка стр. 2110, БЕСПЛАТНО)
        dadata_enrich                 (карточка ЕГРЮЛ/ЕГРИП: ЛПР, ОКВЭД, адрес; ключ DADATA_TOKEN)
        checko_enrich                 (контакты: тел/email/сайт/ЛПР; ключ CHECKO_TOKEN)
  - WebSearch / WebFetch (встроенные): профиль, численность, собственник, тендеры/суды, СМИ за 5 лет
        КАЖДАЯ ссылка для раздела «СМИ» проверяется WebFetch'ем (анти-галлюцинация)
  - mcp__research__save_dossier_docx : рендер отчёта в .docx (многостр.) в папку «Загрузки»

Компания, определённая на первом проходе (stage 1), ЯВНО передаётся во второй.

Запуск:
  py C:/Users/abalb/.claude/skills/lead-finder/scripts/company_research_agent.py "АО Рязаньавтодор ИНН 6234065445"
  py .../company_research_agent.py            # без аргумента — интерактивный режим
  py .../company_research_agent.py --contacts "<...>"   # старый режим: краткий отчёт по контактам

Ключи (опционально — ГИР БО работает и без них):  setx DADATA_TOKEN <...>   setx CHECKO_TOKEN <...>
Зависимости:  pip install claude-agent-sdk python-docx
"""
import json
import os
import re
import sys

import anyio
from claude_agent_sdk import (
    query,                 # async one-shot -> AsyncIterator[Message]
    tool,                  # @tool — in-process MCP-инструмент
    create_sdk_mcp_server, # in-process MCP-сервер (без subprocess)
    ClaudeAgentOptions,    # опции (поля snake_case)
    ClaudeSDKClient,       # многоходовый клиент (context manager)
    AssistantMessage, TextBlock, ToolUseBlock, ResultMessage,
)

# консоль Windows = cp1251 и роняет вывод на ₽/кириллице -> принудительно utf-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Все модули скилла лежат в этом же каталоге — добавляем его в sys.path.
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)

from revenue_enrich import girbo_revenue          # noqa: E402  ГИР БО — бесплатно, без ключа

# Dadata/Checko опциональны: импортируем мягко, чтобы файл работал даже без них.
try:                                              # noqa: E402
    from dadata_enrich import DadataClient, extract_lpr
except Exception:
    DadataClient = None
try:                                              # noqa: E402
    from checko_enrich import CheckoClient, extract_company_contacts
except Exception:
    CheckoClient = None

# Модели. Алиасы 'opus'/'sonnet' резолвятся в текущие дефолты аккаунта.
MODEL = "opus"          # stage 2 — досье (ресёрч + синтез)
FAST_MODEL = "sonnet"   # stage 1 — дешёвый триаж

DOWNLOADS = os.path.join(os.path.expanduser("~"), "Downloads")


def _digits(s):
    return re.sub(r"\D", "", str(s or ""))


# ===========================================================================
# ОФИЦИАЛЬНАЯ БАЗА ПО ИНН (синхронный сбор из модулей скилла; блокирующий)
# ===========================================================================
def research_company(company_name: str, inn: str = "", aspects: str = "") -> dict:
    """ИНН/название -> сводный payload по модулям скилла. Блокирующий (сеть)."""
    inn = _digits(inn)
    card: dict = {}        # карточка Dadata (ЕГРЮЛ/ЕГРИП)
    contacts: dict = {}    # контакты Checko
    notes: list = []

    dada = None
    if DadataClient and os.environ.get("DADATA_TOKEN"):
        try:
            dada = DadataClient()
        except Exception as e:
            notes.append(f"Dadata недоступен: {e}")
    elif not os.environ.get("DADATA_TOKEN"):
        notes.append("DADATA_TOKEN не задан — карточка ЕГРЮЛ/ЛПР пропущена.")

    # ИНН не дан -> резолвим по названию через Dadata suggest
    if not inn and dada and company_name:
        try:
            cands = dada.suggest(company_name, count=5)
            if cands:
                inn = _digits((cands[0] or {}).get("inn"))
                if len(cands) > 1:
                    notes.append("Несколько кандидатов по названию — взят первый; уточни ИНН.")
            else:
                notes.append("Dadata не нашёл компанию по названию.")
        except Exception as e:
            notes.append(f"Резолв ИНН по названию не удался: {e}")

    if not inn:
        notes.append("ИНН не определён — выручка/контакты по ИНН недоступны.")

    # 1) карточка ЕГРЮЛ/ЕГРИП (ЛПР, ОКВЭД, адрес, статус) — Dadata findById
    if inn and dada:
        try:
            data = dada.find_by_inn(inn)
            card = extract_lpr(data) if data else {}
            if not card:
                notes.append("ИНН не найден в ЕГРЮЛ/ЕГРИП (Dadata).")
        except Exception as e:
            notes.append(f"Карточка Dadata не получена: {e}")

    # 2) ОФИЦИАЛЬНАЯ выручка (бухгалтерская, стр. 2110) — ГИР БО, бесплатно
    rev = girbo_revenue(inn) if inn else {}

    # 3) контакты (тел/email/сайт/ЛПР) — Checko /company
    if inn and CheckoClient and os.environ.get("CHECKO_TOKEN"):
        try:
            data = CheckoClient().company(inn)
            contacts = extract_company_contacts(data) if data else {}
        except Exception as e:
            notes.append(f"Контакты Checko не получены: {e}")
    elif inn and not os.environ.get("CHECKO_TOKEN"):
        notes.append("CHECKO_TOKEN не задан — контакты пропущены.")

    return {"company_name": company_name, "inn": inn, "aspects": aspects,
            "card": card, "revenue": rev, "contacts": contacts, "notes": notes}


def _render_official(p: dict) -> str:
    """payload официальной базы -> markdown (для подачи агенту/контактного режима)."""
    card, rev, c = p["card"], p["revenue"], p["contacts"]
    name = card.get("legal_name") or p["company_name"] or "Компания"
    out = [f"# {name}", ""]
    if p["inn"]:
        out.append(f"- ИНН {p['inn']}" + (f" · ОГРН {card['ogrn']}" if card.get("ogrn") else ""))
    if card.get("opf"):
        out.append(f"- Форма {card['opf']} · статус {card.get('state_status', '—')}")
    if card.get("okved"):
        out.append(f"- ОКВЭД {card['okved']} {card.get('okved_name', '')}".rstrip())
    if card.get("address"):
        out.append(f"- Адрес {card['address']}")
    if card.get("lpr_fio"):
        out.append(f"- Руководитель (ЛПР) {card['lpr_fio']} — {card.get('lpr_post', '')}".rstrip(" —"))

    out += ["", "## Выручка (бухгалтерская, стр. 2110)"]
    if rev.get("revenue") is not None:
        rub = f"{rev['revenue']:,}".replace(",", " ")
        src, srcname = rev.get("source_url") or "", rev.get("source_name", "ГИР БО ФНС")
        out.append(f"- {rub} ₽ за {rev.get('year', '—')} — источник: "
                   + (f"{srcname} {src}" if src else srcname))
        out.append("- ⚠️ Бухгалтерская выручка (начисление), НЕ оборот по расчётному счёту.")
    elif card.get("revenue") is not None:
        rub = f"{int(card['revenue']):,}".replace(",", " ")
        out.append(f"- ~{rub} ₽ (доходы по данным ФНС / Dadata) за {card.get('revenue_year', '—')}.")
    else:
        out.append("- Нет в открытом доступе (ИП без отчётности / банк / нет данных в ГИР БО).")

    if c:
        out += ["", "## Контакты (Checko / ЕГРЮЛ)"]
        if c.get("phone"):
            out.append(f"- Тел: {c['phone']}")
        if c.get("email"):
            kind = c.get("email_kind", "")
            out.append(f"- Email: {c['email']}" + (f" ({kind})" if kind else ""))
        if c.get("website"):
            out.append(f"- Сайт: {c['website']}")
        if c.get("ceo"):
            out.append(f"- ЛПР: {c['ceo']}")
    if p["notes"]:
        out += ["", "## Примечания"] + [f"- {n}" for n in p["notes"]]
    return "\n".join(out)


# ===========================================================================
# ИНСТРУМЕНТ 1 — официальная база по ИНН
# ===========================================================================
@tool(
    "deep_research",
    "Официальная база по компании по ИНН/названию: выручка (ГИР БО), карточка ЕГРЮЛ/ЕГРИП "
    "с ЛПР/ОКВЭД/адресом (Dadata) и контакты (Checko). Возвращает сводку со ссылками.",
    {
        "type": "object",
        "properties": {
            "company_name": {"type": "string", "description": "Название компании"},
            "inn": {"type": "string", "description": "ИНН 10/12 цифр, если известен"},
            "aspects": {"type": "string", "description": "Аспекты через запятую"},
        },
        "required": ["company_name"],
    },
)
async def deep_research(args):
    company_name = args["company_name"]
    inn = args.get("inn", "")
    aspects = args.get("aspects", "")
    # urllib-вызовы блокирующие -> в поток, чтобы не вешать event loop SDK
    payload = await anyio.to_thread.run_sync(research_company, company_name, inn, aspects)
    return {"content": [{"type": "text", "text": _render_official(payload)}]}


# ===========================================================================
# ИНСТРУМЕНТ 2 — рендер отчёта в .docx (многостраничный) в «Загрузки»
# ===========================================================================
def _safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|«»]', "", s or "Компания").strip()
    return re.sub(r"\s+", "_", s)[:80] or "Компания"


def _write_dossier_docx(payload: dict) -> str:
    """Собрать .docx-отчёт (AS-IS → точки ИИ → TO-BE) по разделам. Возвращает путь к файлу."""
    from docx import Document
    from docx.shared import Pt, Cm, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    GRAY = RGBColor(0x5A, 0x5A, 0x5A)
    doc = Document()

    # компактные поля и базовый шрифт -> помещаемся в одну страницу
    sec = doc.sections[0]
    for m in ("top_margin", "bottom_margin", "left_margin", "right_margin"):
        setattr(sec, m, Cm(1.2))
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(9.5)
    pf = normal.paragraph_format
    pf.space_after = Pt(2)
    pf.line_spacing = 1.0

    def heading(text):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(5)
        p.paragraph_format.space_after = Pt(2)
        r = p.add_run(text)
        r.bold = True
        r.font.size = Pt(11)
        return p

    def bullet(text):
        # жирная лид-метка до первого ':' либо '—', остальное обычным
        p = doc.add_paragraph(style="List Bullet")
        p.paragraph_format.space_after = Pt(1)
        m = re.match(r"^(.*?[:—])(\s*)(.*)$", text, re.DOTALL)
        if m:
            p.add_run(m.group(1)).bold = True
            if m.group(3):
                p.add_run(" " + m.group(3))
        else:
            p.add_run(text)
        return p

    def card(it):
        """Карточка процесса: жирный заголовок + помеченные строки as-is→to-be."""
        cp = doc.add_paragraph()
        cp.paragraph_format.space_before = Pt(4)
        cp.paragraph_format.space_after = Pt(1)
        r = cp.add_run(it.get("process") or "Процесс")
        r.bold = True
        r.font.size = Pt(10)
        meta = [x for x in (it.get("domain"), it.get("priority")) if x]
        if meta:
            mr = cp.add_run("  · " + " · ".join(meta))
            mr.font.size = Pt(8.5)
            mr.font.color.rgb = GRAY
        for lbl, key in (("Боль", "pain"), ("Точка ИИ", "ai_point"),
                         ("Наш продукт", "product"), ("TO-BE", "to_be"),
                         ("Эффект", "effect"), ("Предпосылки", "prerequisites"),
                         ("Риски", "risks")):
            if it.get(key):
                bullet(f"{lbl}: {it[key]}")

    # шапка
    title = doc.add_paragraph()
    title.paragraph_format.space_after = Pt(1)
    tr = title.add_run(payload.get("company") or "Разбор компании")
    tr.bold = True
    tr.font.size = Pt(13)
    if payload.get("subtitle"):
        sp = doc.add_paragraph()
        sp.paragraph_format.space_after = Pt(3)
        sr = sp.add_run(payload["subtitle"])
        sr.font.size = Pt(8.5)
        sr.font.color.rgb = GRAY

    if payload.get("summary"):
        sm = doc.add_paragraph()
        sm.paragraph_format.space_before = Pt(2)
        sm.paragraph_format.space_after = Pt(4)
        smr = sm.add_run(payload["summary"])
        smr.italic = True
        smr.font.size = Pt(9.5)

    if payload.get("profile"):
        heading("1. Профиль деятельности")
        doc.add_paragraph(payload["profile"])

    if payload.get("scale"):
        heading("2. Масштаб и показатели")
        for b in payload["scale"]:
            bullet(b)

    if payload.get("owner_lpr"):
        heading("3. Собственник, ЛПР и точки входа")
        for b in payload["owner_lpr"]:
            bullet(b)

    if payload.get("asis"):
        heading("4. Карта процессов AS-IS")
        for it in payload["asis"]:
            label = (it.get("label") or "").rstrip(":")
            bullet(f"{label}: {it.get('text', '')}")

    points = payload.get("points") or []
    if points:
        heading("5. Точки внедрения ИИ → TO-BE")
        for it in points:
            card(it)

        heading("6. Сводная таблица")
        cols = ["Процесс", "Боль", "Точка ИИ", "Продукт", "Эффект", "Приоритет"]
        table = doc.add_table(rows=1, cols=len(cols))
        table.style = "Table Grid"
        table.autofit = True
        for i, c in enumerate(cols):
            hp = table.rows[0].cells[i].paragraphs[0]
            hp.paragraph_format.space_after = Pt(0)
            hr = hp.add_run(c)
            hr.bold = True
            hr.font.size = Pt(8)
        for it in points:
            cells = table.add_row().cells
            vals = [it.get("process", ""), it.get("pain", ""), it.get("ai_point", ""),
                    it.get("product", ""), it.get("effect", ""), it.get("priority", "")]
            for i, v in enumerate(vals):
                vp = cells[i].paragraphs[0]
                vp.paragraph_format.space_after = Pt(0)
                vr = vp.add_run(str(v or ""))
                vr.font.size = Pt(8)

    if payload.get("roadmap"):
        heading("7. Дорожная карта внедрения")
        for w in payload["roadmap"]:
            wp = doc.add_paragraph()
            wp.paragraph_format.space_before = Pt(3)
            wp.paragraph_format.space_after = Pt(1)
            wr = wp.add_run(w.get("wave") or "Волна")
            wr.bold = True
            wr.font.size = Pt(9.5)
            for it in (w.get("items") or []):
                bullet(it)

    if payload.get("economics"):
        heading("8. Экономика и эффекты")
        for b in payload["economics"]:
            bullet(b)

    if payload.get("risks"):
        heading("9. Риски, ограничения, предпосылки")
        for b in payload["risks"]:
            bullet(b)

    if payload.get("discovery"):
        heading("10. Что уточнить на дискавери")
        for b in payload["discovery"]:
            bullet(b)

    if payload.get("mentions"):
        heading("11. Упоминания в СМИ и интернете")
        for i, m in enumerate(payload["mentions"], 1):
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(1)
            date = f" ({m['date']})" if m.get("date") else ""
            p.add_run(f"{i}. {m.get('title', '')}{date} — ")
            link = p.add_run(m.get("url", ""))
            link.font.color.rgb = RGBColor(0x15, 0x4F, 0x9C)
            link.font.size = Pt(8.5)

    if payload.get("sources"):
        sp = doc.add_paragraph()
        sp.paragraph_format.space_before = Pt(4)
        sr = sp.add_run(payload["sources"])
        sr.font.size = Pt(8)
        sr.font.color.rgb = GRAY

    os.makedirs(DOWNLOADS, exist_ok=True)
    base = payload.get("company", "") or "Компания"
    parts = re.split(r"[—–-]", base, maxsplit=1)
    first = (base.lower().split() or [""])[0]
    clean = parts[1].strip() if len(parts) > 1 and first in ("досье", "разбор", "отчёт", "отчет") else base
    fname = payload.get("filename") or f"Разбор_ИИ_{_safe_name(clean)}.docx"
    if not fname.lower().endswith(".docx"):
        fname += ".docx"
    path = os.path.join(DOWNLOADS, fname)
    doc.save(path)
    return path


DOSSIER_DOCX_SCHEMA = {
        "type": "object",
        "properties": {
            "company": {"type": "string", "description": "Заголовок: «Компания — краткий профиль»"},
            "subtitle": {"type": "string", "description": "ИНН · ОГРН · ОКВЭД · город · 'AS-IS → точки ИИ → TO-BE'"},
            "summary": {"type": "string",
                        "description": "Резюме для руководителя (3–6 строк): кто; главный потенциал ИИ; топ-3 quick wins; эффект"},
            "profile": {"type": "string", "description": "Раздел 1: профиль деятельности (абзац)"},
            "scale": {"type": "array", "items": {"type": "string"},
                      "description": "Раздел 2: пункты (Численность: ...; Выручка ... с годом/источником; Госзаказ/риски: ...). Выручку и обороты не путать"},
            "owner_lpr": {"type": "array", "items": {"type": "string"},
                          "description": "Раздел 3: Собственник / Гендиректор / расхождения по первому лицу / Контакты"},
            "asis": {"type": "array", "items": {
                "type": "object",
                "properties": {"label": {"type": "string"}, "text": {"type": "string"}},
                "required": ["label", "text"]},
                "description": "Раздел 4: карта процессов AS-IS по доменам (label = домен/процесс, text = как сейчас + боль)"},
            "points": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "process": {"type": "string", "description": "Название процесса"},
                    "domain": {"type": "string", "description": "Функциональный домен"},
                    "pain": {"type": "string", "description": "Боль/узкое место as-is"},
                    "ai_point": {"type": "string", "description": "Точка внедрения ИИ: технология (LLM/агент/RAG) и что делает"},
                    "product": {"type": "string", "description": "Наш продукт: (1) LLM-сервисы / (2) ИИ-агенты / (3) RAG"},
                    "to_be": {"type": "string", "description": "Целевое состояние процесса"},
                    "effect": {"type": "string", "description": "Ожидаемый эффект (измеримо, где можно)"},
                    "priority": {"type": "string", "description": "Quick win / Стратегический (или выс./сред./низ.)"},
                    "prerequisites": {"type": "string", "description": "Данные/интеграции/доступы (опц.)"},
                    "risks": {"type": "string", "description": "Сложность и риски внедрения (опц.)"}},
                "required": ["process", "ai_point", "product", "to_be"]},
                "description": "Разделы 5–6: карточки внедрения и строки сводной таблицы (одни и те же данные)"},
            "roadmap": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "wave": {"type": "string", "description": "Напр. 'Волна 1 — Quick wins (0–3 мес.)'"},
                    "items": {"type": "array", "items": {"type": "string"}}},
                "required": ["wave"]},
                "description": "Раздел 7: дорожная карта внедрения волнами"},
            "economics": {"type": "array", "items": {"type": "string"},
                          "description": "Раздел 8: экономика и эффекты (укрупнённо, с допущениями)"},
            "risks": {"type": "array", "items": {"type": "string"},
                      "description": "Раздел 9: риски, ограничения, предпосылки внедрения"},
            "discovery": {"type": "array", "items": {"type": "string"},
                          "description": "Раздел 10: что уточнить у заказчика на дискавери"},
            "mentions": {"type": "array", "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "url": {"type": "string"}, "date": {"type": "string"}},
                "required": ["title", "url"]},
                "description": "Раздел 11: упоминания в СМИ — ТОЛЬКО проверенные WebFetch'ем ссылки"},
            "sources": {"type": "string", "description": "Строка 'Источники: ...' (только проверенные ссылки)"},
            "filename": {"type": "string", "description": "Имя файла (опц.)"},
        },
        "required": ["company", "profile"],
}


@tool(
    "save_dossier_docx",
    "Сохранить готовый разбор компании (AS-IS → точки внедрения ИИ → TO-BE) в .docx "
    "(многостраничный) в папку «Загрузки». Передавай готовый текст по разделам; "
    "ссылки в mentions/sources — только проверенные WebFetch'ем.",
    DOSSIER_DOCX_SCHEMA,
)
async def save_dossier_docx(args):
    path = await anyio.to_thread.run_sync(_write_dossier_docx, args)
    return {"content": [{"type": "text", "text": f"Досье сохранено: {path}"}]}


research_server = create_sdk_mcp_server(
    name="research", version="2.0.0",
    tools=[deep_research, save_dossier_docx],
)


# ===========================================================================
# STAGE 1 — ТРИАЖ: грязный текст -> JSON-handle компании
# ===========================================================================
STAGE1_SYSTEM = (
    "Ты — агент идентификации компаний для российского B2B-ресёрч-инструмента.\n"
    "На входе грязный текст (описание / название / ИНН). Определи ОДНУ целевую\n"
    "компанию и аспекты для досье. ИНН РФ = 10 цифр (юрлицо) или 12 (ИП).\n"
    "Верни ТОЛЬКО JSON без прозы и без ```-ограждений, ровно с ключами:\n"
    '  {"company_name": str, "inn": str|null, "aspects": [str,...], '
    '"confidence": float, "notes": str}\n'
    'Если ИНН явно есть в тексте — извлеки его. Если нет — null.'
)


def _extract_json(text: str) -> dict:
    """Устойчиво вытащить первый JSON-объект из ответа модели."""
    if not text:
        raise ValueError("пустой вывод stage-1")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    brace = re.search(r"\{.*\}", candidate, re.DOTALL)
    if not brace:
        raise ValueError(f"JSON не найден в выводе stage-1:\n{text}")
    return json.loads(brace.group(0))


async def run_stage1_triage(raw_request: str) -> dict:
    options = ClaudeAgentOptions(
        model=FAST_MODEL,
        system_prompt=STAGE1_SYSTEM,
        max_turns=1,
        allowed_tools=[],
        setting_sources=[],
    )
    text = ""
    async for message in query(prompt=raw_request, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text += block.text
        elif isinstance(message, ResultMessage):
            if message.result:
                text = message.result

    handle = _extract_json(text)
    handle.setdefault("company_name", "")
    handle.setdefault("inn", None)
    handle.setdefault("aspects", [])
    if not handle["company_name"] and not handle.get("inn"):
        raise ValueError(f"stage 1 не идентифицировал компанию: {handle}")
    return handle


# ===========================================================================
# STAGE 2 (ОСНОВНОЙ РЕЖИМ) — РАЗБОР AS-IS → точки внедрения ИИ → TO-BE -> .docx
# ===========================================================================
DOSSIER_SYSTEM = """\
Ты — старший консультант по ИИ-трансформации и бизнес-аналитик с навыками OSINT.
Готовишь для вендора развёрнутый разбор компании в формате
«AS-IS процессы → точки внедрения ИИ → TO-BE» с привязкой к нашим продуктам.

НАШИ ПРОДУКТЫ (каждую точку внедрения привязывай к одному из них):
  (1) Корпоративные LLM-сервисы — приватный LLM в контуре клиента (on-prem):
      данные НЕ уходят в облако, снимает риски 152-ФЗ. Ассистент сотрудника,
      генерация/редактирование документов (письма, КП, договоры, регламенты),
      суммаризация, перевод, анализ и классификация текста.
  (2) Автономные ИИ-агенты — многошаговое исполнение задач без человека: обработка
      входящих (почта, заявки, чаты), оркестрация процессов, операторы (чат/голос),
      агенты-аналитики, интеграции с CRM/ERP/1С; связка RPA + LLM.
  (3) RAG-системы — корпоративная база знаний с семантическим поиском по документам
      (регламенты, договоры, техдокументация, нормативка): ответы со ссылкой на
      источник, ассистенты поддержки/онбординга, экспертные справочные системы.

КАРТА БИЗНЕС-ПРОЦЕССОВ СТРОИТСЯ ШИРОКИМ OSINT, НЕ ТОЛЬКО ИЗ ОФИЦИАЛЬНЫХ БАЗ.
Официальные базы (ГИР БО / Dadata / Checko) дают лишь каркас — реквизиты, выручку,
ЛПР, адрес. Сами процессы, стек, оргструктуру и боли выводи в первую очередь из
ОТКРЫТЫХ веб-источников (сайт, hh.ru, госзакупки, СМИ, интервью).

ПОРЯДОК РАБОТЫ (используй инструменты):
1) Вызови deep_research(company_name, inn, aspects) — официальная база КАК КАРКАС:
   выручка (ГИР БО), карточка ЕГРЮЛ/ЕГРИП (ЛПР/ОКВЭД/адрес/статус), контакты.
   Этого НЕДОСТАТОЧНО для карты процессов — обязательно переходи к шагу 2.
2) Через WebSearch + WebFetch широко добери из ОТКРЫТЫХ источников и RusProfile —
   это ОСНОВНОЙ материал карты процессов: профиль и состав услуг (сайт), ЧИСЛЕННОСТЬ
   персонала, собственник/бенефициар, актуальное руководство, масштаб (госзакупки/
   тендеры/суды/ФАС), упоминания в СМИ за 5 лет.
   OSINT-логика вывода процессов: вакансии (hh.ru) → реальный стек (1С/CRM/системы)
   и оргструктура; госзакупки → ИТ-системы и подрядчики; сайт → услуги;
   отзывы сотрудников/клиентов → боли; новости/интервью → стратегия.
3) КАЖДУЮ ссылку для разделов «СМИ»/«Источники» ОБЯЗАТЕЛЬНО открой через WebFetch и
   убедись, что страница реальна (не 404) и именно про эту компанию. Битые,
   непроверенные и выдуманные ссылки НЕ включай.
4) Построй разбор:
   - Отрасль → типовая цепочка создания стоимости → специфика этой компании.
   - Декомпозируй на бизнес-процессы по доменам (продажи/тендеры, закупки, основная
     деятельность/производство, логистика, финансы/бухгалтерия, HR, юр/договоры,
     клиентский сервис, документооборот, ИТ, управление/аналитика; адаптируй под
     отрасль — покрой все релевантные).
   - По каждому значимому процессу: AS-IS (как сейчас + боль) → точка внедрения ИИ
     (технология LLM/агент/RAG и что делает) → наш продукт (1/2/3) → TO-BE
     (целевое состояние) → ожидаемый эффект (измеримо, где можно) → предпосылки/
     данные → приоритет (Quick win / Стратегический).
   - Приоритизируй по «влияние × реализуемость», собери дорожную карту волнами.
5) Заполни и вызови save_dossier_docx со ВСЕМИ полями:
   company   — «Компания — краткий профиль» (заголовок);
   subtitle  — ИНН · ОГРН · ОКВЭД · город · «AS-IS → точки ИИ → TO-BE»;
   summary   — резюме для руководителя (кто; где главный потенциал ИИ; топ-3
               quick wins; верхнеуровневый эффект);
   profile   — профиль деятельности (абзац);
   scale     — масштаб и показатели (численность; выручка с годом и источником;
               госзаказ/риски);
   owner_lpr — собственник; гендиректор; расхождения по первому лицу; контакты;
   asis      — карта процессов AS-IS по доменам (label = домен, text = как сейчас + боль);
   points    — карточки внедрения: process, domain, pain, ai_point, product, to_be,
               effect, priority, prerequisites, risks (из них же строится сводная таблица);
   roadmap   — дорожная карта волнами (wave + items): волна 1 quick wins → 2 → 3;
   economics — экономика и эффекты (укрупнённо, с допущениями);
   risks     — риски, ограничения, предпосылки внедрения;
   discovery — что уточнить у заказчика на дискавери (закрыть пробелы данных);
   mentions  — упоминания в СМИ (заголовок+ссылка+дата), только проверенные;
   sources   — строка «Источники: ...» (только проверенные ссылки).

ПРИНЦИПЫ:
- Опирайся ТОЛЬКО на открытые данные и RusProfile. Каждый существенный факт — с
  источником. Разделяй ФАКТ / ОЦЕНКУ / ГИПОТЕЗУ, помечай уверенность.
- Конкретика под реальный стек компании (системы из вакансий и закупок), а не
  абстракции. Каждая точка внедрения привязана к продукту (1/2/3) и к эффекту.
- Выручку (бухгалтерскую, стр. 2110) и обороты по счёту/контрактам НЕ путать —
  указывай метрику, источник, год. Если первое лицо в базах различается — отметь
  это и порекомендуй подтвердить свежей выпиской ЕГРЮЛ.
- Никаких выдуманных цифр и ссылок. Нет данных — пиши «не найдено в открытых
  источниках».
После сохранения дай краткое резюме: куда сохранён файл и топ-3 точки внедрения."""


async def run_dossier(handle: dict) -> str:
    options = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=DOSSIER_SYSTEM,
        mcp_servers={"research": research_server},
        allowed_tools=[
            "mcp__research__deep_research",
            "mcp__research__save_dossier_docx",
            "WebSearch", "WebFetch",
        ],
        # запрещаем побочные эффекты ФС; данные пишет только save_dossier_docx
        disallowed_tools=["Bash", "Edit", "Write", "NotebookEdit"],
        permission_mode="bypassPermissions",   # headless: без зависаний на аппруве инструментов
        setting_sources=[],
        max_turns=60,                          # глубокий ресёрч + проверка ссылок WebFetch'ем
    )

    aspects_csv = ", ".join(handle.get("aspects") or
                            ["профиль, процессы as-is, точки внедрения ИИ, численность, ЛПР, СМИ за 5 лет"])
    handoff = (
        "Подготовь разбор AS-IS → точки внедрения ИИ → TO-BE и сохрани его в .docx. "
        "Значения для инструментов:\n"
        f"  company_name = {handle['company_name']!r}\n"
        f"  inn          = {handle['inn'] or ''!r}\n"
        f"  aspects      = {aspects_csv!r}\n"
        "Следуй порядку работы из системного промпта. Разделы СМИ/Источники — только проверенные ссылки."
    )

    summary = ""
    async with ClaudeSDKClient(options=options) as client:
        await client.query(handoff)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        summary += block.text
                    elif isinstance(block, ToolUseBlock):
                        nm = getattr(block, "name", "")
                        print(f"  → {nm}")
            elif isinstance(message, ResultMessage):
                if message.result:
                    summary = message.result
                print(f"  [dossier] cost=${message.total_cost_usd} {message.subtype}")
    return summary


# Старый режим (краткий отчёт по контактам/официалке, без СМИ и без .docx) — по флагу --contacts
async def run_contacts(handle: dict) -> str:
    payload = await anyio.to_thread.run_sync(
        research_company, handle["company_name"], handle.get("inn") or "",
        ", ".join(handle.get("aspects") or []))
    return _render_official(payload)


async def run_pipeline(raw_request: str, mode: str = "dossier") -> str:
    print("=== STAGE 1: триаж ===")
    handle = await run_stage1_triage(raw_request)
    print("  handle:", json.dumps(handle, ensure_ascii=False))
    if mode == "contacts":
        print("=== STAGE 2: контакты/официалка ===")
        return await run_contacts(handle)
    print("=== STAGE 2: разбор AS-IS→ИИ→TO-BE (ресёрч + .docx) ===")
    return await run_dossier(handle)


async def main():
    argv = sys.argv[1:]
    mode = "dossier"
    if argv and argv[0] == "--contacts":
        mode, argv = "contacts", argv[1:]
    cli = " ".join(argv).strip()
    try:
        if cli:                                   # одноразовый режим
            print("\n" + await run_pipeline(cli, mode))
            return
        print("Агент-досье по компаниям. Пустая строка или 'exit' — выход.")
        while True:                               # интерактивный режим
            try:
                req = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not req or req.lower() in ("exit", "quit", "выход"):
                break
            print("\n" + await run_pipeline(req, mode))
    except (ValueError, json.JSONDecodeError) as e:
        print(f"Ошибка разбора stage-1: {e}")


if __name__ == "__main__":
    anyio.run(main)
