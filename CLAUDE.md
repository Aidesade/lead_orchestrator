# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Оркестратор лидогенерации для B2B-агентства, продающего **корпоративную on-premises
LLM-платформу** (RAG-база знаний, автономные ИИ-агенты, ИИ-Коуч «Наставник»).
Ядро пайплайна — в подпапке **`lead_orchestrator/`**; рядом — изолированная стадия one-pager
**`lead_orchestrator_kimi/`** (свой venv), веб-интерфейс **`web/`** и контейнеризация под прод
(`Dockerfile`/`docker-compose.yml`). Корень репозитория — **`D:\lead_gen`**. Основная платформа —
Windows, Python 3.12, запуск через `py`; прод-цель — Docker в Linux. Комментарии в коде — на русском.

## Что делает

Полная цепочка в две фазы (детерминированный Python-оркестратор, НЕ LLM-оркестратор):

- **ФАЗА 1 — сбор.** Источник переключается env `LEAD_SOURCE`: `rusprofile` (дефолт; ОКВЭД,
  живой Chrome + платная сессия) либо `checko` (Checko API, без браузера/антибота — в Dockerfile
  стоит `ENV LEAD_SOURCE=checko`). Общий строгий фильтр — **выручка ≥ порога (дефолт 1 млрд ₽)**;
  на пути RusProfile выручка берётся ТОЛЬКО из RusProfile (ГИР БО/Dadata/Checko не зовутся),
  на пути Checko её нет в `/search` → добор из ГИР БО + фильтр в коде.
  Дальше → обогащение контактов (платный аккаунт) → отбор → `D:\лиды\leads_<отрасли>.json`
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
`orchestrator_agent.py` → **`orchestrator.py`** (именно этот файл реально работает).
Рядом `run_kimi_orchestrator.cmd` — то же самое, но заранее выставляет env провайдера Kimi
(`KIMI_API_KEY`←`GPLLM_API_KEY`, `KIMI_BASE_URL`, `KIMI_MODEL_NAME`) и предупреждает, если ключа
нет (тогда стадия one-pager молча пропускается). Оба .cmd — строго ASCII+CRLF: cmd.exe портит
UTF-8/LF батники.

```
# полная цепочка (сбор + ресёрч + 3 файла), ВСЯ отрасль (по умолчанию 200 компаний):
py orchestrator.py --industries mining            # = py orchestrator.py mining (позиционно — только ключи карты INDUSTRY)
py orchestrator.py mining --count 10              # явно 10
# только ресёрч+материалы по готовому JSON лидов (ФАЗА 1 пишет их в D:\лиды\):
py orchestrator.py "D:\лиды\leads_mining.json"
# без 3-й стадии (one-pager .pdf), только 2 .docx:
py orchestrator.py mining --no-presentation
# писатель двух .docx на Kimi вместо Claude (дёшево/без Anthropic; веб-UI так делает по умолчанию):
py orchestrator.py "D:\лиды\leads_mining.json" --model kimi
# локально без заливки на Диск (файлы остаются в temp, путь печатается):
py orchestrator.py "D:\лиды\leads_mining.json" --no-upload
# дёшево проверить связку без LLM и без следов (заглушки обоих .docx; заглушка .pdf — если стоят предусловия стадии):
py orchestrator.py "D:\лиды\leads_mining.json" --dry-run --no-upload
# разовый ресёрч одной компании (оба .docx -> ~/Downloads):
py company_research_agent.py "АО Рязаньавтодор ИНН 6234065445"
py company_research_agent.py --contacts "АО Рязаньавтодор 6234065445"   # только официальная база, без ресёрча и .docx
# разовый прямой контакт ЛПР (ФИО+ИНН -> рабочие email/телефоны, только легитимные источники).
# ⚠️ standalone-дефолты ПРОТИВОПОЛОЖНЫ оркестраторным: SMTP-проба и соцпоиск ВКЛ (гасить --no-verify / --no-social):
py person_enrich.py "Руденко Сергей Александрович" 6234065445
# движок deep_research отдельно, БЕСПЛАТНО (regex-only, без LLM):
DR_USE_LLM=0 py deep_research_engine.py --company "АО Рязаньавтодор" --inn 6234065445 --site https://avtodor-rzn.ru
py deep_research_engine.py --crawl avtodor-rzn.ru # отладка: только краул сайта (SiteCrawler)
# разовый логин RusProfile (cookie в .rp_cookies.json):
py rusprofile_session.py --login
# сбор лидов через Checko API (без браузера/антибота; годится для Docker/Linux) -> leads.json:
py source_checko.py --industries processing --min-revenue 1e9 --region Татарстан --out leads.json
# Docker (из корня D:\lead_gen; секреты в .env): собрать и прогнать 10 компаний:
docker compose build && docker compose run --rm lead-orchestrator mining --count 10
# веб-интерфейс (из корня): API + фронт (подробности web/README.md):
py -m uvicorn web.api.main:app --port 8000      # затем web/ui: npm run dev / npm run build
```

Через ярлык/обёртку достаточно назвать **только отрасль** (`mining`, «добыча угля», «нефтегаз»…):
`orchestrator_agent.py` соберёт **200** компаний (если не задано иное) и выдаст по каждой 3 файла;
перед боевым прогоном называет оценку (~$3–4.5/компания) и просит короткое подтверждение.
Полезные флаги `orchestrator.py`: `--count N` (ВСЕГО по всем отраслям, дефолт 200),
`--per-industry N` (НА КАЖДУЮ отрасль — перекрывает `--count`: итог = N × число отраслей;
в NL-обёртке это `count_per_industry` — «по 10 на отрасль»), `--min-revenue 1e9`, `--region "..."`
(поддерживает ОТРИЦАНИЕ: `"НЕ Москва"` = вся РФ кроме Москвы; смешивание через запятую —
`"Урал, НЕ Москва"`; также `!X`/`-X`/`кроме X`), `--workers N` (дефолт 2, авто→1 при <3 ГБ RAM —
проверка только в боевом запуске, в dry-run её нет), `--model opus|sonnet`, `--no-presentation`,
`--no-person-enrich`, `--show-browser`, `--out <json>` (дефолт `D:\лиды\leads_<отрасли>.json`;
старый .xlsx-путь тоже примется — расширение заменится на .json),
`--base` (корень Диска, дефолт `disk:/Лиды`), `--account`, `--redo` (ВЫКЛючить резюм — переделать
даже компании, у которых на Диске уже лежат реальные документы). NL-обёртка `orchestrator_agent.py`
прокидывает лишь подмножество флагов (нет `--no-person-enrich`/`--out`/`--base`/`--headless`).

## Карта кода (`lead_orchestrator/`)

| Файл | Роль |
|---|---|
| `orchestrator.py` | **главный вход** ФАЗА 1+2 (asyncio). `_collect` = ФАЗА 1; `_research_one` = ресёрч+2 .docx; `_onepager_one` = 3-я стадия (one-pager .pdf: ПОДПРОЦЕСС venv-питона `../lead_orchestrator_kimi`); `_presentation_prereqs` = venv Kimi + фото + ключ; `_kimi_env` = KIMI_API_KEY (фолбэк `GPLLM_API_KEY`) / KIMI_BASE_URL / KIMI_MODEL_NAME; `_no_sleep` блокирует сон Windows на время прогона |
| `orchestrator_agent.py` | SDK-обёртка (NL→запуск `orchestrator.py` подпроцессом); дефолт count=200, презентация по умолчанию; прокидывает не все флаги CLI |
| `../lead_orchestrator_kimi/` | **3-я стадия целиком** (свой venv, Kimi Agent SDK): `onepager_kimi.py` (канон-константы + CLI), `onepager_system.py` (промпт), `html_to_pdf.py` (HTML→PDF), `requirements.txt` (лок: `kimi-agent-sdk==0.0.5` + `kimi-cli==1.12.0` + `playwright==1.60.0`), `patches/apply_patches.py` (один идемпотентный скрипт патчей venv, есть `--check`). Свой `CLAUDE.md` — читать перед правкой стадии |
| `web/` (корень репо) | **веб-интерфейс** (FastAPI + React/Vite). `web/api` спавнит `orchestrator.py` подпроцессом и парсит его stdout в SSE-события (структурированных событий у оркестратора нет); один активный прогон (второй → 409). Свой `README.md` |
| `Dockerfile` / `docker-compose.yml` / `DEPLOY_TIMEWEB.md` (корень) | **контейнеризация под Timeweb Cloud (Москва)**. Два venv в образе (основной + `/opt/kimi-venv`), БЕЗ Chrome/Xvfb → `source_rusprofile` в контейнере неработоспособен (источник — Checko). Сборка = build-gate (`py_compile`, офлайн `test_deep_research.py`, `apply_patches.py --check`). `AUDIT_KIMI.md` — аудит стадии Kimi |
| `assets/` | `bulat_zamaliev.png` — фото эксперта (вшивается в one-pager). `citrt_logo.png` остался от .pptx-стадии; в one-pager логотип — текстовый словомарк, PNG не нужен |
| `company_research_agent.py` (CRA) | **формат документов и промпты**. С 2026-07-16 — ПО ПРОМПТУ НА ДОКУМЕНТ: `PROCESS_MAP_SYSTEM` / `ROLES_CONTACTS_SYSTEM` (задача заказчика дословно, метод отдан агенту) — их берёт Kimi-писатель; `PRESALE_SYSTEM` (один общий) остался только для ветки писателя на Claude; `STAGE1_SYSTEM` — триаж (не удалять: `run_stage1_triage` его зовёт → был NameError). Рендереры `_write_process_map_docx`/`_write_roles_contacts_docx`, схемы `PROCESS_MAP_SCHEMA` (`process_catalog` — полный каталог процессов, включая без точки внедрения ИИ, ОТДЕЛЬНО от `processes` = 8–14 детальных карточек: каталог отделён и ради лимита `KIMI_WRITER_MAX_TOKENS`, иначе JSON рвётся) / `ROLES_CONTACTS_SCHEMA` (`contacts_table` — сводная таблица контактов по корзинам; `ecosystem_table` — «Экосистема и вертикаль принятия решений»; писателю разрешена доверка по ЛЮБЫМ существенным пробелам, не только ИТ/тендер), тул `deep_research`; CLI-режим `--contacts`; standalone-модели захардкожены (писатель opus, триаж sonnet) — `--model` оркестратора на CRA-CLI не влияет |
| `writer_kimi.py` | **альтернативный писатель двух .docx на Kimi** (`--model kimi`). НЕ агент: инструментов нет, ресёрч уже выполнен движком → модель HTTP-вызовом (OpenAI-совместимый `openai`, БЕЗ kimi-agent-sdk → тот же основной venv) возвращает JSON по ТЕМ ЖЕ схемам CRA, рендерят ТЕ ЖЕ `CRA._write_*_docx`. По вызову НА ДОКУМЕНТ: свой промпт (`CRA.PROCESS_MAP_SYSTEM`/`ROLES_CONTACTS_SYSTEM` + `SYSTEM_TAIL`) и свои находки (`write_two_docx(..., findings_process, findings_roles)`). `SYSTEM_TAIL` явно ОТМЕНЯЕТ требование звать `save_*_docx` — у Kimi инструментов нет, деливерабл там JSON. `is_kimi`/`kimi_model` зовёт `orchestrator` ещё на разборе флагов, поэтому CRA импортится лениво. Env: `KIMI_WRITER_MODEL`→`KIMI_MODEL_NAME` (дефолт `kimi-k2.7-code`), `KIMI_WRITER_MAX_TOKENS`/`_TIMEOUT`/`_ATTEMPTS`. Следствие: у Kimi-писателя НЕТ веб-инструментов — качество ограничено полнотой находок движка |
| `source_checko.py` | **источник лидов через Checko API — замена RusProfile без браузера/антибота** (обычный HTTPS). Drop-in для `source_rusprofile.harvest()`; в `_collect` ПОДКЛЮЧЁН — включается `LEAD_SOURCE=checko` (`_collect_checko`), в Docker это дефолт. Плюс свой CLI → `leads.json` для ФАЗЫ 2. ОКВЭД матчится ТОЧНО (не префиксом) и не короче NN.NN, поэтому `usable_okved` разворачивает коды отраслей уровня NN/NN.N в подклассы через `okved2_codes.py` — работают все 21 отрасль (правка `INDUSTRY` руками не нужна). Регион — свой `resolve_region`: негатив разбирается ПО КАЖДОМУ элементу списка («Дагестан, не Москва» = вкл. Дагестан / искл. Москву), аббревиатуры (ХМАО/ЯНАО/СПб/МСК) — через `source_rusprofile.REGION_ALIASES`. Ключи ротируются на 403/лимите: `CHECKO_TOKEN`→`CHECKO_TOKEN_ALT`/`_2`/`_3` (общий `checko_keys()`, рабочий ключ запоминается). Выручки в /search нет → добор из ГИР БО + фильтр порогом в коде; ИП (`obj=org`) не попадают |
| `okved2_codes.py` | справочник ОКВЭД-2 (ОК 029-2014): 623 подкласса NN.NN, снимок 2026-07-15 с github.com/carono/okvad2. Нужен ТОЛЬКО `source_checko` (разворачивает NN/NN.N в подклассы); RusProfile им не пользуется |
| `deep_research_engine.py` | **настоящий deep-research**: supervisor — шаг-0 официальная база (`official_lookup`, блокирующе) + 4 параллельных коллектора (site/eis/courts_media/hh), Crawl4AI BestFirst (PRIMARY) → HTTP-фолбэк, петля целевого добора пустых ячеек, `completeness_critic` (в т.ч. ячейка «экосистема»: учредитель/ведомство/сёстры-структуры/комиссии — добавлено 2026-07-06), `consolidate` (у строк source URL); сбой supervisor'а пайплайн не роняет. Поиск — мульти-бэкенд brave→ddg→bing (ротация, кулдаун 90 с на 429). **Мульти-домен** (2026-07-06): `discover_domains` подтверждает до `DR_MAX_DOMAINS`=3 сайтов (у госструктур свой сайт + ведомственный портал), краулятся все (доп. — половинный кап страниц); дополнительные домены — строго по ИНН на странице либо по полной фразе названия в SERP-сниппете, когда портал не отдаётся HTTP (анти-бот; краул сделает Crawl4AI); карточки-каталоги режутся блок-листом `AGGREGATORS` и формой URL (глубокий путь/query = отказ). Валидация `_text_belongs` для «родовых» названий («Центр информационных технологий») требует ПОЛНУЮ фразу или ИНН — одиночное слово матчило и Центробанк (кейс cbr.ru); отличительные токены — по границе слова. Чужой ДОСТУПНЫЙ `website` из Фазы-1 отбрасывается, недоступный берётся как есть |
| `source_rusprofile.py` | сбор RusProfile по ОКВЭД (uc, антибот); карта `INDUSTRY` (21 отрасль); строгий фильтр выручки; `parse_region_query` — регион-фильтр с отрицанием («НЕ Москва», списки через запятую) |
| `rusprofile_session.py` | контакты с платного аккаунта (cookie; `--login`) |
| `pipeline.py` | отбор `_select` + сохранение `_save`; его СОБСТВЕННАЯ полная цепочка `run()` (RusProfile→ГИР БО→Checko→site_verify) работает только при прямом `py pipeline.py` — оркестратор её не вызывает |
| `build_excel.py` | выгрузка .xlsx — из боевой ФАЗЫ 1 УБРАНА (2026-07-06, только JSON); используется лишь standalone-цепочкой `pipeline.run()` |
| `disk_organize.py` | пути/папки на Диске поверх `connectors/yadisk_client` (`_mkdir`/`_upload` с ретраями на 423 и транзиентные сетевые сбои — `_is_locked`/`_is_transient`), заглушки .docx/.pdf (`generate_presentation`, `_make_pdf` — голый PDF без зависимостей; текст транслитерирован, т.к. базовые шрифты PDF кириллицу не несут) |
| `connectors/` | **пакет коннекторов.** `yadisk_client.py` — ядро Яндекс Диска на официальном REST API (stdlib, без зависимостей; строковые операции `_info/_list/_mkdir/_upload/...` + исключение-обёртки `ensure_dir`/`upload_file` для пайплайна); `yadisk_mcp.py` — MCP-обёртка над ним |
| `revenue_enrich.py` | выручка из ГИР БО ФНС (бесплатно, стр. 2110) — официальная база ФАЗЫ 2 и `pipeline.run()`; в `_collect` НЕ участвует |
| `dadata_enrich.py` / `checko_enrich.py` | карточка ЕГРЮЛ (Dadata) / контакты (Checko) |
| `person_enrich.py` | ЛПР: ФИО+ИНН → прямой РАБОЧИЙ контакт (Dadata/Checko → домен с валидацией → email по шаблону+MX, телефоны; соцпрофили — только подтверждённые ИНН-контекстом). Вызывается из `_research_one` ДО писателя, блок дописывается к находкам. «Пробив»/утечки конструктивно исключены (`DENY_SOURCES`) |
| `email_finder.py`, `site_verify.py`, `harvest_inn_site.py`, `inn_util.py`, `webutil.py`, `browser_util.py` | утилиты |
| `test_deep_research.py` | смоук-тест логики движка (без сети/LLM): `py test_deep_research.py` |
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
3. **Писатель.** Развилка по `--model` (`writer_kimi.is_kimi`): **Claude** (`opus`/`sonnet`,
   `PRESALE_SYSTEM` — один общий промпт, ветка сохранена как есть) — агент с инструментами,
   получает находки и обязан вызвать ОБА `save_*_docx` (есть нудж-ретрай, если не сохранил);
   либо **Kimi** (`--model kimi`, `writer_kimi.write_two_docx`) — не агент, по ОДНОМУ JSON-вызову
   на документ, у каждого СВОЙ системный промпт (`CRA.PROCESS_MAP_SYSTEM` / `CRA.ROLES_CONTACTS_SYSTEM`
   + `SYSTEM_TAIL`) и СВОИ находки со своего прохода → те же рендереры (см. карту кода).
   CLI-дефолт — `opus`; веб-UI по умолчанию `kimi`. На Kimi долларовая вилка `[оценка]` не печатается (цену за
   вызов шлюз наружу не отдаёт → итог `$0`, это не баг).
4. **Гард против болванок:** заливаются только реальные .docx (>5000 байт, проверка в `process()`);
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
- **Таймауты сессий** `ORQ_RESEARCH_TIMEOUT`=1800с / `ORQ_ONEPAGER_TIMEOUT`=900с на попытку:
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

- Аутентификация моделей: `ANTHROPIC_API_KEY` **или** подписочный логин `claude` CLI.
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
- Опциональные токены: `DADATA_TOKEN`, `CHECKO_TOKEN` (+ запасные `CHECKO_TOKEN_ALT`/`_2`/`_3` —
  ротация на 403/лимите) — без них работают только ГИР БО + веб.
- Пресейл-брендинг (.docx): `PRESALE_VENDOR` (строка «Подготовлено для», по умолчанию пусто),
  `PRESALE_PLATFORM_DESC`.
- **Стадия one-pager (.pdf):** живёт в `../lead_orchestrator_kimi/` — нужны её venv
  (`.venv_kimi`: `kimi-agent-sdk==0.0.5` + `kimi-cli==1.12.0` + `playwright==1.60.0` по
  `requirements.txt`, **плюс патчи venv** через `patches/apply_patches.py` — см. `CLAUDE.md` там;
  пин НЕ снимать: без него резолвер ставил kimi-cli 1.12, а патч бил вслепую под 1.4x → `TypeError`),
  фото `lead_orchestrator/assets/bulat_zamaliev.png` и **ключ провайдера Kimi**: `KIMI_API_KEY`,
  а если его нет — `GPLLM_API_KEY` (так он задан на этой машине). `KIMI_BASE_URL`
  (дефолт `https://gpllmkeeper.dtc.tatar/v1`) и `KIMI_MODEL_NAME` (дефолт `kimi-k2.7-code`)
  перекрываются через env. Нет предусловий → стадия мягко пропускается (2 .docx делаются как обычно).
  `GEN_PRESENTATION=0` / `--no-presentation` отключают. LibreOffice/Poppler/node/скилл `pptx`
  этой стадии больше НЕ нужны (Poppler остаётся полезен для ручной проверки PDF: `pdfinfo`/`pdftoppm`).
- Бренд — платформа **Telepatt** от **АО «ЦИТ РТ»** (госкомпания РТ, citrt.ru). ⚠️ Факты о людях,
  контакты и реквизиты в one-pager ЗАХАРДКОЖЕНЫ (`SPEAKER_FACTS`, `FOOTER`, `VENDOR_PHONE`/
  `VENDOR_CONTACT_PERSON` (Шабанов Али Магомедович — строкой под телефоном)/`VENDOR_EMAIL`/
  `VENDOR_SITE` в `onepager_kimi.py`) — модель их выдумывала. Эксперт «Булат Замалиев» публично подтверждается как «Уполномоченный по технологиям ИИ
  при Минцифры РТ»; связь с ЦИТ РТ как «руководителя направления» публично НЕ подтверждена — в
  one-pager стоит подтверждённая формулировка, не утверждать вторую как факт.
- Тумблеры движка: `DR_LLM_PROVIDER` — провайдер LLM-экстракта: `claude` (дефолт, claude-agent-sdk)
  либо `kimi` (тот же OpenAI-совместимый шлюз, что у писателя; `DR_KIMI_MAX_TOKENS`=8000).
  `kimi` нужен, когда Claude недоступен (прод в РФ / Docker) — тогда **весь пайплайн работает
  без Anthropic**; в Dockerfile это дефолт (`ENV DR_LLM_PROVIDER=kimi`). Далее:
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
- **Переключатели окружения (дефолты Windows ≠ дефолты Docker):** `LEAD_SOURCE`
  (`rusprofile` | `checko`), `DR_LLM_PROVIDER` (`claude` | `kimi`), `ORQ_STORE`
  (`local` | `disk`). Первые два в образе перекрыты на `checko`/`kimi` (`Dockerfile`, блок ENV
  в конце) — так контейнер живёт без Chrome и без Anthropic; на Windows переменные не заданы
  и работают дефолты `rusprofile`/`claude`.
- **Портируемость путей (для Docker/Linux; на Windows работают дефолты):** `ORQ_DATA_ROOT`
  (корень служебных папок; в контейнере `/data`), `ORQ_LEADS_DIR` (дефолт `D:\лиды` / `/data/leads`),
  `RUSPROFILE_PROFILE_DIR` / `RUSPROFILE_COOKIES_FILE` (иначе — абсолютные пути с именем `abalb`
  внутри копии-скилла, см. «Подводные камни»), `KIMI_DIR` / `KIMI_PY` (папка и python-бинарь
  venv стадии Kimi). Веб-обвязка читает ещё `ORQ_REPO_ROOT` / `ORQ_PYTHON` / `ORQ_DISK_BASE`.
- **Kimi-писатель (`--model kimi`):** ключ — тот же `KIMI_API_KEY`→`GPLLM_API_KEY`; модель —
  `KIMI_WRITER_MODEL`→`KIMI_MODEL_NAME` (дефолт `kimi-k2.7-code`); плюс `KIMI_WRITER_MAX_TOKENS`
  (16000), `KIMI_WRITER_TIMEOUT` (600), `KIMI_WRITER_ATTEMPTS` (3).
- **`.env.example` в репо нет**, но де-факто список секретов — блок `x-orch-env` в
  `docker-compose.yml` (все `${...}` оттуда). Локальный `.env` в корне — gitignored, не коммитить
  (см. `DEPLOY_TIMEWEB.md`: права 600). Для Docker минимум: `KIMI_API_KEY` (или `GPLLM_API_KEY`),
  `CHECKO_TOKEN` (источник лидов там — Checko), опц. `DADATA_TOKEN`/`YANDEX_DISK_TOKEN`
  (последний нужен только при `ORQ_STORE=disk`).

## Развёртывание и веб-интерфейс (добавлено 2026-07-14)

- **Docker / Timeweb Cloud** (`Dockerfile`, `docker-compose.yml`, `DEPLOY_TIMEWEB.md`). Целевой
  прод — сервер в Москве (все внешние сервисы российские; Россия вне supported-countries Anthropic
  → на прод-сервере Claude не жилец, писатель переносится на Kimi). Образ x86-64-only
  (`google-chrome-stable` был amd64). Два venv (`/opt/venv` + `/opt/kimi-venv`), конфликт
  `pydantic-core` сохраняется. **Chrome/Xvfb из образа убраны** → `source_rusprofile` в контейнере
  неработоспособен; источник лидов там — `source_checko.py` (HTTPS, без антибота). Chromium в образе
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
  ⚠️ Cookie и Chrome-профиль платного RusProfile захардкожены В ПАПКЕ КОПИИ-СКИЛЛА
  (`rusprofile_session.py:38-39` → `~\.claude\skills\lead-finder\.rp_profile` / `.rp_cookies.json`) —
  снести её = потерять логин RusProfile. `lead_orchestrator/README.md` тоже местами старого
  поколения («досье и стратегия», $1–2/компания), как и внутренние докстринги живого кода (шапки
  `orchestrator_agent.py`/`run_orchestrator.cmd`, «yacli-фолбэк» в `yadisk_client.py`, «2 файла»
  в `disk_organize.py`, yacli в `pipeline.py`) — при расхождениях авторитет этот CLAUDE.md.
- **Порог выручки строгий:** компании без подтверждённой выручки или ниже порога отсекаются
  (`source_rusprofile.py`, серверный фильтр `finance_revenue_from` + клиентская перепроверка).
  Регион, в отличие от выручки, фильтруется ТОЛЬКО клиентски (серверного фильтра нет).
  Отрицание региона («НЕ Москва») убирает город фед. значения, но НЕ трогает одноимённую
  область («Московская область» остаётся) — см. `region_excluded()`. У ВКЛЮЧАЮЩЕГО фильтра
  асимметрия обратная: `--region "Москва"` захватывает и Московскую область
  (в `region_included()` такой защиты нет).
- **Сбор RusProfile — headed-Chrome «за экраном» (offscreen)** по умолчанию: настоящий headless
  может не пройти антибот Cloudflare. `--show-browser` — видимое окно; `--headless` оставлен
  скрытым флагом для совместимости. Offscreen-дефолт и скрытие `--headless` — уровень
  `orchestrator.py`; прямой запуск `source_rusprofile.py`/`pipeline.py` открывает ВИДИМОЕ окно.
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
- **RAM:** каждый ресёрч = свой `claude` CLI (Node) + Playwright; при нехватке падает 0xC0000409 —
  оркестратор авто-снижает `--workers` до 1. One-pager легче прежней .pptx (нет LibreOffice+node),
  но каждая его стадия — свой python + chromium: параллелизм ограничен `ORQ_ONEPAGER_CONCURRENCY`=2.
- **Два несовместимых SDK в одном прогоне.** `kimi-agent-sdk` (стадия one-pager) и `claude-agent-sdk`
  (ресёрч/писатель) конфликтуют по `pydantic-core` — ставить Kimi в ОСНОВНОЕ окружение НЕЛЬЗЯ, это
  ломает рабочий пайплайн (уже случалось). Стадия зовётся подпроцессом venv-питона соседней папки;
  правки стадии — только там, по её `CLAUDE.md` (в venv два обязательных патча, хрупких к upgrade).
- **Место на C::** системный диск тесный; temp вынесен на `D:\orq_tmp` и чистится по компаниям.

## Конвенции

- Python 3.12, запуск `py`. Комментарии и строки — на русском, под стиль соседнего кода.
- Git: корень репозитория — **`D:\lead_gen`** (перенесён туда 2026-07-14, коммит `075dcdb`; раньше
  был внутри `lead_orchestrator/` — старые доки/докстринги ещё говорят так). Под git теперь ОБЕ папки
  (`lead_orchestrator/` + `lead_orchestrator_kimi/`), `web/` и docker/деплой-доки. Origin
  `github.com/Aidesade/lead_orchestrator`, рабочая ветка `kimi` (основная `master`); коммиты на русском.
- Перед коммитом гонять `py -m py_compile` изменённых файлов. Тестов в репо всего два, оба —
  обычные скрипты (не pytest), сеть/LLM не нужны:
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
