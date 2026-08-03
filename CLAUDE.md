# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Оркестратор лидогенерации для B2B-агентства, продающего корпоративную on-premises LLM-платформу
**Telepatt** от **АО «ЦИТ РТ»** (RAG-база знаний, автономные ИИ-агенты, ИИ-Коуч «Наставник»).
Корень репозитория — **`D:\lead_gen`**, рабочая ветка **`kimi`**. Ядро пайплайна — `lead_orchestrator/`;
изолированные Kimi-агенты Фазы 2 — `lead_orchestrator_kimi/` (свой venv); веб — `web/`;
контейнеризация — `Dockerfile`/`docker-compose.yml`.
Python 3.12, основная платформа Windows, прод-цель — Docker в Linux. Комментарии в коде — на русском.

Оркестратор **детерминированный Python**, а не LLM-оркестратор: модель принимает решения только
внутри стадий (NL-контроллер, ресёрч, enrichment, писатель), а порядок и устойчивость — код.

**Сателлитные доки — здесь НЕ дублируются, читать по ссылке:**
`lead_orchestrator_kimi/CLAUDE.md` (патчи venv, канон блоков one-pager, реальный API Kimi SDK,
вёрстка полосы метрик), `web/README.md` (ручки API и решения веба), `DEPLOY_TIMEWEB.md` (прод),
`AUDIT_KIMI.md` (аудит Kimi-стадии).

## Runtime: Kimi-only

Штатный режим всего агента — **Kimi-only**: NL-контроллер, LLM-экстракт движка ресёрча,
пять enrichment-ролей, писатель двух `.docx` и one-pager работают на одной модели
`kimi-k2.7-code` через OpenAI-совместимый шлюз `KIMI_BASE_URL`.

- `ORQ_KIMI_ONLY` — **кодовый дефолт `1`** (`kimi_config.py:35`), а не только env лаунчера.
- В этом режиме ID модели зафиксирован: любое другое `KIMI_MODEL_NAME` → fail-fast до первого
  запроса (`kimi_config.model_name()`).
- `--model` у `orchestrator.py` имеет **дефолт `kimi`**; любое не-kimi значение под guard'ом
  роняет запуск в `SystemExit`. Ветка Claude недостижима без явного `ORQ_KIMI_ONLY=0`.
- Ключ берётся в порядке `KIMI_API_KEY` → `GPLLM_API_KEY`; на этой машине задан второй.
- Веб принимает только `model=kimi` (`/api/models` возвращает один пункт).

Ветка Claude сохранена в коде исключительно для аварийного отката (`_research_one` ниже
kimi-диспатча, `PRESALE_SYSTEM`, `ORQ_SCOUT_MODEL`/`ORQ_CRITIC_MODEL`/`ORQ_VERIFIER_MODEL`,
`ORQ_CRAWL_*`). Это **не** поддерживаемый рабочий дефолт — не «чинить» её попутно.

## Запуск

Ярлык на рабочем столе **`new_orchestrator`** → `run_orchestrator.cmd` (канонический делегат) →
`run_kimi_orchestrator.cmd` → NL-контроллер `orchestrator_agent.py` → **`orchestrator.py`**.
Лаунчер выставляет `KIMI_API_KEY`←`GPLLM_API_KEY`, `KIMI_BASE_URL`, `KIMI_MODEL_NAME`,
`LEAD_SOURCE=rusprofile`, `RUSPROFILE_BROWSER=playwright`, `DR_LLM_PROVIDER=kimi`, `ORQ_KIMI_ONLY=1`;
отсутствие ключа/cookie — жёсткая ошибка ДО запуска, а не тихий пропуск стадии.

⚠️ **Лаунчеры намеренно не зовут `py`.** Они резолвят `ORQ_MAIN_PY` →
`%LOCALAPPDATA%\Programs\Python\Python312\python.exe` («py launcher may have no registered runtime»).
Та же переменная — часть контракта между окружениями: `lead_orchestrator_kimi/leadgen_tools.py:49`
по ней находит основной venv для read-only веб-моста Kimi→движок. Задавая её вручную, указывай
питон с зависимостями пайплайна.

Оба `.cmd` — строго **ASCII+CRLF**: cmd.exe портит UTF-8/LF батники.

```
# полная цепочка (сбор + ресёрч + 3 файла), ВСЯ отрасль (по умолчанию 200 компаний):
py orchestrator.py --industries mining       # = py orchestrator.py mining (позиционно — только ключи INDUSTRY)
py orchestrator.py mining --count 10         # явно 10 ВСЕГО по всем отраслям
py orchestrator.py mining,energy --per-industry 10   # 10 НА КАЖДУЮ отрасль (итог 20)
# только ресёрч+материалы по готовому JSON лидов (ФАЗА 1 пишет их в D:\лиды\):
py orchestrator.py "D:\лиды\leads_mining.json"
py orchestrator.py "D:\лиды\leads_mining.json" --no-presentation   # без 3-й стадии, только 2 .docx
py orchestrator.py "D:\лиды\leads_mining.json" --no-upload         # файлы остаются в temp, путь печатается
py orchestrator.py "D:\лиды\leads_mining.json" --dry-run --no-upload  # заглушки, без LLM и без следов
py orchestrator.py sample_ryazanavtodor.json --no-upload           # разовый прогон одной компании
# только официальная база контактов — детерминированно, без LLM и без .docx:
py company_research_agent.py --contacts "АО Рязаньавтодор 6234065445"
# разовый прямой контакт ЛПР (ФИО+ИНН -> рабочие email/телефоны, только легитимные источники).
# ⚠️ standalone-дефолты ПРОТИВОПОЛОЖНЫ оркестраторным: SMTP-проба и соцпоиск ВКЛ:
py person_enrich.py "Руденко Сергей Александрович" 6234065445 --no-verify --no-social
# движок deep_research отдельно, БЕСПЛАТНО (regex-only, без LLM):
DR_USE_LLM=0 py deep_research_engine.py --company "АО Рязаньавтодор" --inn 6234065445 --site https://avtodor-rzn.ru
py deep_research_engine.py --crawl avtodor-rzn.ru      # отладка: только краул сайта (SiteCrawler)
# ФАЗА 1 отдельными источниками:
py rusprofile_session.py --login                       # разовый логин, cookie в env/rusprofile_cookies.json
py source_rusprofile.py --industries processing --min-revenue 1e9 --per-industry 10 --out leads.json
py source_ofdata.py --industries processing --region Татарстан --out leads.json
py source_checko.py --industries processing --min-revenue 1e9 --region Татарстан --out leads.json
# Docker (из корня D:\lead_gen; секреты в env/.env):
docker compose build && docker compose run --rm lead-orchestrator mining --count 10
# веб-интерфейс (из корня; подробности web/README.md):
py -m uvicorn web.api.main:app --port 8000             # затем web/ui: npm run dev / npm run build
```

Через ярлык достаточно назвать **только отрасль** («mining», «добыча угля», «нефтегаз»…):
контроллер соберёт **200** компаний (если не задано иное), а перед большим боевым прогоном
покажет точный объём и попросит подтверждение.

**Флаги `orchestrator.py`:** `--count N` (ВСЕГО, дефолт 200), `--per-industry N` (НА КАЖДУЮ отрасль,
перекрывает `--count`; в NL-обёртке это `count_per_industry`), `--min-revenue` (1e9),
`--region "..."` (поддерживает ОТРИЦАНИЕ: `"НЕ Москва"`, `!X`, `-X`, `кроме X`; смешивание через
запятую), `--workers N` (2, авто→1 при <3 ГБ RAM — только в боевом запуске, в dry-run проверки нет),
`--model kimi`, `--no-presentation`, `--no-person-enrich`, `--show-browser`, `--out <json>`
(дефолт `D:\лиды\leads_<отрасли>.json`), `--base` (`disk:/Лиды`), `--account`, `--redo`
(ВЫКЛючить резюм). NL-обёртка прокидывает лишь подмножество (нет `--no-person-enrich`/`--out`/
`--base`/`--headless`).

**Разовые/партийные точки входа** (не канон пайплайна, но живые): `run_cit_kimi.cmd` — фиксированный
прогон по `cit_lead.json` (только 2 `.docx`, `ORQ_STORE=local`, самопроверка `CIT_KIMI_VERIFY=1`);
`run_kimi_oil28.cmd` / `.ps1` (он же `run_kimi_orchestrator.cmd oil28`) — партия из
`rusprofile_28_pending.json` в `D:\лиды_нефтяная_отрасль`. В корне репо трекаются одноразовые
подготовители этой партии `_prepare_rusprofile_28.py` / `_prepare_researched_subset.py`;
сами `rusprofile_28_*.json` — гитигнор. Ничего из этого не трогать при правках пайплайна.

## Проверка правки

Тесты **не pytest** — это самостоятельные скрипты, поэтому «прогнать один тест» = запустить один файл.
Гонять из `lead_orchestrator/`.

```
py -m py_compile orchestrator.py writer_kimi.py deep_research_engine.py company_research_agent.py
py test_kimi_only.py             # единый Kimi K2.7 runtime и запрет Claude в штатных entrypoint
py test_kimi_agent_freedom.py    # контракт свободного Kimi Agent loop
py test_research_enrichment.py   # scheduler/контракты/cache/DOCX-adapter пяти ролей (вкл. деградацию)
py test_rusprofile_playwright.py # cookie-нормализация, порог 1 млрд, revenue-sort по убыванию
py test_source_ofdata.py         # формы /finances, включительный порог, ключ не в URL
DR_USE_LLM=0 py test_deep_research.py   # смоук движка: экстракт, completeness_critic, петля добора
py orchestrator_agent.py --selftest     # план NL-контроллера -> argv
py kimi_research_cli.py --selftest      # subprocess-мост URL-инструментов
py connectors\yadisk_mcp.py --selftest  # ядро Яндекс Диска офлайн
py ..\web\api\test_events.py            # парсер stdout -> SSE на НАСТОЯЩИХ логах D:\orq_tmp\run_*.log
py orchestrator.py sample_ryazanavtodor.json --dry-run --no-upload   # вся цепочка офлайн, заглушки
```

В venv Kimi-папки — ещё два (см. её `CLAUDE.md`):
`.venv_kimi\Scripts\python.exe writer_kimi_agent.py --selftest` и
`... research_enrichment_agent.py --selftest`, плюс `patches/apply_patches.py --check`.

**Docker build-gate** (`Dockerfile`) падает, если не прошли: `py_compile`, `test_deep_research.py`,
`test_source_ofdata.py`, `test_rusprofile_playwright.py`, `test_kimi_only.py`,
`test_kimi_agent_freedom.py`, `test_research_enrichment.py`, `apply_patches.py --check` и импорт
Kimi-стадии. Добавил тест — добавь его и в гейт.

⚠️ `web/api/test_events.py` гонять при ЛЮБОЙ правке печати в `orchestrator.py`: веб парсит именно
эти русские префиксы, структурированных событий у оркестратора нет. Переименовал строку вывода —
молча сломал прогресс в UI.

## ФАЗА 1 — сбор

Источник выбирается env `LEAD_SOURCE`. **Кодовый дефолт и desktop-лаунчер — `rusprofile`**
(`orchestrator.py:650`, `run_kimi_orchestrator.cmd:38`). `ofdata` выставлен **только в Docker**
(`Dockerfile`, `docker-compose.yml`), потому что в образе нет Chrome. `checko` — второй API-откат.

- **`rusprofile`** (`source_rusprofile.py` + `rusprofile_playwright.py`) — штатный локальный путь.
  Playwright грузит cookie из игнорируемого `env/rusprofile_cookies.json`, внутри страницы зовёт
  advanced-search по ОКВЭД с СЕРВЕРНЫМ `finance_revenue_from` (не ниже 1 млрд ₽), читает до
  `MAX_SEARCH_PAGES`=20 страниц и **явно сортирует их по выручке по убыванию**. Только после
  сортировки открывается ровно по одной карточке для выбранных N компаний. Карта `INDUSTRY` — 21 отрасль.
- **`ofdata`** (`source_ofdata.py`) — `/search` → выручка (`/finances`, а на бесплатном тарифе
  автоматический fallback на ГИР БО ФНС) → обязательный порог ≥1 млрд → `/company` только для
  прошедших. POST form-urlencoded, ключ не попадает в URL/логи; ретраи 429/5xx.
  Переиспользует `usable_okved`/`resolve_region`/`extract_company_contacts` из Checko.
- **`checko`** (`source_checko.py`) — drop-in для `harvest()`: `/search`, выручка из ГИР БО,
  контакты `/company`.

Дальше: отбор → `D:\лиды\leads_<отрасли>.json` (`--out`; .xlsx из боевой ФАЗЫ 1 убран 2026-07-06)
→ при `ORQ_STORE=disk` ещё и дерево `<--base>/<отрасль>/<категория полноты контактов>/<компания>/`
с 3 файлами-заглушками, которые ФАЗА 2 перезапишет. Категория — по наличию email/телефона/сайта/ЛПР
(`disk_organize.category_for`), имена деливераблов — `disk_organize._doc_names` (обязаны совпадать
с `orchestrator._doc_names`).

## ФАЗА 2 — ресёрч и материалы (важно — не сломать)

Выход на компанию — **ТРИ файла**:
1. `<Компания>_карта_бизнес-процессов.docx` — нейтральный аналитический отчёт: профиль → процессы
   as-is → боли → ИИ-решение → to-be, сводная таблица, дорожная карта, специфика 44/223-ФЗ, оговорки.
2. `<Компания>_карта_ролей_и_контактов_пресейл.docx` — нейтральный: оргструктура, ЛПР, профильные
   отделы, филиалы, официальные контакты, план захода, оговорки.
3. `<Компания>_презентация_Telepatt.pdf` — клиентский one-pager (см. ниже).

Порядок в `_research_one_kimi` (`orchestrator.py:234`) — менять только осознанно:

1. **Пред-запуск движка ДО сессии писателя, ДВА прохода.** `RESEARCH_PASSES` гоняет
   `deep_research_engine.deep_research()` дважды со своими `aspects`: проход **`process`** (профиль,
   процессы as-is, ИТ-ландшафт, госконтракты, точки внедрения ИИ) и проход **`roles`** (оргструктура,
   ЛПР, закупки, филиалы, контакты, соцпрофили, учредитель/ведомство). Каждый документ получает
   находки СВОЕГО прохода. Раньше был один общий проход, и добор под роли разменивался на добор под
   процессы — **не «упрощать» обратно к одному вызову**.
   ⚠️ НЕ вызывать движок как live-тул ВНУТРИ сессии писателя: вложенные SDK-сессии сбивают писателя,
   и он не сохраняет документы. Сбой прохода компанию не валит (останется хотя бы официальная база).
2. **Обогащение ЛПР (`person_enrich`)** — тоже детерминированно, вне SDK-сессии: по `contact_person`+ИНН
   прямые рабочие контакты; блок дописывается к находкам прохода `roles` (в `process` не идёт).
   ВКЛ по умолчанию; тихо пропускается, если у лида нет `contact_person` или `_inn`.
3. **Пять enrichment-ролей (подпроцесс venv Kimi).** Граф
   `official_sources → (corporate_contour + secondary_sources) → role_candidates → candidate_contacts`.
   Контактник не видит общий seed персоналий — только структурированный список кандидатов.
   Ответы проходят enum/URL/date/confidence/source-policy validation; сниппет без Fetch/Crawl
   доказательством не считается. Checkpoint атомарный, проверяет schema/prompt/input/dependency hash
   + TTL, поэтому старые параллельные кэши не принимаются. Trusted-домены official-роли засеяны
   сайтом лида И доменом его почты из Фазы 1, чтобы валидатор не отвергал уже доказанный домен.
   ⚠️ **Мягкая деградация (2026-07-22, `4317dc4`): сбой роли НЕ роняет компанию.** Упавшая роль и её
   downstream пишутся в `roles_failed`, наружу идёт частичное досье (`complete=False`), писатель
   добирает недостающее из находок движка. Вызов обёрнут в try/except — **писатель запускается ВСЕГДА**
   (`orchestrator.py:280-299`). Раньше любая ошибка роли → `RuntimeError` → exit 1 → писатель вообще
   не стартовал, а лог винил писателя («не сохранил .docx»). **Не возвращать `raise` в `run()`.**
   Родитель (`writer_kimi.run_research_subagents`) поднимает НАСТОЯЩУЮ причину из stdout, а не хвост
   stderr с шумом `authlib.jose` DeprecationWarning.
4. **Писатель** — `writer_kimi.write_two_docx`: агент в отдельном venv, на каждый документ свой
   главный писатель со свободным read-only веб-поиском + динамический веер `scout`/`verifier` +
   обязательный `critic`, свой системный промпт (`PROCESS_MAP_SYSTEM` / `ROLES_CONTACTS_SYSTEM` из CRA)
   и свои находки-seed со своего прохода → финальный JSON → общие рендереры CRA.
   `apply_research_enrichment` машинно переносит критические таблицы в DOCX. URL-инструменты идут
   subprocess-мостом через `kimi_research_cli.py` — SDK не смешиваются. Legacy HTTP-режим —
   `KIMI_WRITER_AGENT=0`.
   На Kimi долларовая вилка `[оценка]` не печатается (шлюз цену наружу не отдаёт → итог `$0`, не баг).
5. **Гард против болванок:** наружу идут только реальные `.docx` (>5000 байт, проверка в `process()`);
   иначе компания = неуспех.

### Третья стадия — one-pager (`_onepager_one`, по умолчанию ВКЛ)

НЕ агентная SDK-сессия и НЕ python-рендерер: оркестратор запускает **подпроцесс venv-питона соседней
папки** — `../lead_orchestrator_kimi/.venv_kimi/Scripts/python.exe onepager_kimi.py <компания>
--industry … --pain … --out <p.pdf>`. Kimi выдаёт самодостаточный HTML → Playwright рендерит PDF.

Почему подпроцесс: `kimi-agent-sdk` тянет `pydantic-core 2.41.5`, а `claude-agent-sdk`/`anthropic`
требуют `2.46.4` — в одном интерпретаторе не живут. Поэтому Kimi-стадии изолированы в своём venv.

Блоки листа 1–3, 6, 8 (шапка/герой/фичи/спикер/футер) — **КАНОН**, одинаковы у всех; под заказчика
пишется только «Одна проблема — одно решение» + подписи метрик. Боль берётся из находок ресёрча
(`_main_pain`). Отключить — `--no-presentation` / `GEN_PRESENTATION=0`.
Гард: заливается только реальный `.pdf` (>5000 байт); сбой стадии компанию НЕ валит (2 `.docx` уже
готовы), ретрай 3×, таймаут `ORQ_ONEPAGER_TIMEOUT`. Стоимость провайдер наружу не отдаёт, поэтому
в `[оценка] ~$N` one-pager не входит.

### Хранилище

Дефолт `ORQ_STORE=local` (2026-07-15): три файла ложатся в `ORQ_DATA_ROOT/deliverables/<ИНН>/`,
откуда веб отдаёт их на скачивание (`GET /api/leads/{инн}/file/{slug}`, slug = `process|roles|onepager`).
`ORQ_STORE=disk` возвращает прежнюю заливку на Яндекс Диск — тогда работают резюм по Диску, outbox
и раскладка-заглушки ФАЗЫ 1.

## Карта кода

Только то, что нужно знать до правки; утилиты (`email_finder`, `site_verify`, `harvest_inn_site`,
`inn_util`, `webutil`, `browser_util`, `build_excel`, `dadata_enrich`, `checko_enrich`,
`revenue_enrich`, `okved2_codes`) открываются по имени.

| Файл | Роль |
|---|---|
| `orchestrator.py` | **главный вход ФАЗА 1+2** (asyncio). `_collect` = ФАЗА 1; `_research_one_kimi` = штатный ресёрч+2 .docx; `_research_one` = диспатч (ниже него — legacy Claude); `_onepager_one` = 3-я стадия; `_upload_verified`/`_remote_state`/`_flush_outbox` = устойчивость; `_no_sleep` блокирует сон Windows |
| `orchestrator_agent.py` | Kimi NL-контроллер: строгий JSON-план → валидация → запуск `orchestrator.py --model kimi` подпроцессом; дефолт count=200 |
| `kimi_config.py` | единая конфигурация Kimi: ключ, endpoint, модель, `child_env`, Kimi-only guard |
| `writer_kimi.py` | **штатный писатель двух .docx** и parent-мост пяти enrichment-ролей: сначала отдельный Kimi-процесс делает граф ролей, затем на документ запускается `writer_kimi_agent.py` |
| `kimi_research_cli.py` | subprocess-мост URL-инструментов (search/fetch/crawl) из venv Kimi в основной; `--selftest` |
| `company_research_agent.py` (CRA) | **общие схемы, промпты и DOCX-рендереры.** `PROCESS_MAP_SYSTEM`/`ROLES_CONTACTS_SYSTEM` берёт Kimi-писатель; импорт модуля НЕ загружает Claude SDK. Детерминированно дописывает таблицы центров решений, корпоративного графа, кандидатов, каналов и реестр доказательств |
| `deep_research_engine.py` | **настоящий deep-research**: официальная база + site/eis/courts_media/hh, Crawl4AI→HTTP, targeted refill и `completeness_critic`; поиск brave→ddg→bing. HTTP-фетч защищён от `file://`, credentials, localhost/private/link-local IP и небезопасных redirect. Мульти-домен подтверждается по ИНН/полному названию; каталоги режутся `AGGREGATORS` |
| `person_enrich.py` | ЛПР: ФИО+ИНН → прямой РАБОЧИЙ контакт (Dadata/Checko → домен с валидацией → email по шаблону+MX, телефоны; соцпрофили только с ИНН-контекстом). «Пробив»/утечки конструктивно исключены (`DENY_SOURCES`) |
| `source_rusprofile.py` / `rusprofile_playwright.py` / `rusprofile_session.py` | штатный источник Фазы 1: карта `INDUSTRY` (21), регион-фильтр с отрицанием; единая Playwright-context; создание cookie (`--login`, UC остался как `RUSPROFILE_BROWSER=uc`) |
| `source_ofdata.py` / `source_checko.py` | API-пути (Docker и откат) |
| `disk_organize.py` | пути/имена на Диске и в локальном хранилище поверх `connectors/yadisk_client` (`_mkdir`/`_upload` с ретраями на 423 и транзиентные сбои), заглушки .docx/.pdf (`_make_pdf` — голый stdlib-PDF, текст транслитерирован: базовые шрифты PDF кириллицу не несут) |
| `connectors/` | `yadisk_client.py` — ядро Яндекс Диска на официальном REST API (stdlib); `yadisk_mcp.py` — MCP-обёртка над ним |
| `project_env.py` | тихая загрузка `env/.env` в desktop/CLI без печати значений (`ORQ_ENV_FILE` перекрывает путь) |
| `pipeline.py` | отбор `_select` + сохранение `_save`; его СОБСТВЕННАЯ цепочка `run()` работает только при прямом `py pipeline.py` — оркестратор её не вызывает |
| `../lead_orchestrator_kimi/` | **изолированные Kimi-агенты** (свой venv): `research_enrichment_agent.py` (пять ролей, schema v3, evidence trace), `writer_kimi_agent.py` (агентный писатель одного документа), `onepager_kimi.py`+`html_to_pdf.py` (3-я стадия), `leadgen_tools.py` (read-only мост). Свой `CLAUDE.md` — читать перед правкой |
| `web/` | FastAPI + React/Vite. Спавнит `orchestrator.py` подпроцессом и парсит его stdout в SSE; один активный прогон (второй → 409). Свой `README.md` |
| `Dockerfile` / `docker-compose.yml` | два venv в образе (`/opt/venv` + `/opt/kimi-venv`), БЕЗ Chrome/Xvfb → `source_rusprofile` в контейнере неработоспособен. Сборка = build-gate (см. «Проверка правки») |
| `assets/` | `bulat_zamaliev.png` — фото эксперта (вшивается в one-pager). `citrt_logo.png` остался от .pptx-стадии; в one-pager логотип — текстовый словомарк |

## Устойчивость прогона

Прогон рассчитан на перезапуск **ТОЙ ЖЕ командой**.

- **Резюм.** Перед компанией проверяются размеры её файлов (`_remote_state` для Диска /
  `_local_state` для хранилища): оба `.docx` >5 КБ → пропуск; `.docx` есть, а `.pdf` нет или это
  заготовка (реальный one-pager — `REAL_PDF_MIN`=60 КБ) → доделывается ТОЛЬКО one-pager.
  `--redo` отключает. ⚠️ Резюм ищет файлы **по имени**: после переименования третьего деливерабла
  (`.pptx`→`.pdf`, 2026-07-13) у компаний, сделанных раньше, презентация считается отсутствующей.
  Раскладка ФАЗЫ 1 (`_put_stub`) существующие файлы НЕ перезаписывает — повторный полный прогон
  не затирает реальные документы и не ломает резюм.
- **Outbox.** Сбой заливки НЕ удаляет оплаченные файлы: они откладываются в
  `D:\orq_outbox\<компания>_<ts>\` (+meta.json) и доливаются автоматически в начале и конце
  следующего прогона (`_flush_outbox`).
- **Кэш находок движка** — `D:\orq_cache\findings_<ИНН>__<проход>.md`, ОТДЕЛЬНЫЙ файл на каждый
  проход (TTL `ORQ_FINDINGS_TTL_H`=72 ч). ⚠️ Откат на легаси-кэш от старого ОДНОГО прохода
  (`findings_<ИНН>.md` без суффикса) по умолчанию ВЫКЛЮЧЕН (`legacy_ok=False`): иначе оба прохода
  прочитали бы один файл и двухпроходность молча выродилась бы в старую схему.
- **Таймауты.** `ORQ_RESEARCH_TIMEOUT`=0 для Kimi (без общего дедлайна), 1800 с для legacy Claude;
  `ORQ_ONEPAGER_TIMEOUT`=900 с на попытку. При таймауте подпроцесс убивается — иначе висел бы
  осиротевший python+chromium. Ретрай 3×.
- **RAM-бэкпрешер** — перед тяжёлой стадией ожидание свободной RAM ≥ `ORQ_MIN_RAM_GB`=2.5 ГБ (до 5 мин);
  one-pager ограничен `ORQ_ONEPAGER_CONCURRENCY`=2 (каждая стадия — свой python+chromium).
- **Изоляция сбоев:** `gather(..., return_exceptions=True)` + ловля `BaseExceptionGroup` (anyio так
  оборачивает крах CLI) — сбой одной компании не валит прогон; Ctrl+C/SystemExit пробрасываются.
- **Временные файлы:** каждая компания работает в `D:\orq_tmp\orq_XXXX\<idx>\`; после заливки папка
  удаляется СРАЗУ (не копим все 200). temp на D:, т.к. C: переполнен. При `--no-upload` остаётся локально.
- **Лог всегда:** stdout+stderr дублируются в `D:\orq_tmp\run_<ts>.log` (`_Tee`); в конце — список
  компаний, оставшихся с заготовками. Exit-код 3 — если не удалась НИ одна компания.
- **ФАЗА 1:** harvest и сессия контактов ретраятся (2×, свежий браузер); Cloudflare ждётся по факту
  CSRF-cookie (до 30 с); контакты чекпойнтятся каждые 20 карточек; протухшая платная сессия =
  SystemExit с подсказкой `--login`, а не молчаливый сбор без контактов.

## Окружение

**Ключи.** `KIMI_API_KEY` → фолбэк `GPLLM_API_KEY` (обязателен). `OFDATA_API_KEY` — для ofdata-пути.
Опционально: `DADATA_TOKEN`, `CHECKO_TOKEN` (+`_ALT`/`_2`/`_3`), `FIRECRAWL_API_KEY`,
`YANDEX_DISK_TOKEN` (нужен только при `ORQ_STORE=disk`). Anthropic-аутентификация относится только
к явно включённому legacy-откату.

Секреты живут в gitignored `env/.env` (`.env.example` в репо нет); Docker подключает его через
`env_file`, в build context папка не попадает.

**Зависимости:** `py -m pip install -r requirements.txt` (pinned-lock) + `python -m playwright install chromium`.
Реально несущие пакеты: **`openai`** (весь штатный LLM-трафик к Kimi идёт через `AsyncOpenAI` —
ленивый импорт в `deep_research_engine`, `writer_kimi`, `orchestrator_agent`), **`playwright`**
(Фаза 1 и рендер PDF), `python-docx`, `Crawl4AI`, `openpyxl`.
⚠️ **Шапка `requirements.txt` устарела:** она числит `claude-agent-sdk` прямой зависимостью,
`openai`/`playwright` — транзитивными, а `markitdown`/`python-pptx`/`pillow` — тулчейном
`.pptx`-стадии, удалённой 2026-07-13. Пины рабочие; верить надо этому разделу, а не комментариям в файле.

**Яндекс Диск:** и пайплайн (`disk_organize.py`), и MCP-сервер `yadisk` (зарегистрирован в `.mcp.json`)
идут через одно ядро `connectors/yadisk_client.py` (REST API, stdlib). Нужен `YANDEX_DISK_TOKEN`
со scope `cloud_api:disk.write`. CLI `yacli` для Диска больше НЕ используется.
Аккаунты (2026-07-06): `YANDEX_DISK_TOKEN` = РАБОЧИЙ Диск **AI-CIT.RT@yandex.com** — пайплайн пишет туда;
`YANDEX_DISK_TOKEN_TEST` = прежний тестовый **TESTosteronthegreat** (старый `disk:/Лиды`, 51 компания,
кодом не используется). ⚠️ Уже открытые консоли наследуют старое окружение — новый токен подхватится
только после перезапуска (ярлык открывает свежую консоль).

**Переключатели режима:** `LEAD_SOURCE` (`rusprofile` дефолт | `ofdata` | `checko`),
`ORQ_STORE` (`local` дефолт | `disk`), `DR_LLM_PROVIDER` (`kimi` дефолт | `claude` — только с `ORQ_KIMI_ONLY=0`),
`ORQ_KIMI_ONLY` (`1`).

**Портируемость путей** (для Docker/Linux; на Windows работают дефолты): `ORQ_DATA_ROOT` (корень
служебных папок; в контейнере `/data`), `ORQ_LEADS_DIR` (`D:\лиды` / `/data/leads`),
`ORQ_DELIVERABLES_DIR` (перекрывает `ORQ_DATA_ROOT/deliverables`), `ORQ_DELIVERABLE_KEY`,
`ORQ_MAIN_PY`, `ORQ_ENV_FILE`, `KIMI_DIR`/`KIMI_PY` (папка и python venv Kimi-стадии),
`RUSPROFILE_PROFILE_DIR`/`RUSPROFILE_COOKIES_FILE`. Веб читает ещё `ORQ_REPO_ROOT`, `ORQ_PYTHON`,
`ORQ_DISK_BASE`, `ORQ_WEB_JOBS_DIR`.

**Тумблеры движка:** `DR_USE_LLM` (1; `0` = regex-only, бесплатно), `DR_USE_CRAWL4AI` (1; `0` → HTTP-фолбэк),
`DR_PAGE_CHARS` (9000), `DR_BREADTH`/`DR_DEPTH` (4/2), `DR_MAXPAGES` (25), `DR_MAX_DOMAINS` (3),
`DR_LLM_CONCURRENCY` (2), `DR_CRAWL_CONCURRENCY` (4), `DR_SEARCH_INTERVAL` (1.6 с),
`DR_SEARCH_CONCURRENCY` (1), `DR_LLM_TIMEOUT` (300), `DR_KIMI_MAX_TOKENS` (8000),
`DR_FETCH_MAX_BYTES` (5 МиБ), `DR_EXTRACT_MODEL` (sonnet — legacy).

**Тумблеры Kimi-стадий:** `KIMI_WRITER_AGENT` (1; `0` = прежний одиночный HTTP-режим),
`KIMI_WRITER_AGENT_TIMEOUT` (0 = без общего таймаута), `KIMI_WRITER_ATTEMPTS` (3),
`KIMI_WRITER_MAX_STEPS`/`KIMI_WRITER_THINKING` (пусто = config Kimi CLI),
`KIMI_WRITER_MAX_TOKENS` (16000) / `KIMI_WRITER_TIMEOUT` (600) — только legacy HTTP;
`KIMI_RESEARCH_SUBAGENTS_TIMEOUT` (2400), `KIMI_RESEARCH_COMPANY_CONCURRENCY` (2),
`KIMI_RESEARCH_SUBAGENT_ATTEMPTS` (2), `KIMI_RESEARCH_SUBAGENT_MAX_STEPS`,
`KIMI_RESEARCH_CHECKPOINT_TTL_H` (72), `KIMI_TOOL_LOG_DIR`, `ORQ_KIMI_TOOL_TIMEOUT` (180 с на вызов),
`ORQ_RESEARCH_TOOL` (путь к мосту).

**Тумблеры ЛПР:** `PERSON_ENRICH` (1), `PERSON_VERIFY_EMAIL` (ВЫКЛ), `PERSON_SOCIAL` (ВЫКЛ) —
последние два выключены, чтобы 200-прогон был быстрым и не долбил чужие серверы.
⚠️ Эти env читает ТОЛЬКО оркестратор; standalone `py person_enrich.py` по умолчанию делает и SMTP-пробу,
и соцпоиск — гасить `--no-verify`/`--no-social`.

**Капы источников:** `RUSPROFILE_MAX_PAGES` (20), `RUSPROFILE_TIMEOUT_MS`, `RUSPROFILE_BROWSER`
(`playwright` | `uc`), `OFDATA_MAX_CANDIDATES` (3000), `OFDATA_MAX_PAGES_PER_CODE` (50),
`OFDATA_CONTACTS_CAP` (0 = все), `OFDATA_REVENUE_SOURCE` (`auto|ofdata|girbo`), `CHECKO_CONTACTS_CAP` (100).

**Пресейл-брендинг (.docx):** `PRESALE_VENDOR` (строка «Подготовлено для», по умолчанию пусто),
`PRESALE_PLATFORM_DESC`.

**Служебные папки:** `D:\orq_cache` (кэш находок), `D:\orq_outbox` (недолитые файлы),
`D:\orq_tmp` (temp + `run_*.log`).

## Подводные камни

- **⚠️ Яндекс Диск молча врёт об успехе заливки (найдено 2026-07-13).** Два режима: (1) клиент вернул
  «✓ Загружено», а файла нет вовсе (прямой GET → 404); (2) при `overwrite=True` рапортует успех, но
  остаётся СТАРЫЙ файл (прежние размер и md5). Сбой перемежающийся, причина не установлена. На бэкфилле
  это стоило 7 «призраков» — компаний, помеченных готовыми без файлов. **Рапорту `DO._upload` верить
  НЕЛЬЗЯ:** вся заливка идёт через `orchestrator._upload_verified` — папка перечитывается, сверяется
  размер, при расхождении старый файл сносится в Корзину (перезапись не работает) и файл льётся заново
  (3×). Не «упрощать» обратно к голому `DO._upload`: иначе прогон снова отчитается об успехе при пустой
  папке, а резюм такую компанию не догонит — она уже числится сделанной.
- **Яндекс Диск 423 (DiskResourceLockedError):** файлы, синхронизированные десктоп-клиентом, лочатся —
  перезапись даёт 423. `_mkdir`/`_upload` ретраят, но устойчивый лок не снимется. Обходы: заливка
  в соседнюю папку либо пауза синка. Удаление/Корзина — тулзы MCP `yadisk` или `yadisk_client` напрямую.
- **Две копии кода:** живая `D:\lead_gen\lead_orchestrator` — **авторитетная, её запускает ярлык**.
  Копия-скилл `C:\Users\abalb\.claude\skills\lead-finder\scripts` — **старого поколения**
  (`DOSSIER_SYSTEM`/`save_dossier_docx`, ОДИН документ — не текущая архитектура из 2 .docx), а не
  «отличается только CRLF/LF». Синхронизировать её только осознанно. Удалять без бэкапа нельзя:
  там лежит Chrome-профиль ручного логина. Cookie рабочего проекта — в `env/rusprofile_cookies.json`;
  `~\.claude\skills\lead-finder\.rp_cookies.json` читается только как legacy fallback.
- **Порог выручки строгий:** компании без подтверждённой выручки или ниже порога отсекаются.
  Порядок ответа API сортировкой не считается: сначала читаются доступные страницы, затем применяется
  явная сортировка по убыванию. Регион, в отличие от выручки, фильтруется ТОЛЬКО клиентски.
- **Асимметрия регион-фильтра.** ВКЛЮЧАЮЩИЙ `--region "Москва"` захватывает и Московскую область
  (в `region_included()` защиты нет). ИСКЛЮЧАЮЩИЙ «НЕ Москва» убирает город фед. значения, но
  «Московская область» остаётся (`region_excluded()`).
- **Сбор RusProfile — headed Chromium «за экраном» (offscreen)** по умолчанию: headless может не пройти
  антибот Cloudflare. `--show-browser` — видимое окно; `--headless` оставлен скрытым флагом для
  совместимости. Offscreen-дефолт — уровень `orchestrator.py`.
- **ЕИС/zakupki.gov.ru под антиботом:** карточки извещений напрямую не парсятся — даются ссылки на
  выдачу по ИНН; контактные лица закупок берутся из поисковой выдачи; полный парсинг — за
  `FIRECRAWL_API_KEY`/browser-use.
- **Изоляция SDK сохраняется.** Все Kimi Agent SDK-стадии выполняются подпроцессом из
  `lead_orchestrator_kimi/.venv_kimi`; ставить `kimi-agent-sdk` в основное окружение нельзя из-за
  конфликта `pydantic-core` с сохранёнными legacy-зависимостями. Это не означает вызов Claude —
  штатный маршрут только запускает Kimi-процессы.
- **Консоль Windows = cp1251** и роняет вывод на кириллице/₽ → скрипты принудительно ставят UTF-8
  stdout; для дампа docx-текста выгружай в файл, а не в консоль.
- **RAM:** каждый боевой ресёрч запускает Kimi-агентов и веб-инструменты отдельными процессами;
  оркестратор авто-снижает `--workers` до 1 при нехватке памяти.
- **Место на C::** системный диск тесный; temp вынесен на `D:\orq_tmp` и чистится по компаниям.
- **Факты о людях, контакты и реквизиты в one-pager ЗАХАРДКОЖЕНЫ** (`SPEAKER_FACTS`, `FOOTER`,
  `VENDOR_PHONE`/`VENDOR_CONTACT_PERSON` (Шабанов Али Магомедович)/`VENDOR_EMAIL`/`VENDOR_SITE`
  в `onepager_kimi.py`) — модель их выдумывала. Контакты CTA — ОТПРАВИТЕЛЯ, не лида.
- **Оговорка про эксперта.** «Булат Замалиев» публично подтверждается как «Уполномоченный по технологиям
  ИИ при Минцифры РТ»; связь с ЦИТ РТ как «руководителя направления» публично НЕ подтверждена — в
  one-pager стоит подтверждённая формулировка, вторую как факт не утверждать.
- **Устаревшие докстринги в живом коде:** шапки `orchestrator_agent.py` и `run_orchestrator.cmd`,
  «yacli-фолбэк» в `yadisk_client.py`, «2 файла» в `disk_organize.py`, yacli в `pipeline.py`,
  `lead_orchestrator/README.md` («досье и стратегия», $1–2/компания). При расхождениях авторитет —
  этот CLAUDE.md.

## Конвенции

- Python 3.12, запуск `py` (но лаунчеры — через `ORQ_MAIN_PY`, см. «Запуск»). Комментарии и строки —
  на русском, под стиль соседнего кода.
- Перед коммитом: `py -m py_compile` изменённых файлов + релевантные офлайн-тесты из «Проверки правки».
  Правил `orchestrator.py`/CRA — обязательно `py orchestrator.py <leads.json> --dry-run --no-upload`.
  Правил печать в `orchestrator.py` — обязательно `py web/api/test_events.py`.
  Правил `deep_research_engine.py` — обязательно `test_deep_research.py`.
- **Не плодить параллельные модули:** формат .docx и промпты живут в CRA, ресёрч — в
  `deep_research_engine.py`, обвязка 3-й стадии — в `orchestrator._onepager_one`, а сама стадия
  (промпт, канон, рендер) — в `../lead_orchestrator_kimi/`.
- Git: origin `github.com/Aidesade/lead_orchestrator`, рабочая ветка `kimi` (основная `master`),
  коммиты на русском. Под git обе папки пайплайна, `web/` и docker/деплой-доки.
