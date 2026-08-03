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

Штатный боевой режим ветки claude-sdk — генератор на Claude Agent SDK: controller,
deep-research extract, enrichment-роли, оба DOCX-писателя и one-pager работают через
claude-agent-sdk основного окружения (авторизация — логин Claude Code/ANTHROPIC_API_KEY).
Pipeline тот же, что у Kimi-runtime; прежний полный Kimi-режим возвращается
ORQ_KIMI_ONLY=1 (ключ KIMI_API_KEY, fallback GPLLM_API_KEY).
Доступ к Диску — env YANDEX_DISK_TOKEN. dry-run не требует ничего сверх stdlib.
"""
import argparse
import asyncio
import collections
import json
import os
import shutil
import sys
import tempfile
import time
import warnings
from typing import Annotated

# Косметический RequestsDependencyWarning (chardet 7.x вне диапазона requests; ставится Crawl4AI,
# на работу не влияет) — глушим ДО первого импорта requests. Фильтр по тексту, без импорта requests.
warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)

from project_env import load_project_env

load_project_env()

import kimi_config as KC
import disk_organize as DO  # пути на Диске + upload + заглушки (stdlib, без сети при импорте)

KC.ensure_env()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# Имена выходных файлов в папке компании на Диске.
def _doc_names(dn):
    return (f"{dn}_карта_бизнес-процессов.docx",
            f"{dn}_карта_ролей_и_контактов_пресейл.docx",
            f"{dn}_презентация_Telepatt.pdf")


# Инструменты ресёрч-агента: свой save на документ + веб + веер субагентов ("Agent";
# типы scout/critic/verifier объявлены в _research_one). Движка (deep_research) в списке
# нет намеренно — см. _research_one: где искать, решает агент, а не зашитый конвейер.
# ⚠️ Справочная константа: кодом НЕ используется — боевой allowlist собирается инлайном
# в _session (по ОДНОМУ save-инструменту на сессию, а не оба сразу). Правя один список,
# правь и второй.
ALLOWED = [
    "mcp__research__save_process_map_docx",
    "mcp__research__save_roles_contacts_docx",
    "WebSearch", "WebFetch",
    "Agent",
]

# --- Стадия презентации (третий деливерабл: редакционный one-pager .pdf на Kimi) ---
# Раньше здесь была брендированная .pptx (Claude SDK + официальный скилл pptx, LibreOffice/node).
# Заменена на one-pager: модель Kimi по своему системному промпту выдаёт HTML -> Playwright -> PDF.
ASSETS_DIR = os.path.join(SCRIPTS, "assets")
PHOTO_PNG = os.path.join(ASSETS_DIR, "bulat_zamaliev.png")   # фото Булата Замалиева (вшивается в PDF)

# Стадия живёт в СОСЕДНЕЙ папке со СВОИМ venv: kimi-agent-sdk конфликтует по зависимостям с
# claude-agent-sdk (pydantic-core), поэтому в один интерпретатор их ставить нельзя. Отсюда зовём
# её подпроцессом venv-питона — это и есть «сведение двух SDK» в одном прогоне.
KIMI_DIR = os.environ.get(
    "KIMI_DIR", os.path.join(os.path.dirname(SCRIPTS), "lead_orchestrator_kimi"))
KIMI_PY = os.environ.get(
    "KIMI_PY",
    os.path.join(KIMI_DIR, ".venv_kimi", "Scripts", "python.exe"),
)
KIMI_CLI = os.path.join(KIMI_DIR, "onepager_kimi.py")

# Endpoint провайдера Kimi. Ключ — KIMI_API_KEY, иначе GPLLM_API_KEY (так он задан на этой машине).
# base_url/модель имеют рабочие дефолты, любой из них перекрывается env.
KIMI_BASE_URL_DEFAULT = KC.DEFAULT_BASE_URL
KIMI_MODEL_DEFAULT = KC.DEFAULT_MODEL


def _kimi_key():
    return KC.api_key()


# --- Кап одновременных браузерных краулов (тул crawl_site у субагентов-разведчиков) ---
# Каждый краул Crawl4AI поднимает СВОЙ chromium. Веер scout'ов множится на --workers, и
# без капа прогон упирается в RAM (то самое 0xC0000409). Семафор ленивый и привязан к
# текущему loop — как _sem в движке: у воркеров могут быть разные loop'ы.
ORQ_CRAWL_CONCURRENCY = int(os.environ.get("ORQ_CRAWL_CONCURRENCY", "2"))
ORQ_CRAWL_PAGE_CHARS = int(os.environ.get("ORQ_CRAWL_PAGE_CHARS", "4000"))
_CRAWL_SEMS = {}


def _crawl_sem():
    try:
        key = id(asyncio.get_running_loop())
    except RuntimeError:
        key = 0
    s = _CRAWL_SEMS.get(key)
    if s is None:
        s = _CRAWL_SEMS[key] = asyncio.Semaphore(ORQ_CRAWL_CONCURRENCY)
    return s


# Белый список переменных, которые доезжают до подпроцесса стадии. Всё остальное — не доезжает.
# Список, а не «весь os.environ минус секреты»: при чёрном списке каждый НОВЫЙ токен в окружении
# автоматически утекал бы в стадию, и про него надо было бы вспомнить. Здесь — наоборот: новая
# переменная по умолчанию НЕ передаётся.
_KIMI_ENV_ALLOW = frozenset((
    # Ради чего стадия и запускается.
    "KIMI_API_KEY", "KIMI_BASE_URL", "KIMI_MODEL_NAME",
    # claude-runtime: авторизация Claude Code/Anthropic и модель стадии.
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_OAUTH_TOKEN", "ORQ_ONEPAGER_MODEL",
    # Python и кодировки. PYTHONPATH намеренно НЕ пропускаем: он подмешал бы site-packages
    # основного venv в интерпретатор Kimi — а это ровно тот конфликт pydantic-core, из-за
    # которого venv и разведены.
    "PATH", "PYTHONIOENCODING", "PYTHONUTF8",
    # Windows: без SYSTEMROOT на старте падают сокеты и ssl; остальное нужно python и chromium.
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "OS",
    "TEMP", "TMP", "TMPDIR", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "ALLUSERSPROFILE", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "PUBLIC",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    # Linux/Docker: контейнер + xvfb (headless-рендер PDF).
    "HOME", "USER", "LANG", "LC_ALL", "TZ", "DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR",
    # Chromium для рендера PDF.
    "PLAYWRIGHT_BROWSERS_PATH",
    # Сеть и TLS: иначе перестанут работать корпоративный прокси и свой CA.
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
))


def _kimi_env():
    """Окружение для подпроцесса стадии: ключ/endpoint/модель + инфраструктура. Больше ничего.

    Стадию исполняет СТОРОННИЙ kimi-cli (альфа 0.0.5) и запускается он с yolo=True, то есть
    с авто-аппрувом вызовов инструментов. Отдавать ему весь os.environ незачем: раньше туда
    уезжали ANTHROPIC_API_KEY, YANDEX_DISK_TOKEN, DADATA_TOKEN, OFDATA_API_KEY,
    CHECKO_TOKEN и FIRECRAWL_API_KEY,
    хотя стадии нужен ровно один ключ — свой. Сравнение имён по upper() заодно ловит linux'овые
    http_proxy/https_proxy в нижнем регистре, сохраняя исходное написание ключа."""
    env = {k: v for k, v in os.environ.items() if k.upper() in _KIMI_ENV_ALLOW}
    env["KIMI_API_KEY"] = _kimi_key()
    env["KIMI_BASE_URL"] = KC.base_url()
    env["KIMI_MODEL_NAME"] = KC.model_name()
    env["ORQ_LLM_RUNTIME"] = KC.runtime()      # claude|kimi — выбирает ветку в onepager_kimi
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# ДВА прохода дипресёрча: у документов разные цели, значит и разный добор. Раньше был
# один общий проход на оба — добор под роли/контакты разменивался на добор под процессы.
# Проходы кэшируются раздельно (findings_<ключ>__<проход>.md), поэтому ретрай писателя и
# повторный прогон не гоняют движок заново.
RESEARCH_PASSES = (
    ("process", "профиль и виды деятельности, услуги, процессы as-is, ИТ-ландшафт и "
                "внедрённые ИС, госконтракты как заказчика и как поставщика, финансы, "
                "вакансии, регламенты, суды и жалобы, точки внедрения ИИ"),
    ("roles", "оргструктура, руководство и замы, ЛПР, руководитель ИТ/цифровизации, "
              "закупки и контактные лица извещений, филиалы и их директора, официальные "
              "контакты, деловые соцсети и публичные профили руководителей, учредитель и "
              "курирующее ведомство, назначения в СМИ за 5 лет"),
)

_ASPECTS_DEFAULT = "профиль, процессы as-is, точки внедрения ИИ, оргструктура, ЛПР, контакты, СМИ за 5 лет"


def _handle(lead, aspects=None):
    aspects = aspects or _ASPECTS_DEFAULT
    # website из ФАЗЫ 1 -> подсказка-домен движку deep_research (сайт-коллектор без зависимости от поиска)
    site = (lead.get("website") or "").strip()
    if site:
        aspects += f"; сайт: {site}"
    return {
        "company_name": lead.get("name") or "",
        "inn": str(lead.get("_inn") or "").strip(),
        "aspects": aspects,
    }


async def _person_enrichment_block(lead, idx, enabled=True):
    """Детерминированное обогащение ЛПР, общее для Kimi и legacy-ветки."""
    if not (enabled and lead.get("contact_person") and lead.get("_inn")):
        return ""
    try:
        import person_enrich as PEN
        verify = os.environ.get("PERSON_VERIFY_EMAIL", "").strip().lower() in (
            "1", "true", "yes", "on", "да")
        social = os.environ.get("PERSON_SOCIAL", "").strip().lower() in (
            "1", "true", "yes", "on", "да")
        enriched = await asyncio.to_thread(
            PEN.enrich_person, lead["contact_person"], lead["_inn"],
            domain=lead.get("website"), verify_email=verify, social=social)
        print(f"    [{idx}] person_enrich: email "
              f"{len(enriched['contacts']['work_emails'])}, "
              f"тел {len(enriched['contacts']['work_phones'])}"
              + ("" if enriched.get("fio_confirmed") else " (ФИО ЛПР не подтв. ЕГРЮЛ)"))
        return PEN.format_findings_block(enriched)
    except Exception as exc:
        print(f"    [{idx}] person_enrich пропущен: {str(exc)[:80]}")
        return ""


async def _research_one_kimi(lead, idx, d_tmp, s_tmp, model, person_enrich=True):
    """Полный ШТАТНЫЙ research/write-маршрут (текущий pipeline) для обоих runtime:
    движок двумя проходами -> person_enrich -> граф пяти enrichment-ролей -> агентный
    писатель на документ. SDK под стадиями выбирает kimi_config.runtime(); в claude-режиме
    Claude Agent SDK живёт в подпроцессах агентов, а не в этом интерпретаторе."""
    import deep_research_engine as DRE
    import writer_kimi as WK

    h = _handle(lead)
    ttl_h = float(os.environ.get("ORQ_FINDINGS_TTL_H", "72"))
    pe_block = await _person_enrichment_block(lead, idx, person_enrich)
    findings = {}
    for pass_, aspects in RESEARCH_PASSES:
        hp = _handle(lead, aspects)
        cached = _cached_findings(hp["company_name"], hp["inn"], ttl_h=ttl_h, pass_=pass_)
        if cached:
            print(f"    [{idx}] deep_research[{pass_}]: находки из кэша "
                  f"(моложе {ttl_h:g} ч) — движок пропущен")
            findings[pass_] = cached
            continue
        print(f"    [{idx}] deep_research[{pass_}] (движок) ...")
        try:
            findings[pass_] = await DRE.deep_research(
                hp["company_name"], hp["inn"], hp["aspects"])
        except Exception as exc:
            print(f"    [{idx}] deep_research[{pass_}] engine error: {str(exc)[:90]}")
            findings[pass_] = ""
        cache_path = _findings_cache_path(
            hp["company_name"], hp["inn"], pass_)
        if cache_path and findings[pass_] and len(findings[pass_]) > 200:
            try:
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as fh:
                    fh.write(findings[pass_])
            except OSError:
                pass

    if pe_block:
        findings["roles"] = (findings.get("roles") or "") + "\n\n" + pe_block

    print(f"    [{idx}] research-субагенты: official → (contour + secondary) → roles → contacts")
    enrichment_path = _enrichment_cache_path(h["company_name"], h["inn"])
    seed = {
        "process": findings.get("process", ""),
        "roles": findings.get("roles", ""),
    }
    # Обогащение НЕ должно ронять компанию: даже если граф ролей упал целиком, писатель обязан
    # запуститься по находкам движка + person_enrich (гард на пустые .docx остаётся в process()).
    try:
        enrichment = await WK.run_research_subagents(
            lead, idx, seed, os.path.dirname(d_tmp), model,
            checkpoint=enrichment_path, checkpoint_ttl_h=ttl_h)
    except Exception as exc:                       # noqa: BLE001 — краш/таймаут подпроцесса ролей
        print(f"    [{idx}] research-субагенты недоступны ({str(exc)[:160]}); "
              f"писатель работает по находкам движка")
        enrichment = {"schema_version": None, "complete": False, "roles": {}, "roles_failed": {}}

    roles_present = enrichment.get("roles") or {}
    process_roles = {key: roles_present[key]
                     for key in ("official_sources", "corporate_contour") if key in roles_present}
    if process_roles:
        process_dossier = {"schema_version": enrichment.get("schema_version"), "roles": process_roles}
        findings["process"] = (findings.get("process") or "") + (
            "\n\n=== ДОПОЛНИТЕЛЬНОЕ ДОСЬЕ: OFFICIAL + CORPORATE CONTOUR ===\n"
            + json.dumps(process_dossier, ensure_ascii=False, indent=2))
    if roles_present:
        findings["roles"] = (findings.get("roles") or "") + (
            "\n\n=== JSON-ДОСЬЕ RESEARCH-СУБАГЕНТОВ ===\n"
            + json.dumps(enrichment, ensure_ascii=False, indent=2))
    cost = await WK.write_two_docx(
        lead, idx, findings.get("process", ""), findings.get("roles", ""),
        d_tmp, s_tmp, model, enrichment=enrichment)
    return cost, (findings.get("process") or findings.get("roles") or "")


async def _research_one(lead, idx, d_tmp, s_tmp, model, person_enrich=True):
    """Один ресёрч-проход. Возвращает (стоимость, находки): находки нужны стадии
    презентации для формулировки боли заказчика. d_tmp = карта бизнес-процессов,
    s_tmp = карта ролей и контактов.

    Ветки принципиально разные — и это осознанно:

    * Claude (opus/sonnet) — АГЕНТ. Своя сессия на документ, свой системный промпт
      (PROCESS_MAP_SYSTEM / ROLES_CONTACTS_SYSTEM), инструменты — только WebSearch/WebFetch
      и свой save_*_docx. Движка тут НЕТ вообще: где искать и что брать, решает агент, а
      направляет его ТОЛЬКО системный промпт. На вход даются известные данные лида.
    * Kimi — АГЕНТ в отдельном .venv_kimi: движок сначала гоняется двумя проходами
      (RESEARCH_PASSES), затем писатель вызывает нативным Task scout/critic/verifier.
      Scout/verifier могут точечно добирать веб через read-only subprocess-мост в движок;
      JSON возвращается в основной venv, где его рендерят общие функции CRA.

    Почему движок убран из агентной ветки: он решал за агента И где искать (зашитые
    коллекторы site/ЕИС/суды-СМИ/hh/TAdviser), И что взять со страницы (схема llm_extract —
    ролецентричная, бизнес-процессов в ней нет вовсе). Заодно исчезла вложенная SDK-сессия
    (движок внутри сессии писателя) — та самая, из-за которой его когда-то и вынесли."""
    import writer_kimi as WK
    if KC.kimi_only() and not WK.is_kimi(model):
        raise RuntimeError(
            "Claude/Anthropic отключён: _research_one принимает только model='kimi'")
    # Оба псевдонима runtime ведут в ТЕКУЩИЙ pipeline: разница только в SDK под стадиями.
    # Ниже по файлу остаётся ДРУГАЯ архитектура — legacy «агент ресёрчит сам», достижимая
    # только явным именем модели (opus/sonnet) при ORQ_KIMI_ONLY=0.
    if WK.is_kimi(model) or WK.is_claude(model):
        return await _research_one_kimi(
            lead, idx, d_tmp, s_tmp, model, person_enrich=person_enrich)

    # Ни один импорт ниже этой точки не выполняется в штатном Kimi-only маршруте.
    import anyio  # noqa: F401  (нужен косвенно legacy SDK/CRA)
    import company_research_agent as CRA
    import deep_research_engine as DRE
    from claude_agent_sdk import (
        tool, create_sdk_mcp_server, AgentDefinition, ClaudeAgentOptions, ClaudeSDKClient,
        ResultMessage, AssistantMessage, TextBlock, ToolUseBlock,
    )

    # Субагент-разведчик: писатель раздаёт направления, scout'ы читают страницы каждый
    # в СВОЁМ контексте и возвращают выжимку со ссылками. Инструменты — только веб:
    # save_*_docx ему не дают, деливерабл делает писатель. Модель по умолчанию sonnet
    # (как DR_EXTRACT_MODEL у движка): scout'ы дают основную массу токенов, а их работа —
    # «прочитать и пересказать со ссылкой», а не выводы; opus держим на писателе.
    SCOUT = AgentDefinition(
        description=(
            "OSINT-разведка по ОДНОМУ направлению о компании. Запускай нескольких "
            "параллельно, по одному на независимое направление. Возвращает выжимку "
            "находок со ссылками на реально открытые страницы; документы не сохраняет."
        ),
        prompt=CRA.SCOUT_SYSTEM,
        # crawl_site — браузерный обход сайта поверх SiteCrawler движка. Даём его только
        # scout'ам, не писателю: дампы страниц должны оседать в ИХ контекстах, ради чего
        # веер и заводился.
        tools=["WebSearch", "WebFetch", "mcp__research__crawl_site"],
        mcpServers=["research"],
        model=os.environ.get("ORQ_SCOUT_MODEL", "sonnet"),
    )

    # Критик полноты. Роль уехала вместе с движком (его completeness_critic) и ничем не
    # заменилась: нудж-ретрай проверяет НАЛИЧИЕ файла, а не содержимое, и схема молчит о
    # том, что автор чего-то не нашёл. Инструментов нет: он судит черновик, а не ищет —
    # поэтому и дёшев. Follow-up'ы не формулирует (в отличие от прежнего): чем закрывать
    # дыру, решает писатель, иначе критик снова начнёт диктовать источники.
    CRITIC = AgentDefinition(
        # description — это триггер вызова, а не описание: агент читает именно его,
        # решая, звать ли. Поэтому здесь КОГДА, а не только ЧТО.
        description=(
            "Критик полноты документа. Зови ПЕРЕД save_*_docx, передав черновик: вернёт "
            "список дыр — пустых ячеек под видом заполненных, утверждений без источника, "
            "недатированных ролей, находок, не доехавших до документа. Где искать "
            "недостающее, не подскажет — это твоё решение."
        ),
        prompt=CRA.CRITIC_SYSTEM,
        tools=[],
        model=os.environ.get("ORQ_CRITIC_MODEL", "sonnet"),
    )

    # Состязательный верификатор. ROLES_CONTACTS_SYSTEM требует два независимых источника
    # и «однофамилец — не ЛПР», но обеспечивал это только сам писатель — который же и
    # заинтересован закрыть поле. Этот играет за другую сторону: его задача — опровергнуть.
    VERIFIER = AgentDefinition(
        description=(
            "Состязательная проверка ОДНОГО утверждения об ЛПР или спорном факте: "
            "пытается его ОПРОВЕРГНУТЬ (однофамилец, устаревшая роль, подмена юрлица, "
            "единственный источник). Зови параллельно — по одному на каждое лицо или "
            "факт, который собираешься внести в документ."
        ),
        prompt=CRA.VERIFIER_SYSTEM,
        tools=["WebSearch", "WebFetch", "mcp__research__crawl_site"],
        mcpServers=["research"],
        model=os.environ.get("ORQ_VERIFIER_MODEL", "sonnet"),
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

    # Браузерный обход сайта для scout'ов — ОБЁРТКА над SiteCrawler движка (Crawl4AI
    # BestFirst -> HTTP-фолбэк), а не второй краулер: краул живёт в deep_research_engine
    # и правится там. Смысл — дать разведчику путь к тому, что WebFetch не отдаёт:
    # JS-рендеринг и часть антибот-порталов (портал ведомства и т.п.).
    @tool("crawl_site",
          "Обойти САЙТ браузером (Crawl4AI/chromium) и вернуть текст страниц в markdown. "
          "Берёт то, чего не отдаёт WebFetch: JS-рендеринг и часть антибот-порталов. "
          "Обход ранжируется ТВОИМИ keywords — передавай слова своего направления. "
          "Это обход домена, а не страницы: для одиночного URL бери WebFetch. Дорого "
          "(поднимает браузер) — зови, когда WebFetch не справился либо нужен раздел целиком.",
          {"domain": Annotated[str, "Домен или URL сайта, напр. 'citrt.ru'"],
           "keywords": Annotated[list[str], "Слова твоего направления для ранжирования обхода"],
           "max_pages": Annotated[int, "Сколько страниц взять, 1-15"]})
    async def _crawl_site(args):
        dom = str(args.get("domain") or "").strip()
        if not dom:
            return {"content": [{"type": "text", "text": "domain пуст — нечего обходить"}]}
        cap = max(1, min(int(args.get("max_pages") or 8), 15))
        try:
            async with _crawl_sem():
                pages = await DRE.SiteCrawler(
                    max_pages=cap, keywords=args.get("keywords")).crawl_sections(dom)
        except Exception as e:
            # Сбой краула — не отказ инструмента: разведчик должен узнать причину и
            # пойти другим путём (WebFetch, архив, зеркало), а не молча потерять домен.
            return {"content": [{"type": "text",
                                 "text": f"краул {dom} упал: {str(e)[:200]}. Попробуй WebFetch "
                                         f"или архивную копию."}]}
        if not pages:
            return {"content": [{"type": "text",
                                 "text": f"{dom}: не собрано ни одной страницы (сайт пуст, "
                                         f"недоступен или целиком за антиботом)"}]}
        print(f"    [{idx}] scout crawl_site: {dom} -> {len(pages)} стр.")
        out = [f"Обход {dom}: {len(pages)} страниц (движок: {pages[0].get('source') or '?'}). "
               f"Текст каждой страницы обрезан до {ORQ_CRAWL_PAGE_CHARS} символов."]
        for p in pages:
            out.append(f"\n--- {p.get('url') or ''}\n"
                       f"{(p.get('markdown') or '')[:ORQ_CRAWL_PAGE_CHARS]}")
        return {"content": [{"type": "text", "text": "\n".join(out)}]}

    h = _handle(lead)
    pe_block = await _person_enrichment_block(lead, idx, person_enrich)

    # ============ ветка Claude: агент решает всё сам ============
    # Движка здесь НЕТ — ни deep_research, ни коллекторов, ни discover_domains. Движок = зашитый
    # список источников (site/ЕИС/суды-СМИ/hh/TAdviser) и ролецентричная схема llm_extract, где
    # бизнес-процессов нет вовсе: он решал за агента И где искать, И что оттуда взять. Агенту
    # даются только веб и его save-инструмент, а направляет его ТОЛЬКО системный промпт.
    # Приятный побочный эффект: SDK-сессии внутри сессии больше нет — уходит и та причина,
    # по которой движок когда-то вынесли в пред-запуск.
    server = create_sdk_mcp_server(
        name="research", version="6.1.0",
        tools=[_save_process_map, _save_roles_contacts, _crawl_site],
    )

    # Вход агента — то, что известно про компанию из лида. Не метод, а данные: откуда копать
    # дальше, решает он.
    known = [f"  company_name = {h['company_name']!r}", f"  inn          = {h['inn']!r}"]
    for field, label in (("_ogrn", "ogrn"), ("website", "сайт"), ("contact_person", "ЛПР"),
                         ("phone", "телефон"), ("email", "email")):
        if (lead.get(field) or "").strip():
            known.append(f"  {label:12} = {lead[field]!r}")
    if lead.get("_revenue"):
        known.append(f"  выручка      = {lead['_revenue']:,} ₽".replace(",", " ")
                     + (f" за {lead['_revenue_year']} г." if lead.get("_revenue_year") else ""))
    known = "\n".join(known)

    cost = 0.0

    async def _session(system, save_tool, out_path, task, extra=""):
        """Одна сессия = один документ = свой системный промпт + свой ЕДИНСТВЕННЫЙ
        save-инструмент. Второй save не отдаём: агент физически не уедет в чужой документ.
        Где искать и что брать — не навязываем ничем, кроме системного промпта."""
        options = ClaudeAgentOptions(
            model=model,
            system_prompt=system,
            mcp_servers={"research": server},
            # "Agent" — тул порождения субагентов. Имя именно такое: в CLI лежит таблица
            # ренейма {Task: "Agent"}, т.е. "Task" — легаси-имя и наружу не уходит
            # (докстринг claude_agent_sdk/types.py:1850 — «invokable via the Agent tool»;
            # комментарий там же на :293 про «Task-spawned» просто не обновлён).
            allowed_tools=[f"mcp__research__{save_tool}", "WebSearch", "WebFetch", "Agent"],
            disallowed_tools=["Bash", "Edit", "Write", "NotebookEdit"],
            permission_mode="bypassPermissions",
            setting_sources=[],
            # Три РОЛИ, а не три темы: разведать / оспорить / отревизовать. Тематических
            # scout'ов (scout_it, scout_закупки) тут нет намеренно — это вернуло бы
            # зашитые коллекторы site/ЕИС/суды/hh, ради ухода от которых движок и
            # выносили. Направление scout получает в задании, а не в своём типе.
            agents={"scout": SCOUT, "critic": CRITIC, "verifier": VERIFIER},
            # max_turns НЕ задаём: дефолт SDK — None, и тогда --max-turns в CLI не
            # уходит вовсе (types.py: max_turns: int|None = None; subprocess_cli:
            # `if self._options.max_turns`). Стоял лимит 120, но одна только разведка
            # по компании — это сотни вызовов, и агент упирался в потолок раньше, чем
            # в исчерпанность темы. Ограничитель теперь не ходы, а веер scout'ов:
            # тяжёлое чтение уходит в их контексты, писатель остаётся налегке.
        )
        handoff = (
            f"{task}\n"
            "Что известно о компании (это ВХОДНЫЕ ДАННЫЕ, а не готовый ресёрч):\n"
            f"{known}\n"
            + (f"{extra}\n" if extra else "")
            + f"Работа НЕ выполнена, пока не вызван {save_tool}: текстовый ответ результатом "
              "не является."
        )

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
            for _ in range(2):      # нудж-ретрай: документ не сохранён -> потребовать
                if os.path.exists(out_path):
                    break
                await _drive(f"Ты НЕ вызвал {save_tool} — документ не сохранён. Немедленно "
                             "вызови его с заполненными данными. Больше ничего не делай.")

    await _session(
        CRA.PROCESS_MAP_SYSTEM, "save_process_map_docx", d_tmp,
        "Сделай КАРТУ БИЗНЕС-ПРОЦЕССОВ по компании.")
    await _session(
        CRA.ROLES_CONTACTS_SYSTEM, "save_roles_contacts_docx", s_tmp,
        "Сделай КАРТУ РОЛЕЙ И КОНТАКТОВ · ПРЕСЕЙЛ по компании.",
        extra=(("Прямые контакты ЛПР уже добыты детерминированно (легитимные источники) — "
                f"перенеси их в документ с источником и уровнем доверия:\n{pe_block}")
               if pe_block else ""))

    # Находок движка тут нет по построению — агент ресёрчил сам, в своей сессии. Боль для
    # one-pager'а стадия возьмёт из кэша движка (если он остался от Kimi-прогона), иначе
    # сформулирует по отрасли — `_main_pain` пустую строку переживает.
    return cost, ""


def _presentation_prereqs():
    """Готовность стадии one-pager. Возвращает (ok: bool, reason: str).
    reason — человекочитаемая русская причина пропуска (пусто при ok=True).
    Деградируем мягко: НИКОГДА не валим компанию — просто пропускаем стадию (два .docx делаются).
      - CLI стадии в соседней папке lead_orchestrator_kimi + фото спикера;
      - claude-runtime: claude-agent-sdk в ОСНОВНОМ окружении (venv/ключ Kimi не нужны);
      - kimi-runtime: venv Kimi и ключ провайдера (KIMI_API_KEY или GPLLM_API_KEY)."""
    if not os.path.isfile(KIMI_CLI):
        return False, f"нет {KIMI_CLI} (стадия one-pager живёт в соседней папке lead_orchestrator_kimi)"
    if not os.path.exists(PHOTO_PNG):
        return False, "нет assets/bulat_zamaliev.png (фото спикера для one-pager)"
    if KC.runtime() == "claude":
        try:
            import claude_agent_sdk  # noqa: F401 — стадия пойдёт этим же интерпретатором
        except ImportError:
            return False, "claude-agent-sdk не установлен в основном окружении (pip install -r requirements.txt)"
        return True, ""
    if not os.path.isfile(KIMI_PY):
        return False, (f"нет venv Kimi: {KIMI_PY} "
                       "(создай: py -m venv .venv_kimi && .venv_kimi\\Scripts\\python.exe -m pip "
                       "install kimi-agent-sdk playwright — и применить патчи, см. CLAUDE.md той папки)")
    if not _kimi_key():
        return False, "не задан ключ Kimi (KIMI_API_KEY или GPLLM_API_KEY)"
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


async def _onepager_one(lead, idx, p_tmp, findings=""):
    """Третий деливерабл: редакционный one-pager .pdf (Kimi -> HTML -> Playwright -> PDF).

    Запускается ПОДПРОЦЕССОМ venv-питона соседней папки: kimi-agent-sdk и claude-agent-sdk
    несовместимы по зависимостям и в одном интерпретаторе не живут (см. CLAUDE.md там же).
    Канон листа (шапка/герой/фичи/спикер/футер) зашит в onepager_kimi.py; под компанию
    пишется только блок «Одна проблема — одно решение» — его кормим болью из находок.

    Пишет итог в p_tmp. Возвращает (cost, remark): cost=0.0 — стоимость Kimi считает
    провайдер, наружу CLI её не отдаёт; remark — хвост вывода при неудаче (для лога)."""
    h = _handle(lead)
    industry = DO.industry_folder(lead) if hasattr(DO, "industry_folder") else (lead.get("niche") or "")
    pain = _main_pain(findings)

    # claude-runtime исполняет тот же CLI основным python'ом (SDK и playwright там);
    # kimi-runtime — прежним python'ом изолированного .venv_kimi.
    stage_py = sys.executable if KC.runtime() == "claude" else KIMI_PY
    cmd = [stage_py, KIMI_CLI, h["company_name"], "--out", p_tmp]
    if industry:
        cmd += ["--industry", industry]
    if pain:
        cmd += ["--pain", pain]

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=KIMI_DIR, env=_kimi_env(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await proc.communicate()
    except (asyncio.CancelledError, BaseException):
        # таймаут/Ctrl+C: не оставляем осиротевший python+chromium висеть в фоне
        try:
            proc.kill()
        except Exception:
            pass
        raise

    text = (out or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        # не валим компанию: два .docx уже готовы, стадию просто ретрайнут/пропустят
        tail = text[-400:] if text else f"код возврата {proc.returncode}"
        print(f"    [{idx}] [onepager] неуспех: {tail}")
        return 0.0, tail
    return 0.0, ""


def _collect(industries, count, min_revenue, region, headless, offscreen, base, account, json_out):
    """ФАЗА 1 (первый агент): сбор -> контакты -> отбор -> JSON -> папки+заготовки на Диске.
    Источник — env LEAD_SOURCE: 'rusprofile' (по умолчанию, Playwright+cookie),
    'ofdata' или 'checko' (явные API-пути отката). Возвращает picked[]."""
    import math
    import source_rusprofile as RP          # конфиг отраслей INDUSTRY нужен обоим источникам
    import pipeline

    inds = [s.strip() for s in industries.split(",") if s.strip() in RP.INDUSTRY]
    if not inds:
        raise SystemExit("не распознаны отрасли. Доступно: " + ", ".join(sorted(RP.INDUSTRY)))
    per_ind = math.ceil(count / max(1, len(inds)))

    source = (os.environ.get("LEAD_SOURCE") or "rusprofile").strip().lower()
    if source in ("ofdata", "ofdata_api"):
        return _collect_ofdata(inds, count, min_revenue, region, base, account, json_out, per_ind)
    if source in ("checko", "checko_api", "api"):
        return _collect_checko(inds, count, min_revenue, region, base, account, json_out, per_ind)
    if source not in ("rusprofile", "rp"):
        raise SystemExit(
            f"неизвестный LEAD_SOURCE={source!r}; допустимо: ofdata, checko, rusprofile")

    # --- RusProfile: штатная Фаза 1; Playwright search + одна карточка на выбранный лид ---
    import rusprofile_session as RPS
    if not os.path.exists(RPS.COOKIES_FILE):
        raise SystemExit("нет cookie RusProfile — один раз: py rusprofile_session.py --login")
    threshold = max(float(min_revenue), float(RP.MIN_REVENUE_FLOOR))
    browser = (os.environ.get("RUSPROFILE_BROWSER") or "playwright").strip().lower()
    print(f"[1/2] RusProfile/{browser}: {inds} | порог >={threshold / 1e9:g} млрд"
          + (f" | регион {region}" if region else ""))
    leads = []
    res = {}
    if browser in ("playwright", "pw"):
        from rusprofile_playwright import (
            RusProfilePlaywrightError,
            RusProfilePlaywrightSession,
        )
        playwright_completed = False
        for attempt in (1, 2):
            try:
                with RusProfilePlaywrightSession(
                        headless=headless, offscreen=offscreen) as rs:
                    leads = RP.harvest(
                        inds, min_revenue=threshold, per_industry=per_ind,
                        region=region, out_path=json_out, session=rs)
                    if not leads:
                        raise RusProfilePlaywrightError(
                            "расширенный поиск вернул пустой список")
                    # Карточки открываются только после revenue-sort и отбора N.
                    res = rs.enrich_leads(
                        leads, only_missing=True, log=print,
                        checkpoint=lambda: RP._save(leads, json_out))
                playwright_completed = True
                break
            except Exception as e:
                print(f"[1/2] RusProfile/Playwright: {str(e)[:160]}"
                      + (" — повтор через 10с" if attempt == 1 else ""))
                if attempt == 1:
                    time.sleep(10)
        if not playwright_completed:
            raise SystemExit(
                "RusProfile/Playwright не завершил Фазу 1 после 2 попыток — "
                "проверь cookie, коды ОКВЭД, доступ и антибот.")
    elif browser in ("uc", "chrome", "selenium"):
        for attempt in (1, 2):
            try:
                leads = RP.harvest(
                    inds, min_revenue=threshold, per_industry=per_ind,
                    region=region, headless=headless, out_path=json_out,
                    offscreen=offscreen)
            except Exception as e:
                print(f"[1/2] RusProfile/UC: {str(e)[:160]}")
                leads = []
            if leads:
                break
            if attempt == 1:
                print("[1/2] пусто/сбой — повтор через 15с (новая Chrome-сессия)")
                time.sleep(15)
        if not leads:
            raise SystemExit(
                "RusProfile ничего не вернул (2 попытки) — проверь коды ОКВЭД/доступ/антибот.")
        for attempt in (1, 2):
            try:
                with RPS.RusProfileAuth(headless=headless, offscreen=offscreen) as rs:
                    res = rs.enrich_leads(
                        leads, only_missing=True, log=print,
                        checkpoint=lambda: RP._save(leads, json_out))
                break
            except Exception as e:
                print(f"[1/2] сессия контактов упала: {str(e)[:120]}"
                      + (" — повтор через 10с" if attempt == 1 else ""))
                if attempt == 1:
                    time.sleep(10)
    else:
        raise SystemExit(
            f"неизвестный RUSPROFILE_BROWSER={browser!r}; допустимо: playwright, uc")
    if res.get("locked"):
        raise SystemExit("Контакты RusProfile закрыты — платная сессия протухла. Один раз: "
                         f"py rusprofile_session.py --login (сырой список уже сохранён: {json_out})")
    picked = pipeline._select(leads, count, inds)
    pipeline._save(picked, json_out)
    print(f"[1/2] собрано {len(picked)} | JSON: {json_out}")
    if _store_mode() == "disk":
        print("[1/2] раскладка папок+заготовок на Диске ...")
        DO.organize_to_disk(picked, base=base, account=account, log=print)   # папки создаёт ПЕРВЫЙ агент
    return picked


def _collect_ofdata(inds, count, min_revenue, region, base, account, json_out, per_ind):
    """ФАЗА 1 через OfData: /search -> /finances -> >=1 млрд -> /company."""
    import source_ofdata as OD
    import pipeline

    threshold = max(float(min_revenue), float(OD.MIN_REVENUE_FLOOR))
    print(f"[1/2] OfData API: {inds} | порог >={threshold / 1e9:g} млрд"
          + (f" | регион {region}" if region else ""))
    try:
        client = OD.OfDataClient()
        max_candidates = int(os.environ.get("OFDATA_MAX_CANDIDATES", "3000"))
        leads = OD.harvest(
            inds,
            min_revenue=threshold,
            per_industry=per_ind,
            region=region,
            out_path=json_out,
            max_candidates=max_candidates,
            client=client,
        )
    except OD.OfDataSourceError as e:
        raise SystemExit(f"OfData: {e}")
    except ValueError as e:
        raise SystemExit(f"OfData: неверная числовая настройка окружения — {e}")
    except Exception as e:
        raise SystemExit(f"OfData: сбор упал — {str(e)[:180]}")
    if not leads:
        raise SystemExit(
            "OfData не вернул компаний с выручкой >= 1 млрд ₽ — проверь отрасль, регион, "
            "доступ тарифа к /finances и OFDATA_API_KEY.")

    # Как в Checko-пути: карточки запрашиваем только для уже прошедших дорогой фильтр.
    # Ноль означает «все отобранные»; положительное значение ограничивает расход /company.
    try:
        cap = int(os.environ.get("OFDATA_CONTACTS_CAP", "0"))
        OD.ofdata_contacts_pass(client, leads, cap=cap, only_missing=True, log=print)
    except Exception as e:                          # noqa: BLE001
        print(f"[1/2] контакты OfData не добраны: {str(e)[:140]}")

    picked = pipeline._select(leads, count, inds)
    pipeline._save(picked, json_out)
    print(f"[1/2] собрано {len(picked)} | JSON: {json_out}")
    print(f"[1/2] расход OfData: {client.usage_line()}")
    if _store_mode() == "disk":
        print("[1/2] раскладка папок+заготовок на Диске ...")
        DO.organize_to_disk(picked, base=base, account=account, log=print)
    return picked


def _collect_checko(inds, count, min_revenue, region, base, account, json_out, per_ind):
    """ФАЗА 1 через Checko API (LEAD_SOURCE=checko): без браузера/антибота — годится для Docker/Linux.
    harvest сам добирает выручку из ГИР БО и режет порогом; контакты (сайт/тел/email/ЛПР) добираются
    по ИНН через checko_enrich — аналог платной сессии RusProfile. Дальше — общий отбор/сохранение/раскладка."""
    import source_checko as CK
    import checko_enrich as CE
    import pipeline

    print(f"[1/2] Checko: {inds} | порог >{min_revenue / 1e9:g} млрд"
          + (f" | регион {region}" if region else ""))
    try:
        leads = CK.harvest(inds, min_revenue=min_revenue, per_industry=per_ind,
                           region=region, out_path=json_out)
    except CK.CheckoSourceError as e:
        raise SystemExit(f"Checko: {e}")
    except Exception as e:                          # сеть/токен/лимит — стоп с понятным сообщением
        raise SystemExit(f"Checko: сбор упал — {str(e)[:160]}")
    if not leads:
        raise SystemExit("Checko ничего не вернул — проверь коды ОКВЭД (нужен уровень NN.NN), "
                         "регион и CHECKO_TOKEN.")

    # Контакты по ИНН (сайт/тел/email/ЛПР). Best-effort: сбой не должен ронять уже собранные лиды.
    try:
        cap = int(os.environ.get("CHECKO_CONTACTS_CAP", "100"))
        CE.checko_contacts_pass(CE.CheckoClient(), leads, cap=cap, only_missing=True, log=print)
    except Exception as e:                          # noqa: BLE001
        print(f"[1/2] контакты Checko не добраны: {str(e)[:120]}")

    picked = pipeline._select(leads, count, inds)
    pipeline._save(picked, json_out)
    print(f"[1/2] собрано {len(picked)} | JSON: {json_out}")
    if _store_mode() == "disk":
        print("[1/2] раскладка папок+заготовок на Диске ...")
        DO.organize_to_disk(picked, base=base, account=account, log=print)
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
    CLI) — пробрасывает. timeout <= 0 означает ожидание без общего дедлайна."""
    try:
        if timeout <= 0:
            return await coro
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
    data_root = os.environ.get("ORQ_DATA_ROOT", "").strip()
    if data_root:
        d = os.path.join(data_root, "orq_tmp")
        os.makedirs(d, exist_ok=True)
        return d
    if os.path.isdir("D:\\"):
        d = os.path.join("D:\\", "orq_tmp")
        os.makedirs(d, exist_ok=True)
        return d
    return tempfile.gettempdir()


def _work_base(subdir):
    """Путь к постоянной папке данных прогона рядом с temp: D:\\<subdir> (на C: мало
    места) или %TEMP%\\<subdir>. Саму папку НЕ создаёт."""
    data_root = os.environ.get("ORQ_DATA_ROOT", "").strip()
    if data_root:
        return os.path.join(data_root, subdir)
    return os.path.join("D:\\" if os.path.isdir("D:\\") else tempfile.gettempdir(), subdir)


def _store_mode():
    """Куда складывать деливераблы: 'local' (сервер сайта, по умолчанию) или 'disk' (Яндекс Диск).
    ORQ_STORE=disk возвращает прежнее поведение с заливкой на Диск."""
    return (os.environ.get("ORQ_STORE") or "local").strip().lower()


def _store_root():
    """Корень ЛОКАЛЬНОГО хранилища деливераблов: ORQ_DATA_ROOT/deliverables (или D:\\deliverables).
    Тот же путь читает веб (web/api/config.DELIVERABLES_DIR) и отдаёт файлы на скачивание."""
    explicit = os.environ.get("ORQ_DELIVERABLES_DIR", "").strip()
    return explicit or _work_base("deliverables")


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


def _findings_cache_path(name, inn, pass_=""):
    """Файл кэша находок движка (переживает прогоны). Ключ — ИНН, фолбэк — имя.
    pass_ разводит проходы дипресёрча по разным файлам; пустой — легаси-кэш от
    одного общего прохода (его ещё читают старые каталоги, поэтому имя не трогаем)."""
    key = str(inn or "").strip() or DO._safe(name)[:60].replace(" ", "_")
    if not key:
        return ""
    suffix = f"__{pass_}" if pass_ else ""
    return os.path.join(_work_base("orq_cache"), f"findings_{key}{suffix}.md")


def _enrichment_cache_path(name, inn):
    """JSON-кеш пяти специализированных research-субагентов."""
    key = str(inn or "").strip() or DO._safe(name)[:60].replace(" ", "_")
    return os.path.join(_work_base("orq_cache"), f"enrichment_{key}.json") if key else ""


def _cached_findings(name, inn, ttl_h=None, pass_="", legacy_ok=False):
    """Прочитать кэш находок, если он есть, содержателен (>200 симв.) и свеж.
    ttl_h=None — возраст не проверять (например, боль для слайда 3 не протухает).

    legacy_ok — разрешить откат на кэш от старого ОДНОГО общего прохода. По умолчанию
    выключено и включается только там, где находки нужны как справка (боль для слайда 3):
    для самих проходов откат недопустим — оба прочитали бы ОДИН и тот же легаси-файл, и
    двухпроходный ресёрч молча выродился бы в однопроходный на всех уже прогнанных
    компаниях. Пусть лучше проход честно сходит в движок и заведёт свой кэш."""
    paths = [_findings_cache_path(name, inn, pass_)]
    if pass_ and legacy_ok:
        paths.append(_findings_cache_path(name, inn))
    for p in paths:
        try:
            if p and os.path.isfile(p) and os.path.getsize(p) > 200:
                if ttl_h is None or time.time() - os.path.getmtime(p) < ttl_h * 3600:
                    return open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            pass
    return ""


REAL_PDF_MIN = 60000   # заготовка _make_pdf ≈ 10 КБ; реальный one-pager с фото — сотни КБ


def _remote_state(comp_dir, names):
    """Резюм: какие деливераблы уже лежат на Диске. Возвращает (docx_ok, pdf_ok).
    Любая ошибка (нет папки/сеть/токен) -> (False, False): резюм просто не срабатывает,
    компания честно переделывается."""
    try:
        sizes = DO._disk_client().list_file_sizes(comp_dir)
    except Exception:
        return False, False
    bp, rc, pp = names
    docx_ok = sizes.get(bp, 0) > 5000 and sizes.get(rc, 0) > 5000
    pdf_ok = sizes.get(pp, 0) > REAL_PDF_MIN
    return docx_ok, pdf_ok


def _local_state(store_dir, names):
    """Резюм для локального хранилища: какие деливераблы уже лежат в store_dir.
    Пороги те же, что у Диска (_remote_state): .docx >5 КБ, реальный .pdf >REAL_PDF_MIN."""
    def _sz(fn):
        p = os.path.join(store_dir, fn)
        try:
            return os.path.getsize(p) if os.path.isfile(p) else 0
        except OSError:
            return 0
    bp, rc, pp = names
    docx_ok = _sz(bp) > 5000 and _sz(rc) > 5000
    pdf_ok = _sz(pp) > REAL_PDF_MIN
    return docx_ok, pdf_ok


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


def _upload_verified(local, comp_dir, rname, account=None):
    """Залить файл и УБЕДИТЬСЯ, что он лёг: перечитать папку и сверить размер.

    ⚠️ Не убирать эту проверку. Яндекс Диск наблюдался (2026-07-13) в двух режимах
    молчаливого вранья: (1) рапортует «✓ Загружено», а файла на Диске нет вовсе;
    (2) при overwrite=True рапортует успех, но СТАРЫЙ файл остаётся (md5/размер прежние).
    Без сверки прогон считает компанию успешной, а на Диске — пусто или прошлая версия,
    и резюм такую компанию уже не догонит (она числится сделанной).
    При расхождении: сносим старый файл в Корзину (перезапись не работает) и льём заново.
    """
    want = os.path.getsize(local)
    remote = f"{comp_dir}/{rname}"
    last = 0
    for attempt in range(3):
        DO._upload(local, remote, account, True)
        time.sleep(1.5)                       # дать Диску применить запись
        try:
            last = DO._disk_client().list_file_sizes(comp_dir).get(rname, 0)
        except Exception:
            last = 0
        if last == want:
            return
        print(f"    [!] {rname}: Диск отрапортовал успех, а лежит {last} б вместо {want} б "
              f"— перезаливаю ({attempt + 2}/3)")
        if last:                              # старый файл мешает перезаписи — в Корзину
            try:
                DO._disk_client()._delete(remote, False)
            except Exception:
                pass
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"upload {remote}: файл не подтвердился на Диске "
                       f"({last} б вместо {want} б) после 3 попыток")


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
                    # с проверкой чтением: иначе outbox «дольёт» в пустоту и сотрёт файлы
                    _upload_verified(lp, comp_dir, rname, account)
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
                    help="параллельных Kimi-ресёрчей; при нехватке RAM авто-снижается до 1")
    ap.add_argument("--model", default=KC.default_model_flag(),
                    help="runtime писателя двух .docx: 'claude' (штат ветки, Claude Agent SDK; "
                         "модели стадий — ORQ_WRITER_MODEL/ORQ_ENRICH_MODEL и т.д.) или 'kimi' "
                         "(точный ID из KIMI_MODEL_NAME, дефолт kimi-k2.7-code)")
    ap.add_argument("--dry-run", action="store_true", help="ресёрч без LLM — заготовки (бесплатно)")
    ap.add_argument("--no-upload", action="store_true", help="ресёрч-файлы не грузить на Диск")
    ap.add_argument("--redo", action="store_true",
                    help="переделать даже компании, у которых на Диске уже лежат реальные "
                         "документы (по умолчанию резюм их пропускает)")
    # 3-я стадия (one-pager .pdf на Kimi) ВКЛючена ПО УМОЛЧАНИЮ. Отключить: --no-presentation
    # или GEN_PRESENTATION=0. Если предусловия (venv Kimi + фото + ключ) не выполнены —
    # стадия мягко пропускается, два .docx при этом делаются как обычно.
    _pp_default = (os.environ.get("GEN_PRESENTATION", "1").strip().lower()
                   not in ("0", "false", "no", "off", "нет"))
    ap.add_argument("--presentation", dest="presentation", action="store_true",
                    default=_pp_default,
                    help="3-я стадия: one-pager .pdf на Kimi — ВКЛючена по умолчанию")
    ap.add_argument("--no-presentation", dest="presentation", action="store_false",
                    help="ОТКЛЮЧИТЬ 3-ю стадию (one-pager .pdf)")
    _pe_default = (os.environ.get("PERSON_ENRICH", "1").strip().lower()
                   not in ("0", "false", "no", "off", "нет"))
    ap.add_argument("--person-enrich", dest="person_enrich", action="store_true", default=_pe_default,
                    help="обогащать ЛПР прямыми контактами (Dadata/Checko/сайт) — ВКЛ по умолчанию; "
                         "SMTP-проверка email — env PERSON_VERIFY_EMAIL=1, соцсети — PERSON_SOCIAL=1")
    ap.add_argument("--no-person-enrich", dest="person_enrich", action="store_false",
                    help="не обогащать ЛПР прямыми контактами")
    a = ap.parse_args()
    model_is_kimi = str(a.model or "").strip().lower().startswith("kimi")
    if KC.kimi_only():
        if not model_is_kimi:
            raise SystemExit(
                "Kimi-only режим: Claude/Anthropic отключён. Используй --model kimi "
                "или явно задай ORQ_KIMI_ONLY=0 для аварийного legacy-отката.")
        a.model = "kimi"
        KC.ensure_env(require_key=not a.dry_run)
        print(f"[LLM] Kimi-only: все модельные стадии -> {KC.model_name()} ({KC.base_url()})")
    else:
        # Runtime на прогон определяет флаг --model: kimi* -> kimi (нужен ключ шлюза),
        # всё остальное -> claude. Дочерние процессы наследуют выбор через env.
        os.environ["ORQ_LLM_RUNTIME"] = "kimi" if model_is_kimi else "claude"
        if model_is_kimi:
            # Явный --model kimi сильнее унаследованного env: ensure_env ставит провайдера
            # только через setdefault, а для claude он и так выставит "claude".
            os.environ["DR_LLM_PROVIDER"] = "kimi"
        KC.ensure_env(require_key=not a.dry_run)
        if model_is_kimi:
            print(f"[LLM] Kimi runtime: все модельные стадии -> {KC.model_name()} ({KC.base_url()})")
        else:
            print(f"[LLM] Claude Agent SDK: писатель={KC.claude_model('writer')}, "
                  f"роли={KC.claude_model('enrich')}, one-pager={KC.claude_model('onepager')}, "
                  f"extract={os.environ.get('DR_EXTRACT_MODEL', 'sonnet')} "
                  f"(DR_LLM_PROVIDER={os.environ.get('DR_LLM_PROVIDER')})")
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
        default_leads_dir = os.environ.get("ORQ_LEADS_DIR", "").strip()
        if not default_leads_dir:
            default_leads_dir = r"D:\лиды" if os.name == "nt" else "/data/leads"
        os.makedirs(default_leads_dir, exist_ok=True)
        json_out = (os.path.splitext(a.out)[0] + ".json" if a.out
                    else os.path.join(default_leads_dir,
                                      "leads_" + a.industries.replace(",", "_") + ".json"))
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
    import writer_kimi as WK          # лёгкий модуль: CRA внутри него импортируется лениво
    if not a.dry_run:
        _what = ("2 .docx + one-pager .pdf" if a.presentation else "2 .docx")
        if WK.is_kimi(a.model):
            # Вилка в долларах верна ТОЛЬКО для Claude-сессий. На Kimi писателя считает
            # провайдер (gpllmkeeper), цену за вызов он наружу не отдаёт — врать вилкой нельзя.
            print(f"[оценка] {len(sel)} компаний, писатель на {WK.kimi_model(a.model)} "
                  f"({_what} на компанию). Стоимость считает провайдер Kimi — "
                  "оркестратор её не видит, в итоге будет $0.")
        else:
            # One-pager считает провайдер Kimi отдельно и цену не отдаёт — в вилку не входит.
            _lo, _hi = len(sel), 2 * len(sel)
            _tail = " (one-pager — отдельный счёт Kimi)" if a.presentation else ""
            print(f"[оценка] {len(sel)} компаний = ~${_lo}–${_hi} ({a.model}, {_what} "
                  f"на компанию){_tail}. Число = --count (по умолч. 200).")
        free = _free_ram_gb()                         # Kimi-процессы + browser crawl требуют RAM
        if free is not None and free < 3.0 and a.workers > 1:
            print(f"[ОЗУ] свободно ~{free:.1f} ГБ — снижаю параллелизм ресёрча до 1 "
                  "(параллельные Kimi-процессы и Chromium могут исчерпать память). "
                  "Освободи RAM или задай --workers вручную.")
            a.workers = 1

    # Стадия презентации опциональна и включается флагом --presentation / GEN_PRESENTATION.
    # Готовность проверяем ОДИН раз заранее — иначе один и тот же скип спамил бы по компаниям.
    gen_pdf = bool(a.presentation)
    if gen_pdf:
        ok_pp, why_pp = _presentation_prereqs()
        if not ok_pp:
            print(f"[onepager] стадия отключена: {why_pp}. Два .docx делаются как обычно.")
            gen_pdf = False
        else:
            print("[onepager] стадия включена: по каждой компании будет one-pager .pdf "
                  f"({'Kimi' if KC.runtime() == 'kimi' else 'Claude'}).")
    # Транзитная рабочая папка. Файлы здесь ВРЕМЕННЫЕ: после заливки на Я.Диск папка удаляется
    # (см. конец) — на компьютере ничего не остаётся. Предпочитаем D: (на C: мало места);
    # если D: нет — системный %TEMP%.
    tmp = tempfile.mkdtemp(prefix="orq_", dir=_tmp_root())

    store = _store_mode()                   # 'local' (хранилище сайта) по умолчанию | 'disk' (Яндекс Диск)
    if store == "local" and not a.no_upload:
        print(f"[хранилище] деливераблы -> {os.path.join(_store_root(), '<инн>')} "
              "(Диск отключён; ORQ_STORE=disk вернёт заливку на Я.Диск)")

    disk_cache = set()                      # кэш созданных путей Диска — общий на прогон (режим disk)
    if store == "disk" and not a.no_upload and not a.dry_run:   # сперва долить отложенное прошлыми прогонами
        await _drain_outbox(a.account, "долив с прошлых прогонов")
    # верхние уровни (отрасль/категория) у первого агента уже есть; mkdir идемпотентный.
    if store == "disk" and not a.no_upload:
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
    # One-pager легче прежней .pptx (нет LibreOffice+node — только вызов Kimi и рендер chromium),
    # поэтому не сериализуем его намертво, но и не даём разойтись: каждая стадия = свой chromium.
    pdf_sem = asyncio.Semaphore(max(1, int(os.environ.get("ORQ_ONEPAGER_CONCURRENCY", "2"))))
    min_ram = float(os.environ.get("ORQ_MIN_RAM_GB", "2.5"))
    # Агентный писатель с веером субагентов может работать дольше 30 минут; в текущем
    # pipeline (kimi и claude) общий дедлайн по умолчанию не ставим — у стадий свои
    # таймауты. Прежний предохранитель 1800с остаётся только для legacy-архитектуры.
    default_research_timeout = ("0" if (WK.is_kimi(a.model) or WK.is_claude(a.model))
                                else "1800")
    research_timeout = float(os.environ.get("ORQ_RESEARCH_TIMEOUT", default_research_timeout))
    pdf_timeout = float(os.environ.get("ORQ_ONEPAGER_TIMEOUT", "900"))        # сек на попытку one-pager

    async def process(idx, lead):
        async with sem:
            comp_tmp = os.path.join(tmp, str(idx))       # СВОЯ папка на компанию: изоляция рендера
            os.makedirs(comp_tmp, exist_ok=True)         # (файлы воркеров не пересекаются)
            d_tmp = os.path.join(comp_tmp, "d.docx")
            s_tmp = os.path.join(comp_tmp, "s.docx")
            p_tmp = os.path.join(comp_tmp, "p.pdf")      # one-pager (третий деливерабл)
            cost = 0.0
            findings = ""
            have_pdf = False             # готов ли реальный one-pager у этой компании
            skip_docx = False            # оба .docx уже на Диске (резюм) — доделываем только one-pager
            keep_tmp = False             # файлы не удалось ни залить, ни отложить — temp не удалять
            comp_dir = _company_dir(lead, a.base, dup_names)
            dn = DO._safe(lead.get("name"))
            bp_name, rc_name, pdf_name = _doc_names(dn)
            store_dir = os.path.join(_store_root(), DO.deliverables_subdir(lead))  # хранилище сайта (режим local)
            dest = comp_dir if store == "disk" else store_dir                      # куда лягут файлы (для логов)
            try:
                # ---- РЕЗЮМ: не переделывать (и не переоплачивать) уже готовое ----
                if not a.dry_run and not a.no_upload and not a.redo:
                    names = (bp_name, rc_name, pdf_name)
                    if store == "disk":
                        docx_done, pdf_done = await asyncio.to_thread(_remote_state, comp_dir, names)
                    else:
                        docx_done, pdf_done = _local_state(store_dir, names)
                    if docx_done and (pdf_done or not gen_pdf):
                        print(f"  ↷ [{idx}] {lead.get('name')[:40]}: уже готово — пропуск (--redo, чтобы переделать)")
                        return {"name": lead.get("name"), "ok": True, "cost": 0.0,
                                "dir": dest, "resumed": True, "pdf": pdf_done, "files": 0}
                    if docx_done:
                        skip_docx = True    # догоняем только one-pager
                        # .docx уже готовы — находки нужны только как справка для боли
                        # на слайде 3, поэтому легаси-кэш здесь годится: гонять движок
                        # ради одного абзаца незачем.
                        findings = _cached_findings(lead.get("name"), lead.get("_inn"),
                                                    pass_="process", legacy_ok=True)
                        print(f"  ↷ [{idx}] {lead.get('name')[:40]}: .docx уже готовы — делаю только one-pager")

                if a.dry_run:
                    try:
                        await asyncio.to_thread(DO.generate_dossier, lead, d_tmp)
                        await asyncio.to_thread(DO.generate_strategy, lead, s_tmp)
                        if gen_pdf:         # паритет с .docx: в dry-run кладём валидную болванку .pdf
                            await asyncio.to_thread(DO.generate_presentation, lead, p_tmp)
                            have_pdf = os.path.exists(p_tmp) and os.path.getsize(p_tmp) > 5000
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
                    # Ретраим ПО ФАКТУ отсутствия .pdf (>5КБ): ловим и исключения (таймаут, краш
                    # подпроцесса), и «тихие» сбои. Параллелизм ограничен pdf_sem: каждая стадия —
                    # свой python + chromium (ORQ_ONEPAGER_CONCURRENCY, дефолт 2).
                    if gen_pdf:
                        async with pdf_sem:
                            for attempt in range(3):
                                await _wait_ram(min_ram, f"[{idx}] [onepager]")
                                await _attempt(
                                    _onepager_one(lead, idx, p_tmp, findings),
                                    pdf_timeout, f"    [{idx}] [onepager retry {attempt + 1}/3]")
                                if os.path.exists(p_tmp) and os.path.getsize(p_tmp) > 5000:
                                    break                       # one-pager готов
                                if attempt < 2:
                                    print(f"    [{idx}] [onepager retry {attempt + 1}/3]: .pdf не получен — повтор")
                                    _rm(p_tmp)                  # убрать недописанный перед повтором
                                    await asyncio.sleep(4)
                        have_pdf = os.path.exists(p_tmp) and os.path.getsize(p_tmp) > 5000
                        if not have_pdf:
                            print(f"    [{idx}] [onepager] .pdf не получен (>5КБ) за 3 попытки — "
                                  + (".docx уже на Диске" if skip_docx else "зальём только два .docx"))

                pairs = [] if skip_docx else [(d_tmp, bp_name), (s_tmp, rc_name)]
                if have_pdf:            # one-pager — рядом с .docx (если получился)
                    pairs.append((p_tmp, pdf_name))
                if not a.no_upload and pairs:
                    if store == "local":
                        # ХРАНИЛИЩЕ САЙТА: копируем готовые файлы под красивыми именами; их отдаёт веб
                        try:
                            os.makedirs(store_dir, exist_ok=True)
                            for lp, rname in pairs:
                                await asyncio.to_thread(shutil.copy2, lp, os.path.join(store_dir, rname))
                        except Exception as e:
                            keep_tmp = True     # копия не удалась — не теряем оплаченные файлы
                            print(f"  [!] сохранение в хранилище {lead.get('name')}: {e} — файлы в {comp_tmp}")
                            return {"name": lead.get("name"), "ok": False, "cost": cost,
                                    "why": "сохранение в хранилище", "kept": comp_tmp}
                    else:
                        try:
                            await asyncio.to_thread(DO.ensure_dir, comp_dir, a.account, disk_cache)
                            for lp, rname in pairs:
                                # с проверкой чтением: «✓ Загружено» от Диска — не доказательство
                                await asyncio.to_thread(_upload_verified, lp, comp_dir, rname, a.account)
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
                tag = "  +one-pager" if have_pdf else ""
                if skip_docx:
                    tag += ("  (докинут только one-pager)" if have_pdf
                            else "  (one-pager не вышел — прежние .docx на месте)")
                print(f"  ✓ [{idx}] {lead.get('name')[:40]} -> {dest}" + tag
                      + (f"  (${cost:.2f})" if cost else ""))
                return {"name": lead.get("name"), "ok": True, "cost": cost, "dir": dest,
                        "pdf": have_pdf, "files": len(pairs)}
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
    n_pdf = sum(1 for r in ok if r.get("pdf"))
    n_files = sum(r.get("files") or 0 for r in ok)   # реально сделанных/залитых в ЭТОТ прогон
    print(f"\n[ГОТОВО] компаний: {len(ok)}/{len(sel)}"
          + (f" (из них {resumed} по резюму, без затрат)" if resumed else "")
          + f" | файлов за прогон: {n_files}"
          + (f" (в т.ч. {n_pdf} one-pager'ов)" if n_pdf else "") + " "
          + ("(в temp, без сохранения) " if a.no_upload
             else f"в {a.base} " if store == "disk"
             else f"в хранилище {_store_root()} ")
          + (f"| стоимость ~${total:.2f}" if total else "| $0"))
    if fails:
        print(f"[ВНИМАНИЕ] {len(fails)} компаний остались БЕЗ свежих документов "
              "(на Диске у них лежат заготовки ФАЗЫ 1):")
        for r in fails:
            print(f"  - {(r or {}).get('name')}: {(r or {}).get('why') or 'сбой'}")
        print("  Повтори ту же команду: резюм пропустит готовые компании и доделает только эти.")
    if store == "disk" and not a.no_upload and not a.dry_run:
        await _drain_outbox(a.account, "финальный долив")
    kept = [r.get("kept") for r in results if r and isinstance(r, dict) and r.get("kept")]
    if a.no_upload:
        print(f"[локально] .docx/.pdf во временной папке: {tmp}")
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
