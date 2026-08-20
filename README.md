# lead_gen — оркестратор лидогенерации Telepatt / АО «ЦИТ РТ»

Детерминированный Python-пайплайн для B2B-агентства, продающего корпоративную on-premises
LLM-платформу **Telepatt** (RAG-база знаний, автономные ИИ-агенты, ИИ-Коуч «Наставник»):
**сбор крупных компаний по ОКВЭД и выручке → deep-research по каждой → два `.docx` + one-pager
`.pdf` → (ветка outreach) адресное письмо ЛПР с ящика `@tatar.ru` → лид в CRM**.

Оркестратор — код, а не LLM: модель принимает решения только внутри стадий (NL-контроллер,
экстракт ресёрча, enrichment-роли, писатель, текст письма), а порядок, ретраи, резюм и гарды
держит `orchestrator.py` / `outreach.py`. Python 3.12, Windows — основная платформа, прод — Docker.

## Архитектура

<!-- Схема намеренно лежит ОДНИМ блоком: кнопка «Copy» в правом верхнем углу забирает её целиком. -->

```text
                  ярлык new_orchestrator                       веб-интерфейс web/
                  run_orchestrator.cmd                         React/Vite → FastAPI (POST /api/runs)
                           │                                              │
                           ▼                                              │
                ┌─────────────────────────┐                               │
                │  orchestrator_agent.py  │  NL-контроллер (LLM):         │
                │  «нефтегаз, 50 компаний»│  текст → JSON-план → argv,    │
                │  → argv orchestrator.py │  подтверждение объёма         │
                └───────────┬─────────────┘                               │
                            │ subprocess                                  │ subprocess, stdout → SSE
                            ▼                                             ▼
   ┌───────────────────────────────────────────────────────────────────────────────────────────┐
   │              orchestrator.py — детерминированный asyncio-оркестратор (ФАЗА 1 + ФАЗА 2)    │
   │     порядок, ретраи, резюм, outbox, RAM-бэкпрешер — код; LLM работает только ВНУТРИ стадий│
   └────────────────────────────────────────────┬──────────────────────────────────────────────┘
                                                │
   ══════ ФАЗА 1 — сбор (_collect) ═════════════╪════════════════════════════════════════════
                                                ▼
   ┌────────────────────┐  LEAD_SOURCE  ┌────────────────────┐   ┌────────────────────┐
   │ rusprofile (дефолт)│               │ ofdata             │   │ checko             │
   │ Playwright + cookie│               │ REST API           │   │ REST API           │
   │ advanced-search по │               │ /search → /finances│   │ /search → ГИР БО   │
   │ ОКВЭД, серверный   │               │ → порог → /company │   │ → /company         │
   │ порог выручки,     │               │ (Docker: в образе  │   │ (второй откат)     │
   │ сорт. по убыванию, │               │ нет Chrome)        │   │                    │
   │ 1 карточка на лид  │               │                    │   │                    │
   └─────────┬──────────┘               └─────────┬──────────┘   └─────────┬──────────┘
             │  --state-owned: state_lead_collection → state_ownership      │
             │  выручка ≥2 млрд (2025) · госдоля ≥25% по PDF-выписке ЕГРЮЛ  │
             │  + XLSX Росимущества · юрадрес в STATE_LEAD_REGION           │
             └────────────────────────────┬─────────────────────────────────┘
                                          ▼
                            D:\лиды\leads_<отрасль>.json   (ORQ_LEADS_DIR)
                                          │
   ══════ ФАЗА 2 — ресёрч и материалы (_research_one_kimi, на компанию, --workers 2) ═════════
                                          ▼
   ┌────────────────────────────────────────────────────────────────────────────────────────────┐
   │ 1  deep_research_engine — ДВА прохода со своими aspects:  process │ roles                  │
   │    поиск brave→ddg→bing → Crawl4AI / HTTP → LLM-extract → completeness_critic → refill     │
   │    кэш находок  D:\orq_cache\findings_<ИНН>__<проход>.md  (TTL 72 ч, по файлу на проход)   │
   ├────────────────────────────────────────────────────────────────────────────────────────────┤
   │ 2  person_enrich  ФИО+ИНН → прямой рабочий контакт ЛПР        ┐  детерминированно,         │
   │ 3  email_guess    домен → схема локал-парта → гипотезы почты  ┘  без LLM, вне SDK-сессии   │
   ├────────────────────────────────────────────────────────────────────────────────────────────┤
   │ 4  research_enrichment_agent — ПЯТЬ ролей (подпроцесс; validation + атомарный checkpoint)  │
   │    official_sources → (corporate_contour + secondary_sources)                              │
   │                     → role_candidates → candidate_contacts        сбой роли ≠ сбой компании│
   ├────────────────────────────────────────────────────────────────────────────────────────────┤
   │ 5  writer_kimi → writer_kimi_agent ×2  (главный писатель + scout/verifier + critic)        │
   │    → company_research_agent: схемы, системные промпты, DOCX-рендереры                      │
   ├────────────────────────────────────────────────────────────────────────────────────────────┤
   │ 6  _onepager_one → onepager_kimi (подпроцесс) → HTML → Playwright → PDF  (канон 1-3,6,8)   │
   └────────────────────────────────────────────┬───────────────────────────────────────────────┘
                                                │  3 файла: карта_процессов.docx ·
                                                │  карта_ролей.docx · презентация_Telepatt.pdf
                                                ▼
             ┌─────────────────────────────┐         ┌──────────────────────────────┐
             │ ORQ_STORE=local  (дефолт)   │         │ ORQ_STORE=disk               │
             │ deliverables/<ИНН>/         │         │ Яндекс Диск: _upload_verified│
             │ → веб: GET /api/leads/{инн} │         │ (перечитка папки), outbox,   │
             │   /file/{slug}              │         │ резюм по размерам файлов     │
             └─────────────────────────────┘         └──────────────────────────────┘

   LLM-runtime (ORQ_LLM_RUNTIME):
     claude ─ Claude Agent SDK через lead_orchestrator_kimi/claude_kimi_adapter (тот же prompt(),
              MCP-тулы Lead*), агенты стадий 4-6 запускаются ОСНОВНЫМ python
     kimi   ─ Kimi K2.7 через шлюз (AsyncOpenAI); агенты стадий 4-6 — в изолированном .venv_kimi
              (Docker живёт так: ORQ_KIMI_ONLY=1)

   ══════ OUTREACH — рассылка ЛПР (outreach.py · run_outreach.cmd) ════════════════════════════
   модель работает ровно в одном месте — текст письма; стадии, адреса и подпись — код

   1 реестр ─▶ 2 RusProfile ─▶ 3 one-pager ──▶ 5 прекондишен ─▶ 6 адрес ЛПР ──▶ 7 письмо
   по ИНН      card_facts      assets/onepagers  ящик @tatar.ru   email_guess:     outreach_letter
   (дедуп,     parse_founders  или --generate-   ДО первой        гипотезы БЕЗ     (LLM, только
   бэкфилл)    (ЛПР бесплатен,  onepager         компании         пробивки ящика   проверенные факты)
               контакты — ₽)                                      + копия на       + подпись/отписка
                                                                  общий ящик       КОДОМ
                                                                                        │
        ┌───────────────────────────────────────────────────────────────────────────────┘
        ▼
   outlook_send (Outlook Desktop, COM) ── Save() в Черновики ПО УМОЛЧАНИЮ │ Send() ТОЛЬКО при --send
        │                                 (+ --limit, --pace 30 с; лаунчер требует набрать SEND)
        ├─▶ 8  outreach.py --check → outreach_registry.audit()   кому писали, где застряли
        └─▶ 9  crm_push → POST <CRM_URL>/api/leads/ingest         мягко и ТОЛЬКО после отправки:
                                                                  сбой CRM не меняет статус письма
```

## Быстрый старт

```sh
py -m pip install -r requirements.txt        # основное окружение + web/api
py -m playwright install chromium            # RusProfile, Crawl4AI, рендер PDF
copy .env.example env\.env                   # заполнить нужное режиму; папка env/ в .gitignore
cd lead_orchestrator
py rusprofile_session.py --login             # разовый логин RusProfile → env/rusprofile_cookies.json

py orchestrator.py mining --count 10                      # сбор + ресёрч + 3 файла
py orchestrator.py --state-owned --count 5                # строгий добор госкомпаний Татарстана
py orchestrator.py "D:\лиды\leads_mining.json" --dry-run --no-upload   # вся цепочка офлайн
py outreach.py "D:\лиды\leads_mining.json" --dry-run      # outreach офлайн, без писем
py outreach.py --check                                    # кому писали, где застряли
```

Kimi-runtime (`ORQ_LLM_RUNTIME=kimi` / Docker) требует отдельный venv — см. шапку
`lead_orchestrator_kimi/requirements.txt`. Docker: `docker compose build && docker compose run --rm lead-orchestrator mining --count 10`.

## Структура

| Папка / файл | Что там |
|---|---|
| `lead_orchestrator/` | ядро: `orchestrator.py` (ФАЗА 1+2), `outreach.py` (рассылка), источники, движок ресёрча, писатель, тесты-скрипты |
| `lead_orchestrator_kimi/` | агенты ФАЗЫ 2 для обоих runtime: пять ролей, писатель документа, `claude_kimi_adapter`, one-pager |
| `web/` | FastAPI + React: спавнит `orchestrator.py`, парсит stdout в SSE, отдаёт деливераблы |
| `Dockerfile`, `docker-compose.yml` | образ с двумя venv; сборка = build-gate из тестов |
| `.env.example` | все переменные окружения с дефолтами → копировать в `env/.env` |
| `requirements.txt` | агрегатор: лок `lead_orchestrator/` + `web/api/` |

## Документация

- [`CLAUDE.md`](CLAUDE.md) — полное описание пайплайна, инварианты, тумблеры, проверка правок (авторитетный источник).
- [`lead_orchestrator_kimi/CLAUDE.md`](lead_orchestrator_kimi/CLAUDE.md) — venv Kimi, патчи, канон one-pager, реальный API Kimi SDK.
- [`web/README.md`](web/README.md) — ручки API и решения веба.
- [`DEPLOY_TIMEWEB.md`](DEPLOY_TIMEWEB.md) — прод в Docker; [`DEPLOY_VERIFIER.md`](DEPLOY_VERIFIER.md) — хост email-верификатора.
- [`AUDIT_KIMI.md`](AUDIT_KIMI.md) — аудит Kimi-стадии.
