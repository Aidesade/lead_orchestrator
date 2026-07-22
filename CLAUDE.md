# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Оркестратор лидогенерации для B2B-агентства, продающего **корпоративную on-premises
LLM-платформу** (RAG-база знаний, автономные ИИ-агенты, ИИ-Коуч «Наставник»).
Ядро пайплайна — в подпапке **`lead_orchestrator/`**; рядом — изолированная стадия one-pager
**`lead_orchestrator_kimi/`** (свой venv), веб-интерфейс **`web/`** и контейнеризация под прод
(`Dockerfile`/`docker-compose.yml`). Корень репозитория — **`D:\lead_gen`**. Основная платформа —
Windows, Python 3.12, запуск через `py`; прод-цель — Docker в Linux. Комментарии в коде — на русском.

## Приоритетный runtime: Kimi K2.7 (2026-07-21)

Штатный режим всего агента — **Kimi-only**: NL-контроллер, deep-research extract,
dependency-aware enrichment, писатель двух `.docx` и one-pager работают на одной модели
`KIMI_MODEL_NAME=kimi-k2.7-code` через `KIMI_BASE_URL`. В Kimi-only режиме ID модели
зафиксирован: другое значение вызывает fail-fast до первого запроса.
Ключ берётся в порядке `KIMI_API_KEY` → `GPLLM_API_KEY`; на этой машине используется второй.
`ORQ_KIMI_ONLY=1` и `DR_LLM_PROVIDER=kimi` выставляются лаунчерами, Docker и общей конфигурацией
`lead_orchestrator/kimi_config.py`. Веб принимает только `model=kimi`.

Старая ветка Claude сохранена в коде исключительно для аварийного отката и недоступна из
штатных CLI/web/desktop-путей. Включить её можно только осознанно через `ORQ_KIMI_ONLY=0`;
это не поддерживаемый рабочий дефолт.

## Что делает

Полная цепочка в две фазы (детерминированный Python-оркестратор, НЕ LLM-оркестратор):

- **ФАЗА 1 — сбор.** Локальный/desktop и кодовый дефолт `LEAD_SOURCE=rusprofile`:
  Playwright загружает cookie из игнорируемого `env/rusprofile_cookies.json`, внутри страницы
  вызывает advanced-search по ОКВЭД с серверным `finance_revenue_from`, не ниже
  **1 млрд ₽**, забирает доступные страницы и явно сортирует их по выручке по убыванию.
  Только после сортировки открывается ровно по одной карточке для выбранных N компаний.
  `LEAD_SOURCE=ofdata` и `LEAD_SOURCE=checko` сохранены как явные API-пути отката.
  Дальше → отбор → `D:\лиды\leads_<отрасли>.json`
  (флаг `--out`; .xlsx из боевой ФАЗЫ 1 убран 2026-07-06) → дерево на Яндекс Диске `<--base>/<отрасль>/<категория полноты контактов>/<компания>/`
  (дефолт корня `disk:/Лиды`; категория — по наличию email/телефона/сайта/ЛПР, `category_for`)
  с 3 файлами-заглушками, которые ФАЗА 2 перезапишет.
- **ФАЗА 2 — ресёрч + материалы.** По каждой компании: настоящий deep-research → **ТРИ файла**
  (2 нейтральных .docx + 1 брендированный one-pager .pdf) → в **хранилище сайта**
  `ORQ_DATA_ROOT/deliverables/<ключ>/` (ключ = ИНН; `DO.deliverables_subdir`), откуда веб отдаёт их
  на скачивание. Это дефолт (`ORQ_STORE=local`, 2026-07-15); прежняя заливка на Яндекс Диск —
  по `ORQ_STORE=disk` (тогда работают резюм по Диску, outbox, раскладка-заглушки ФАЗЫ 1).

Выходные файлы на компанию:
1. `<Компания>_карта_бизнес-процессов.docx` — нейтральный аналитический отчёт: профиль → as-is
   процессы → боли → ИИ-решение → to-be, сводная таблица, дорожная карта, специфика 44/223-ФЗ, оговорки.
2. `<Компания>_карта_ролей_и_контактов_пресейл.docx` — нейтральный: оргструктура, ЛПР, профильные
   отделы под внедрение ИИ, филиалы, официальные контакты, план захода, оговорки.
3. `<Компания>_презентация_Telepatt.pdf` — клиентский **one-pager** (редакционно-газетный стиль,
   фото эксперта). Делает **модель Kimi** (НЕ Claude): выдаёт самодостаточный HTML → Playwright
   рендерит в PDF. Блоки листа 1–3, 6, 8 (шапка/герой/фичи/спикер/футер) — КАНОН, одинаковы у всех;
   под заказчика пишется только «Одна проблема — одно решение» + подписи метрик.
   **ВКЛючён по умолчанию**; отключить — `--no-presentation` / `GEN_PRESENTATION=0`.
   ⚠️ Стадия живёт в соседней папке `lead_orchestrator_kimi/` со СВОИМ venv и зовётся подпроцессом:
   `kimi-agent-sdk` и `claude-agent-sdk` несовместимы по зависимостям (pydantic-core) и в одном
   интерпретаторе не живут. Прежняя брендированная `.pptx` (скилл Anthropic `pptx` + LibreOffice)
   ЗАМЕНЕНА этой стадией 2026-07-13 — её код, промпт `PRESENTATION_SYSTEM` и предусловия удалены.

## Запуск

Ярлык на рабочем столе **`new_orchestrator`** → `lead_orchestrator/run_orchestrator.cmd` →
`run_kimi_orchestrator.cmd` → Kimi-контроллер `orchestrator_agent.py` → **`orchestrator.py`**.
`run_orchestrator.cmd` теперь только канонический делегат в Kimi-лаунчер. Лаунчер выставляет
`KIMI_API_KEY`←`GPLLM_API_KEY`, `KIMI_BASE_URL`, `KIMI_MODEL_NAME`, `DR_LLM_PROVIDER=kimi` и
`ORQ_KIMI_ONLY=1`; отсутствие ключа — жёсткая ошибка до запуска, а не тихий пропуск стадии.
Оба `.cmd` — строго ASCII+CRLF: cmd.exe портит UTF-8/LF батники.

```
# полная цепочка (сбор + ресёрч + 3 файла), ВСЯ отрасль (по умолчанию 200 компаний):
py orchestrator.py --industries mining            # = py orchestrator.py mining (позиционно — только ключи карты INDUSTRY)
py orchestrator.py mining --count 10              # явно 10
# только ресёрч+материалы по готовому JSON лидов (ФАЗА 1 пишет их в D:\лиды\):
py orchestrator.py "D:\лиды\leads_mining.json"
# без 3-й стадии (one-pager .pdf), только 2 .docx:
py orchestrator.py mining --no-presentation
# Kimi K2.7 выбирается автоматически; явный флаг эквивалентен дефолту:
py orchestrator.py "D:\лиды\leads_mining.json" --model kimi
# локально без заливки на Диск (файлы остаются в temp, путь печатается):
py orchestrator.py "D:\лиды\leads_mining.json" --no-upload
# дёшево проверить связку без LLM и без следов (заглушки обоих .docx; заглушка .pdf — если стоят предусловия стадии):
py orchestrator.py "D:\лиды\leads_mining.json" --dry-run --no-upload
# разовый Kimi-ресёрч одной компании из one-lead JSON (оба .docx + опциональный PDF):
py orchestrator.py sample_ryazanavtodor.json --no-upload
# только официальная база контактов — детерминированно, без LLM:
py company_research_agent.py --contacts "АО Рязаньавтодор 6234065445"   # только официальная база, без ресёрча и .docx
# разовый прямой контакт ЛПР (ФИО+ИНН -> рабочие email/телефоны, только легитимные источники).
# ⚠️ standalone-дефолты ПРОТИВОПОЛОЖНЫ оркестраторным: SMTP-проба и соцпоиск ВКЛ (гасить --no-verify / --no-social):
py person_enrich.py "Руденко Сергей Александрович" 6234065445
# движок deep_research отдельно, БЕСПЛАТНО (regex-only, без LLM):
DR_USE_LLM=0 py deep_research_engine.py --company "АО Рязаньавтодор" --inn 6234065445 --site https://avtodor-rzn.ru
py deep_research_engine.py --crawl avtodor-rzn.ru # отладка: только краул сайта (SiteCrawler)
# разовый логин RusProfile (cookie в игнорируемом env/rusprofile_cookies.json):
py rusprofile_session.py --login
# штатный локальный сбор RusProfile/Playwright:
py source_rusprofile.py --industries processing --min-revenue 1e9 --per-industry 10 --out leads.json
# сбор лидов через Checko API (без браузера/антибота; годится для Docker/Linux) -> leads.json:
py source_checko.py --industries processing --min-revenue 1e9 --region Татарстан --out leads.json
# штатный сбор через OfData API; на бесплатном тарифе выручка автоматически берётся из ГИР БО:
py source_ofdata.py --industries processing --region Татарстан --out leads.json
# Docker (из корня D:\lead_gen; секреты в .env): собрать и прогнать 10 компаний:
docker compose build && docker compose run --rm lead-orchestrator mining --count 10
# веб-интерфейс (из корня): API + фронт (подробности web/README.md):
py -m uvicorn web.api.main:app --port 8000      # затем web/ui: npm run dev / npm run build
```

Через ярлык/обёртку достаточно назвать **только отрасль** (`mining`, «добыча угля», «нефтегаз»…):
`orchestrator_agent.py` соберёт **200** компаний (если не задано иное) и выдаст по каждой 3 файла;
перед большим боевым прогоном показывает точный объём и просит короткое подтверждение.
Полезные флаги `orchestrator.py`: `--count N` (ВСЕГО по всем отраслям, дефолт 200),
`--per-industry N` (НА КАЖДУЮ отрасль — перекрывает `--count`: итог = N × число отраслей;
в NL-обёртке это `count_per_industry` — «по 10 на отрасль»), `--min-revenue 1e9`, `--region "..."`
(поддерживает ОТРИЦАНИЕ: `"НЕ Москва"` = вся РФ кроме Москвы; смешивание через запятую —
`"Урал, НЕ Москва"`; также `!X`/`-X`/`кроме X`), `--workers N` (дефолт 2, авто→1 при <3 ГБ RAM —
проверка только в боевом запуске, в dry-run её нет), `--model kimi` (единственный штатный вариант),
`--no-presentation`,
`--no-person-enrich`, `--show-browser`, `--out <json>` (дефолт `D:\лиды\leads_<отрасли>.json`;
старый .xlsx-путь тоже примется — расширение заменится на .json),
`--base` (корень Диска, дефолт `disk:/Лиды`), `--account`, `--redo` (ВЫКЛючить резюм — переделать
даже компании, у которых на Диске уже лежат реальные документы). NL-обёртка `orchestrator_agent.py`
прокидывает лишь подмножество флагов (нет `--no-person-enrich`/`--out`/`--base`/`--headless`).

## Карта кода (`lead_orchestrator/`)

| Файл | Роль |
|---|---|
| `orchestrator.py` | **главный вход** ФАЗА 1+2 (asyncio). `_collect` = ФАЗА 1; `_research_one` = ресёрч+2 .docx; `_onepager_one` = 3-я стадия (one-pager .pdf: ПОДПРОЦЕСС venv-питона `../lead_orchestrator_kimi`); `_presentation_prereqs` = venv Kimi + фото + ключ; `_kimi_env` = KIMI_API_KEY (фолбэк `GPLLM_API_KEY`) / KIMI_BASE_URL / KIMI_MODEL_NAME; `_no_sleep` блокирует сон Windows на время прогона |
| `orchestrator_agent.py` | Kimi K2.7 NL-контроллер (OpenAI-compatible API → строгий JSON-план → валидация → запуск `orchestrator.py --model kimi` подпроцессом); дефолт count=200, one-pager по умолчанию |
| `kimi_config.py` | единая конфигурация Kimi: ключ, endpoint, модель, child env и Kimi-only guard |
| `../lead_orchestrator_kimi/` | **Изолированные Kimi-агенты Фазы 2 + 3-я стадия** (свой venv): dependency-aware enrichment из пяти ролей (`official → contour+secondary → roles → contacts`), агентный писатель и one-pager HTML→PDF. `research_enrichment_agent.py` валидирует schema v3, exact evidence URL/redirect, покрытие ролей/кандидатов и ведёт версионированный checkpoint с evidence trace; `requirements.txt` фиксирует `kimi-agent-sdk==0.0.5` + `kimi-cli==1.12.0`. Свой `CLAUDE.md` — читать перед правкой |
| `web/` (корень репо) | **веб-интерфейс** (FastAPI + React/Vite). `web/api` спавнит `orchestrator.py` подпроцессом и парсит его stdout в SSE-события (структурированных событий у оркестратора нет); один активный прогон (второй → 409). Свой `README.md` |
| `Dockerfile` / `docker-compose.yml` / `DEPLOY_TIMEWEB.md` (корень) | **контейнеризация под Timeweb Cloud (Москва)**. Два venv в образе (основной + `/opt/kimi-venv`), БЕЗ Chrome/Xvfb → `source_rusprofile` в контейнере неработоспособен (штатный источник — OfData). Сборка = build-gate (`py_compile`, офлайн `test_deep_research.py` + `test_source_ofdata.py`, `apply_patches.py --check`). `AUDIT_KIMI.md` — аудит стадии Kimi |
| `assets/` | `bulat_zamaliev.png` — фото эксперта (вшивается в one-pager). `citrt_logo.png` остался от .pptx-стадии; в one-pager логотип — текстовый словомарк, PNG не нужен |
| `company_research_agent.py` (CRA) | **общие схемы, промпты и DOCX-рендереры**. `PROCESS_MAP_SYSTEM` / `ROLES_CONTACTS_SYSTEM` берёт Kimi-писатель; импорт модуля не загружает Claude SDK. Старый standalone LLM-agent доступен только при `ORQ_KIMI_ONLY=0`; штатно для одной компании используется one-lead JSON через `orchestrator.py`. В карту ролей детерминированно добавляются таблицы центров решений, корпоративного графа, кандидатов, каналов и реестр доказательств |
| `writer_kimi.py` | **штатный агентный писатель двух .docx на Kimi K2.7** и parent-мост пяти enrichment-ролей. Сначала отдельный Kimi-процесс выполняет `official → (contour + secondary) → role_candidates → candidate_contacts`; затем на документ запускается `writer_kimi_agent.py` с динамическими scout/critic/verifier. `apply_research_enrichment` машинно переносит критические таблицы в DOCX. URL-инструменты subprocess-мостом зовут `kimi_research_cli.py`; SDK не смешиваются. Legacy HTTP — только `KIMI_WRITER_AGENT=0` |
| `source_ofdata.py` | API-путь отката Фазы 1 (`LEAD_SOURCE=ofdata`). Переиспользует проверенные `usable_okved`/`resolve_region` и `extract_company_contacts` Checko: `/search` → выручка (`/finances`, а при бесплатном тарифе balance=0 автоматический fallback на ГИР БО ФНС) → обязательный порог ≥1 млрд → `/company` только для прошедших. Ключ только `OFDATA_API_KEY`, POST form-urlencoded (ключ не в URL/логах), ретраи 429/5xx, счётчики запросов/баланса. `OFDATA_REVENUE_SOURCE=auto|ofdata|girbo`; капы: `OFDATA_MAX_CANDIDATES` (3000), `OFDATA_MAX_PAGES_PER_CODE` (50), `OFDATA_CONTACTS_CAP` (0 = все) |
| `test_source_ofdata.py` | полностью офлайн проверяет формы `/finances`, включительный порог ровно 1 млрд, отсев ниже порога/без строки 2110, `/company`, раскрытие короткого ОКВЭД и отсутствие ключа в URL/ошибке |
| `source_checko.py` | API-путь отката (`LEAD_SOURCE=checko`). Drop-in для `source_rusprofile.harvest()`: `/search`, выручка из ГИР БО, контакты `/company`; ключи `CHECKO_TOKEN` + запасные |
| `okved2_codes.py` | справочник ОКВЭД-2 (ОК 029-2014): 623 подкласса NN.NN, снимок 2026-07-15 с github.com/carono/okvad2. Нужен `source_checko` и через его helpers — `source_ofdata`; RusProfile им не пользуется |
| `deep_research_engine.py` | **настоящий deep-research**: официальная база + site/eis/courts_media/hh, Crawl4AI→HTTP, targeted refill и `completeness_critic`; поиск brave→ddg→bing. HTTP-фетч защищён от `file://`, credentials, localhost/private/link-local IP и небезопасных redirect, ответ ограничен `DR_FETCH_MAX_BYTES`. Мульти-домен подтверждается по ИНН/полному названию; каталоги режутся `AGGREGATORS` |
| `source_rusprofile.py` | **штатный локальный источник Фазы 1**: RusProfile advanced-search по ОКВЭД, серверный порог ≥1 млрд, полный клиентский revenue-sort; карта `INDUSTRY` (21 отрасль); регион-фильтр с отрицанием |
| `rusprofile_playwright.py` | единая Playwright-context для поиска и ровно одного открытия каждой отобранной карточки; безопасная загрузка cookie без печати значений |
| `rusprofile_session.py` | создание/обновление cookie платного аккаунта (`--login`); UC оставлен как `RUSPROFILE_BROWSER=uc` rollback |
| `test_rusprofile_playwright.py` | офлайн-регрессия cookie-нормализации, минимального порога 1 млрд и сортировки по убыванию |
| `pipeline.py` | отбор `_select` + сохранение `_save`; его СОБСТВЕННАЯ полная цепочка `run()` (RusProfile→ГИР БО→Checko→site_verify) работает только при прямом `py pipeline.py` — оркестратор её не вызывает |
| `build_excel.py` | выгрузка .xlsx — из боевой ФАЗЫ 1 УБРАНА (2026-07-06, только JSON); используется лишь standalone-цепочкой `pipeline.run()` |
| `disk_organize.py` | пути/папки на Диске поверх `connectors/yadisk_client` (`_mkdir`/`_upload` с ретраями на 423 и транзиентные сетевые сбои — `_is_locked`/`_is_transient`), заглушки .docx/.pdf (`generate_presentation`, `_make_pdf` — голый PDF без зависимостей; текст транслитерирован, т.к. базовые шрифты PDF кириллицу не несут) |
| `connectors/` | **пакет коннекторов.** `yadisk_client.py` — ядро Яндекс Диска на официальном REST API (stdlib, без зависимостей; строковые операции `_info/_list/_mkdir/_upload/...` + исключение-обёртки `ensure_dir`/`upload_file` для пайплайна); `yadisk_mcp.py` — MCP-обёртка над ним |
| `revenue_enrich.py` | выручка из ГИР БО ФНС (бесплатно, стр. 2110) — официальная база ФАЗЫ 2 и `pipeline.run()`; в `_collect` НЕ участвует |
| `dadata_enrich.py` / `checko_enrich.py` | карточка ЕГРЮЛ (Dadata) / контакты (Checko) |
| `person_enrich.py` | ЛПР: ФИО+ИНН → прямой РАБОЧИЙ контакт (Dadata/Checko → домен с валидацией → email по шаблону+MX, телефоны; соцпрофили — только подтверждённые ИНН-контекстом). Вызывается из `_research_one` ДО писателя, блок дописывается к находкам. «Пробив»/утечки конструктивно исключены (`DENY_SOURCES`) |
| `email_finder.py`, `site_verify.py`, `harvest_inn_site.py`, `inn_util.py`, `webutil.py`, `browser_util.py` | утилиты |
| `test_deep_research.py` / `test_research_enrichment.py` | офлайн-тесты движка и реального scheduler/контрактов/cache/DOCX-adapter пяти ролей (без сети/LLM) |
| `sample_ryazanavtodor.json` | готовый leads.json на одну компанию — для быстрых прогонов (`--dry-run --no-upload`) |

## Архитектура ФАЗЫ 2 (важно — не сломать)

`_research_one` (orchestrator.py):
1. **Пред-запуск движка ДО сессии писателя, ДВА прохода** (`RESEARCH_PASSES`, 2026-07-16) —
   `deep_research_engine.deep_research()` на верхнем уровне гоняется ДВАЖДЫ, со своими `aspects`:
   проход `process` (профиль, процессы as-is, ИТ-ландшафт, госконтракты, точки внедрения ИИ) и
   проход `roles` (оргструктура, ЛПР, закупки, филиалы, контакты, соцпрофили, учредитель/ведомство).
   Раньше был ОДИН общий проход, и добор под роли разменивался на добор под процессы —
   не «упрощать» обратно к одному вызову. Каждый документ получает находки СВОЕГО прохода.
   ⚠️ НЕ вызывать движок как live-тул ВНУТРИ сессии писателя: вложенные SDK-сессии сбивают
   писателя, и он не сохраняет документы. Сбой прохода компанию не валит (вернётся хотя бы
   официальная база). Standalone-CRA — осознанное исключение: там `deep_research`
   остаётся live-тулом писателя (разовый интерактивный запуск, не 200-прогон).
2. **Обогащение ЛПР (`person_enrich`)** — тоже детерминированно ДО писателя (НЕ вложенная
   SDK-сессия): по `contact_person`+ИНН прямые рабочие контакты, блок дописывается к находкам
   прохода `roles` (в `process` не идёт — там он не нужен); шаг тихо пропускается,
   если у лида нет `contact_person` или `_inn`.
   ВКЛ по умолчанию (`--no-person-enrich` / `PERSON_ENRICH=0`); SMTP-проверка email
   (`PERSON_VERIFY_EMAIL=1`) и соцпоиск (`PERSON_SOCIAL=1`) по умолчанию ВЫКЛ — чтобы 200-прогон
   был быстрым и не долбил чужие серверы.
3. **Пять специализированных enrichment-ролей (ветка Kimi).** После обоих DRE-проходов и до
   писателя запускается граф `official_sources → (corporate_contour + secondary_sources) →
   role_candidates → candidate_contacts`. Контактник не видит общий seed персоналий — только
   структурированный список кандидатов. Ответы проходят enum/URL/date/confidence/source-policy
   validation; сниппет без Fetch/Crawl не доказательство. Checkpoint атомарный и проверяет
   schema/prompt/input/dependency hash + TTL, поэтому старые параллельные кэши не принимаются.
   ⚠️ **Мягкая деградация (2026-07-22): сбой роли НЕ роняет компанию.** Упавшая роль и её
   downstream пишутся в `roles_failed`, наружу идёт частичное досье (`complete=False`), а писатель
   добирает недостающее из находок движка (`_research_one_kimi` обёрнут в try/except — writer
   вызывается ВСЕГДА; блоки досье терпят отсутствующие роли). Раньше любая ошибка роли →
   `RuntimeError` → exit 1 → писатель вообще не запускался, а лог винил писателя («писатель не
   сохранил .docx»). НЕ возвращать `raise` в `run()`. Trusted-домены official-роли засеяны сайтом
   лида И доменом его почты (Фаза 1), чтобы валидатор не отвергал уже доказанный сбором домен;
   родитель (`writer_kimi.run_research_subagents`) поднимает НАСТОЯЩУЮ причину из stdout, а не
   хвост stderr с шумом `authlib.jose` DeprecationWarning.
4. **Писатель.** Развилка по `--model` (`writer_kimi.is_kimi`): **Claude** (`opus`/`sonnet`,
   `PRESALE_SYSTEM` — один общий промпт, ветка сохранена как есть) — агент с инструментами,
   получает находки и обязан вызвать ОБА `save_*_docx` (есть нудж-ретрай, если не сохранил);
   либо **Kimi** (`--model kimi`, `writer_kimi.write_two_docx`) — агент в отдельном venv:
   на каждый документ свой главный писатель со свободным read-only веб-поиском + динамический
   веер `scout`/`verifier` + обязательный `critic`, свой системный промпт и свои находки-seed со
   своего прохода → финальный JSON → те же рендереры (см. карту кода).
   CLI-дефолт — `opus`; веб-UI по умолчанию `kimi`. На Kimi долларовая вилка `[оценка]` не печатается (цену за
   вызов шлюз наружу не отдаёт → итог `$0`, это не баг).
5. **Гард против болванок:** заливаются только реальные .docx (>5000 байт, проверка в `process()`);
   иначе компания = неуспех.

**Третья стадия — `_onepager_one` (опц., по умолчанию ВКЛ).** НЕ агентная SDK-сессия и НЕ
python-рендерер: оркестратор запускает **ПОДПРОЦЕСС venv-питона соседней папки**
`../lead_orchestrator_kimi/.venv_kimi/Scripts/python.exe onepager_kimi.py <компания> --industry …
--pain … --out <p.pdf>`. Так решено сведение двух SDK: `kimi-agent-sdk` тянет `pydantic-core 2.41.5`,
а `claude-agent-sdk`/`anthropic` требуют `2.46.4` — в одном интерпретаторе они не живут, поэтому
Kimi-стадия изолирована в своём venv (зависимости пиннятся `lead_orchestrator_kimi/requirements.txt`,
патчи venv накатывает ОДИН идемпотентный `patches/apply_patches.py` — детали в `CLAUDE.md` той папки).
Боль берётся из находок ресёрча (`_main_pain`) и идёт в единственный вариативный блок листа.
Гард: заливается только реальный .pdf (>5000 байт); сбой стадии НЕ валит компанию (2 .docx уже готовы),
ретрай 3×, таймаут `ORQ_ONEPAGER_TIMEOUT`. Стоимость Kimi считает провайдер — CLI её наружу не отдаёт,
поэтому в оценку прогона (`[оценка] ~$N`) one-pager НЕ входит, там только Claude-сессии за 2 .docx.

**Временные файлы.** Каждая компания работает в `D:\orq_tmp\orq_XXXX\<idx>\` (изоляция от
параллельных воркеров); после заливки папка компании удаляется СРАЗУ (не копим все 200 до конца).
temp на D:, т.к. C: переполнен. При `--no-upload` файлы оставляются локально.

**Устойчивость прогона (добавлено 2026-07-05; прогон рассчитан на перезапуск ТОЙ ЖЕ командой):**
- **Резюм.** Перед компанией проверяются размеры её файлов на Диске (`_remote_state` →
  `yadisk_client.list_file_sizes`): оба .docx >5 КБ → пропуск; .docx есть, а .pdf нет/заготовка
  (реальный one-pager — `REAL_PDF_MIN`=60 КБ; заготовка ≈8.6 КБ) → доделывается ТОЛЬКО one-pager.
  `--redo` отключает. ⚠️ Резюм ищет файлы ПО ИМЕНИ: переименование третьего деливерабла
  (`_презентация_Telepatt.pptx` → `.pdf`, 2026-07-13) означает, что у компаний, сделанных ДО этого,
  презентация считается отсутствующей и генерится заново; старые .pptx остаются лежать рядом.
  Раскладка ФАЗЫ 1 (`_put_stub`) заготовки НЕ перезаписывает существующие файлы — повторный
  полный прогон не затирает реальные документы и не ломает резюм.
- **Outbox.** Сбой заливки НЕ удаляет оплаченные файлы: они откладываются в
  `D:\orq_outbox\<компания>_<ts>\` (+meta.json) и доливаются автоматически в начале и конце
  следующего прогона (`_flush_outbox`).
- **Кэш находок движка** `D:\orq_cache\findings_<ИНН>__<проход>.md` — ОТДЕЛЬНЫЙ файл на каждый
  проход `RESEARCH_PASSES` (TTL `ORQ_FINDINGS_TTL_H`=72 ч): ретрай писателя и повторный прогон
  не гоняют deep-research заново. ⚠️ Откат на легаси-кэш от старого ОДНОГО общего прохода
  (`findings_<ИНН>.md` без суффикса) по умолчанию ВЫКЛЮЧЕН (`legacy_ok=False`): иначе оба прохода
  прочитали бы один файл и двухпроходность молча выродилась бы в старую схему. Легаси читается
  только как справка для боли на листе one-pager'а.
- **Таймауты сессий** `ORQ_RESEARCH_TIMEOUT`=0 для Kimi (без общего дедлайна), 1800с для Claude /
  `ORQ_ONEPAGER_TIMEOUT`=900с на попытку:
  зависший claude CLI / подпроцесс Kimi обрывается в обычный ретрай (3×), а не вешает воркер навечно
  (при таймауте подпроцесс убивается — иначе висел бы осиротевший python+chromium).
- **RAM-бэкпрешер** — перед тяжёлой стадией ожидание свободной RAM ≥ `ORQ_MIN_RAM_GB`=2.5 ГБ
  (до 5 мин); one-pager ограничен `ORQ_ONEPAGER_CONCURRENCY`=2 (каждая стадия = свой python+chromium;
  прежняя .pptx требовала жёсткой сериализации из-за LibreOffice+node — теперь их нет).
- **Изоляция сбоев:** `gather(..., return_exceptions=True)` + ловля `BaseExceptionGroup` (anyio
  так оборачивает крах CLI) — сбой одной компании не валит прогон; Ctrl+C/SystemExit пробрасываются.
- **Лог всегда:** stdout+stderr дублируются в `D:\orq_tmp\run_<ts>.log` (`_Tee`); в конце — список
  компаний, оставшихся с заготовками. Exit-код 3 — если не удалась НИ одна компания.
- **ФАЗА 1:** harvest и сессия контактов ретраятся (2×, свежий Chrome); Cloudflare ждётся по факту
  CSRF-cookie (до 30 с), страницы поиска — с одним повтором (`_post_retry`); контакты чекпойнтятся
  в JSON каждые 20 карточек; протухшая платная сессия = SystemExit с подсказкой `--login`
  (а не молчаливый сбор без контактов).

## Окружение / зависимости

- Аутентификация штатного runtime: `KIMI_API_KEY`, с фолбэком на `GPLLM_API_KEY`.
  Anthropic-аутентификация относится только к явно включённому legacy-rollback.
- Зависимости: **`py -m pip install -r requirements.txt`** (pinned-lock рабочего окружения,
  снимок 2026-06-30) + `python -m playwright install chromium` (краул; ~300 МБ). Ключевые пакеты:
  `claude-agent-sdk`, `python-docx`, `crawl4ai`, `undetected-chromedriver`/`selenium` (RusProfile),
  `openpyxl` (Excel).
- Яндекс Диск: и пайплайн (`disk_organize.py`), и интерактивный MCP-сервер **`yadisk`**
  (`connectors/yadisk_mcp.py`, регистрируется в `D:\lead_gen\.mcp.json`) работают через ОДНО ядро —
  `connectors/yadisk_client.py` (официальный REST API, stdlib). Единственное требование —
  env `YANDEX_DISK_TOKEN`: OAuth-токен со scope `cloud_api:disk.write` (получить на
  oauth.yandex.ru, задать `setx YANDEX_DISK_TOKEN "..."`). CLI `yacli` для Диска больше
  НЕ используется. Офлайн-проверка: `py connectors\yadisk_mcp.py --selftest`.
  Аккаунты (переключено 2026-07-06): `YANDEX_DISK_TOKEN` = РАБОЧИЙ Диск **AI-CIT.RT@yandex.com** —
  весь пайплайн пишет туда. `YANDEX_DISK_TOKEN_TEST` = прежний тестовый Диск **TESTosteronthegreat**;
  на нём остался старый `disk:/Лиды` (51 компания, ~10 МБ) — кодом НЕ используется, резюм его не видит.
  ⚠️ Уже открытые консоли/сессии наследуют старое окружение — новый токен подхватится только после
  их перезапуска (ярлык `new_orchestrator` открывает свежую консоль — там всё сразу правильно).
- Для штатной Фазы 1 обязателен `OFDATA_API_KEY`. Платный доступ к `/finances` используется
  напрямую; бесплатная заглушка автоматически переключает выручку на официальный ГИР БО ФНС.
  Ключ хранится в `env/.env`; вся `/env/` исключена из Git и Docker build context.
  `project_env.load_project_env()` тихо загружает файл в desktop/CLI, не печатая значения.
  Опциональные токены: `DADATA_TOKEN`, `CHECKO_TOKEN` (+ запасные `CHECKO_TOKEN_ALT`/`_2`/`_3`
  только для пути отката).
- Пресейл-брендинг (.docx): `PRESALE_VENDOR` (строка «Подготовлено для», по умолчанию пусто),
  `PRESALE_PLATFORM_DESC`.
- **Стадия one-pager (.pdf):** живёт в `../lead_orchestrator_kimi/` — нужны её venv
  (`.venv_kimi`: `kimi-agent-sdk==0.0.5` + `kimi-cli==1.12.0` + `playwright==1.60.0` по
  `requirements.txt`, **плюс патчи venv** через `patches/apply_patches.py` — см. `CLAUDE.md` там;
  пин НЕ снимать: без него резолвер ставил kimi-cli 1.12, а патч бил вслепую под 1.4x → `TypeError`),
  фото `lead_orchestrator/assets/bulat_zamaliev.png` и **ключ провайдера Kimi**: `KIMI_API_KEY`,
  а если его нет — `GPLLM_API_KEY` (так он задан на этой машине). `KIMI_BASE_URL`
  (дефолт `https://gpllmkeeper.dtc.tatar/v1`) можно перекрыть через env;
  `KIMI_MODEL_NAME` в Kimi-only закреплён как `kimi-k2.7-code`. Другая модель или отсутствие
  ключа останавливают агент до старта.
  `GEN_PRESENTATION=0` / `--no-presentation` отключают. LibreOffice/Poppler/node/скилл `pptx`
  этой стадии больше НЕ нужны (Poppler остаётся полезен для ручной проверки PDF: `pdfinfo`/`pdftoppm`).
- Бренд — платформа **Telepatt** от **АО «ЦИТ РТ»** (госкомпания РТ, citrt.ru). ⚠️ Факты о людях,
  контакты и реквизиты в one-pager ЗАХАРДКОЖЕНЫ (`SPEAKER_FACTS`, `FOOTER`, `VENDOR_PHONE`/
  `VENDOR_CONTACT_PERSON` (Шабанов Али Магомедович — строкой под телефоном)/`VENDOR_EMAIL`/
  `VENDOR_SITE` в `onepager_kimi.py`) — модель их выдумывала. Эксперт «Булат Замалиев» публично подтверждается как «Уполномоченный по технологиям ИИ
  при Минцифры РТ»; связь с ЦИТ РТ как «руководителя направления» публично НЕ подтверждена — в
  one-pager стоит подтверждённая формулировка, не утверждать вторую как факт.
- Тумблеры движка: `DR_LLM_PROVIDER` — провайдер LLM-экстракта; штатный и кодовый дефолт —
  `kimi` (тот же OpenAI-совместимый шлюз, что у писателя; `DR_KIMI_MAX_TOKENS`=8000).
  Значение `claude` имеет смысл только вместе с `ORQ_KIMI_ONLY=0` для legacy-отката. Далее:
  `DR_USE_CRAWL4AI=0` (→ HTTP-фолбэк), `DR_USE_LLM=0` (regex-only, бесплатно),
  `DR_EXTRACT_MODEL=sonnet`, `DR_PAGE_CHARS` (9000), `DR_BREADTH`/`DR_DEPTH` (петля добора, 4/2),
  `DR_MAXPAGES` (кап страниц краула, 25), `DR_MAX_DOMAINS` (подтверждённых сайтов на компанию, 3),
  `DR_LLM_CONCURRENCY` (2), `DR_CRAWL_CONCURRENCY` (4),
  `DR_SEARCH_INTERVAL` (троттл поиска, 1.6 с), `DR_SEARCH_CONCURRENCY` (1),
  `FIRECRAWL_API_KEY` (опц., карточки ЕИС).
- Тумблеры ЛПР-обогащения: `PERSON_ENRICH=0` (отключить целиком), `PERSON_VERIFY_EMAIL=1`
  (SMTP-проверка email), `PERSON_SOCIAL=1` (соцпоиск) — последние два по умолчанию ВЫКЛ.
  ⚠️ Эти env читает ТОЛЬКО оркестратор; standalone `py person_enrich.py` по умолчанию делает
  и SMTP-пробу, и соцпоиск — гасить флагами `--no-verify` / `--no-social`.
- Тумблеры устойчивости (см. «Устойчивость прогона»): `ORQ_RESEARCH_TIMEOUT` (1800),
  `ORQ_ONEPAGER_TIMEOUT` (900), `ORQ_ONEPAGER_CONCURRENCY` (2), `ORQ_MIN_RAM_GB` (2.5),
  `ORQ_FINDINGS_TTL_H` (72). Служебные папки:
  `D:\orq_cache` (кэш находок движка), `D:\orq_outbox` (недолитые на Диск файлы — доливаются
  следующим прогоном), `D:\orq_tmp\run_*.log` (логи прогонов).
- **Переключатели окружения:** `LEAD_SOURCE`
  (`ofdata` | `checko` | `rusprofile`), `DR_LLM_PROVIDER` (`claude` | `kimi`), `ORQ_STORE`
  (`local` | `disk`). Источник и на Windows, и в Docker штатно `ofdata`; desktop-ярлык
  выставляет его явно. Checko и RusProfile доступны только как откат, но
  LLM-провайдер на обеих платформах теперь `kimi`; `ORQ_KIMI_ONLY=1` не даёт случайно уйти в Claude.
- **Портируемость путей (для Docker/Linux; на Windows работают дефолты):** `ORQ_DATA_ROOT`
  (корень служебных папок; в контейнере `/data`), `ORQ_LEADS_DIR` (дефолт `D:\лиды` / `/data/leads`),
  `RUSPROFILE_PROFILE_DIR` / `RUSPROFILE_COOKIES_FILE` (иначе — абсолютные пути с именем `abalb`
  внутри копии-скилла, см. «Подводные камни»), `KIMI_DIR` / `KIMI_PY` (папка и python-бинарь
  venv стадии Kimi). Веб-обвязка читает ещё `ORQ_REPO_ROOT` / `ORQ_PYTHON` / `ORQ_DISK_BASE`.
- **Kimi-писатель:** ключ — тот же `KIMI_API_KEY`→`GPLLM_API_KEY`; модель едина для всех стадий —
  `KIMI_MODEL_NAME=kimi-k2.7-code` (зафиксирована Kimi-only guard); агентный режим — дефолт,
  `KIMI_WRITER_AGENT=0` включает прежний одиночный HTTP-режим. Агент: `KIMI_WRITER_MAX_STEPS`
  (не задан = config Kimi CLI; на этой установке safety guard 1000, `0` тоже не переопределяет;
  положительное значение задаёт операторский cap), `KIMI_WRITER_AGENT_TIMEOUT` (0 = без общего
  таймаута), `ORQ_KIMI_TOOL_TIMEOUT` (180 на один сетевой/краул-вызов); legacy HTTP:
  `KIMI_WRITER_MAX_TOKENS` (16000), `KIMI_WRITER_TIMEOUT` (600), `_ATTEMPTS` (3).
- **`.env.example` в репо нет**. Локальные секреты хранятся в gitignored `env/.env`;
  Docker подключает его через `env_file`, а в build context папка не попадает. Для Docker минимум:
  `KIMI_API_KEY` (или `GPLLM_API_KEY`),
  `OFDATA_API_KEY` (поиск — OfData; выручка — `/finances` либо автоматический ГИР БО), опц.
  `DADATA_TOKEN`/`YANDEX_DISK_TOKEN`/`CHECKO_TOKEN`
  (последний нужен только при `ORQ_STORE=disk`).

## Развёртывание и веб-интерфейс (добавлено 2026-07-14)

- **Docker / Timeweb Cloud** (`Dockerfile`, `docker-compose.yml`, `DEPLOY_TIMEWEB.md`). Целевой
  прод — сервер в Москве; весь модельный runtime уже переведён на Kimi. Образ x86-64-only
  (`google-chrome-stable` был amd64). Два venv (`/opt/venv` + `/opt/kimi-venv`), конфликт
  `pydantic-core` сохраняется. **Chrome/Xvfb из образа убраны** → `source_rusprofile` в контейнере
  неработоспособен; источник лидов там — `source_ofdata.py` (HTTPS, без антибота). Chromium в образе
  остаётся (headless, Playwright) — им краулит Crawl4AI и рендерит PDF one-pager. Сборка — build-gate:
  падает, если `py_compile`/`test_deep_research.py`/`apply_patches.py --check`/импорт стадии не прошли.
  ⚠️ Главный операционный риск не решается кодом — антибот RusProfile против IP дата-центра
  (см. `DEPLOY_TIMEWEB.md §3`); на Checko-пути он неактуален.
- **Веб-интерфейс** (`web/`, FastAPI + React/Vite; свой `README.md`). Своей БД нет — источник правды
  тот же, что у CLI (`ORQ_LEADS_DIR`, кэш находок, хранилище `deliverables/`); журнал прогонов —
  `web_jobs/<id>/`. Карточка компании отдаёт готовые .docx/.pdf на скачивание из хранилища сайта:
  `GET /api/leads/{инн}/file/{slug}` (`slug` = `process|roles|onepager`) → `FileResponse`
  (`web/api/leads.deliverable_file`; путь тот же `DO.deliverables_subdir`+`_doc_names`, что у пайплайна).
  `web/api` **спавнит `orchestrator.py` подпроцессом** (импортировать нельзя — `main()` забирает
  stdout под `_Tee` и зовёт `sys.exit`) и парсит русский stdout в SSE-события (`events.py`;
  структурированных событий нет, префиксы стабильны — тест `web/api/test_events.py` на реальных логах).
  Один активный прогон (второй → `409`: параллельные прогоны не залочены и не безопасны). Объём —
  через `--per-industry` (UI показывает `N × отраслей`), не `--count`. Выставлять API наружу без
  авторизации нельзя — он запускает платные прогоны.

## Подводные камни

- **Две копии кода:** живая `D:\lead_gen\lead_orchestrator` — **авторитетная, её запускает ярлык**;
  правь её. Копия-скилл `C:\Users\abalb\.claude\skills\lead-finder\scripts` — **старого поколения**
  (`DOSSIER_SYSTEM`/`save_dossier_docx`, один документ — НЕ текущая пресейл-архитектура из 2 .docx),
  а НЕ «отличается только CRLF/LF». Синхронизируй копию-скилл только осознанно, не автоматически.
  Cookie рабочего проекта лежат в исключённом из Git `env/rusprofile_cookies.json`;
  прежний `~\.claude\skills\lead-finder\.rp_cookies.json` читается только как legacy fallback.
  Chrome-профиль ручного логина всё ещё хранится в копии-скилле, поэтому удалять её без
  резервной копии нельзя. `lead_orchestrator/README.md` тоже местами старого
  поколения («досье и стратегия», $1–2/компания), как и внутренние докстринги живого кода (шапки
  `orchestrator_agent.py`/`run_orchestrator.cmd`, «yacli-фолбэк» в `yadisk_client.py`, «2 файла»
  в `disk_organize.py`, yacli в `pipeline.py`) — при расхождениях авторитет этот CLAUDE.md.
- **Порог выручки строгий:** компании без подтверждённой выручки или ниже порога отсекаются
  (`source_rusprofile.py`, серверный фильтр `finance_revenue_from` + клиентская перепроверка).
  Порядок ответа API не считается сортировкой: до отбора N читается до 20 доступных страниц,
  затем применяется явная сортировка `finance_revenue` по убыванию.
  Регион, в отличие от выручки, фильтруется ТОЛЬКО клиентски (серверного фильтра нет).
  Отрицание региона («НЕ Москва») убирает город фед. значения, но НЕ трогает одноимённую
  область («Московская область» остаётся) — см. `region_excluded()`. У ВКЛЮЧАЮЩЕГО фильтра
  асимметрия обратная: `--region "Москва"` захватывает и Московскую область
  (в `region_included()` такой защиты нет).
- **Сбор RusProfile — headed Playwright Chromium «за экраном» (offscreen)** по умолчанию: headless
  может не пройти антибот Cloudflare. `--show-browser` — видимое окно; `--headless` оставлен
  скрытым флагом для совместимости. Offscreen-дефолт и скрытие `--headless` — уровень
  `orchestrator.py`; `RUSPROFILE_BROWSER=uc` включает прежний undetected-chromedriver fallback.
- **⚠️ Яндекс Диск молча врёт об успехе заливки (найдено 2026-07-13).** Наблюдались ДВА режима:
  (1) клиент вернул «✓ Загружено», а файла на Диске нет вовсе (прямой GET → 404);
  (2) при `overwrite=True` рапортует успех, но остаётся СТАРЫЙ файл (прежние размер и md5).
  Сбой перемежающийся, причина не установлена (Корзина пуста, места полно, десктоп-клиент не
  запущен, ручная заливка тех же файлов проходит). На бэкфилле это стоило 7 «призраков» —
  компаний, помеченных готовыми без файлов на Диске.
  **Поэтому рапорту `DO._upload` верить НЕЛЬЗЯ.** Вся заливка идёт через
  `orchestrator._upload_verified`: после записи папка перечитывается, сверяется размер, при
  расхождении старый файл сносится в Корзину (перезапись не работает) и файл льётся заново (3×).
  Не «упрощать» обратно к голому `DO._upload` — иначе прогон снова будет отчитываться об успехе
  при пустой папке, а резюм такую компанию не догонит (она уже числится сделанной).
- **Яндекс Диск 423 (DiskResourceLockedError):** файлы, синхронизированные десктоп-клиентом
  Я.Диска, лочатся — перезапись существующего файла даёт 423. `_mkdir`/`_upload` ретраят, но
  устойчивый лок не снимется. Обходы: заливка в соседнюю папку, либо пауза синка Я.Диска.
  Удаление/Корзина — тулзы MCP `yadisk` или `connectors/yadisk_client.py` напрямую.
- **ЕИС/zakupki.gov.ru** под антиботом: карточки извещений напрямую не парсятся — даются ссылки на
  выдачу по ИНН; контактные лица закупок берутся из поисковой выдачи; полный парсинг — за
  `FIRECRAWL_API_KEY`/browser-use.
- **Консоль Windows = cp1251** и роняет вывод на кириллице/₽ → скрипты принудительно ставят UTF-8
  stdout; для дампа docx-текста выгружай в файл, а не в консоль.
- **RAM:** каждый боевой ресёрч запускает Kimi-агентов и веб-инструменты в отдельных процессах;
  оркестратор авто-снижает `--workers` до 1 при нехватке памяти. One-pager легче прежней .pptx,
  но каждая его стадия — свой python + chromium: параллелизм ограничен `ORQ_ONEPAGER_CONCURRENCY`=2.
- **Изоляция SDK сохраняется.** Все Kimi Agent SDK-стадии выполняются подпроцессом из
  `lead_orchestrator_kimi/.venv_kimi`; ставить `kimi-agent-sdk` в основное окружение нельзя из-за
  конфликта `pydantic-core` с сохранёнными legacy-зависимостями. Это не означает вызов Claude:
  штатный маршрут только запускает Kimi-процессы.
- **Место на C::** системный диск тесный; temp вынесен на `D:\orq_tmp` и чистится по компаниям.

## Конвенции

- Python 3.12, запуск `py`. Комментарии и строки — на русском, под стиль соседнего кода.
- Git: корень репозитория — **`D:\lead_gen`** (перенесён туда 2026-07-14, коммит `075dcdb`; раньше
  был внутри `lead_orchestrator/` — старые доки/докстринги ещё говорят так). Под git теперь ОБЕ папки
  (`lead_orchestrator/` + `lead_orchestrator_kimi/`), `web/` и docker/деплой-доки. Origin
  `github.com/Aidesade/lead_orchestrator`, рабочая ветка `kimi` (основная `master`); коммиты на русском.
- Перед коммитом гонять `py -m py_compile` изменённых файлов. Офлайн script-тесты (не pytest):
  `py test_kimi_only.py` — регрессия единого Kimi K2.7 runtime и запрет Claude в штатных entrypoint;
  `py test_kimi_agent_freedom.py` — контракт свободного Kimi Agent loop;
  `py test_deep_research.py` — смоук движка (гонять при правках `deep_research_engine.py`);
  `py web/api/test_events.py [лог ...]` — парсер stdout оркестратора в SSE-события, проверяется
  по НАСТОЯЩИМ логам прошлых прогонов (`D:\orq_tmp\run_*.log`, по умолчанию берёт их сам).
  Гонять при правках печати в `orchestrator.py`: веб парсит именно эти русские префиксы —
  переименовал строку вывода, молча сломал прогресс в UI.
- Не плодить параллельные модули: формат .docx и промпт `PRESALE_SYSTEM` живут в CRA, ресёрч —
  в `deep_research_engine.py`, обвязка 3-й стадии — в `orchestrator.py` (`_onepager_one`), а сама
  стадия (промпт, канон, рендер) — в `../lead_orchestrator_kimi/`.
- Изменив `orchestrator.py`/CRA — прогнать `py -m py_compile`; быстрый офлайн-чек всей цепочки:
  `py orchestrator.py <leads.json> --dry-run --no-upload` (заглушки обоих .docx; заглушка .pdf —
  если выполнены предусловия стадии: venv Kimi + фото + ключ). Проверить саму стадию без оплаты
  ресёрча: вызвать `orchestrator._onepager_one(lead, 0, out.pdf, findings)` напрямую.
