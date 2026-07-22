# lead_orchestrator

Оркестратор полной цепочки лидогенерации для агентства «ИИ для бизнеса»: **сбор крупного бизнеса по ОКВЭД+выручке (OfData API) → ресёрч по каждой компании (Kimi K2.7 + WebSearch) → раскладка досье/стратегии по Яндекс Диску**. Одна команда запускает обе фазы.

Собрано из рабочего скилла `lead-finder` (`~/.claude/skills/lead-finder/scripts/`) — только модули, относящиеся к оркестратору. B2C-ветка (2ГИС/Яндекс-карты, `run.py` и т.п.), тестовые данные, Chrome-профиль и секреты намеренно не включены.

## Архитектура

**Фаза 1 — сбор** (`orchestrator._collect`):
`source_ofdata.harvest` (`/search` по основному ОКВЭД и региону → `/finances`, а на бесплатном тарифе автоматический fallback в ГИР БО ФНС → строгий фильтр выручки **≥ 1 млрд ₽**) → `source_ofdata.ofdata_contacts_pass` (`/company`: сайт, телефон, email, руководитель только для прошедших фильтр) → `pipeline._select` → JSON → `disk_organize.organize_to_disk`. `LEAD_SOURCE=checko` и `LEAD_SOURCE=rusprofile` сохранены как явные пути отката.

**Фаза 2 — deep research + два DOCX** (`asyncio.gather` + `Semaphore`):
в Kimi-ветке (`--model kimi`) после двух базовых DRE-проходов запускается граф пяти ролей
`official_sources → (corporate_contour + secondary_sources) → role_candidates → candidate_contacts`.
Результаты проходят машинную валидацию, checkpoint и детерминированно попадают в карту ролей:
корпоративный граф, центры решений, кандидаты и таблица «Контакт / Тип / Лучшее применение».
Контракт schema v3 требует точные фактически открытые URL, стабильные ID официальных пробелов,
покрытие всех 17 функций и каждого кандидата; контакт отдельно несёт тип источника и разрешённую
политику outreach. Evidence trace сохраняется вместе с checkpoint и повторно валидируется на cache hit.
Штатный runtime — **Kimi-only** (`ORQ_KIMI_ONLY=1`): Claude/Anthropic не выбирается ни CLI,
ни вебом. Старый код ветки сохранён только как аварийный rollback при явном `ORQ_KIMI_ONLY=0`.

## Файлы

| Модуль | Роль |
|---|---|
| `orchestrator.py` | **точка входа CLI** — обе фазы одной командой |
| `orchestrator_agent.py` | Kimi K2.7 NL-контроллер над `orchestrator.py` (JSON-план → подпроцесс) |
| `kimi_config.py` | единый ключ, endpoint, модель и Kimi-only guard |
| `company_research_agent.py` | общие схемы/промпты/DOCX-рендереры; старый standalone Claude-agent заблокирован при `ORQ_KIMI_ONLY=1` |
| `writer_kimi.py` | Kimi-писатель + parent-мост пяти enrichment-ролей + DOCX-adapter |
| `kimi_research_cli.py` | безопасные read-only LeadSearch/LeadFetch/LeadCrawl для Kimi |
| `test_research_enrichment.py` | офлайн-тест scheduler, контрактов, кэша и DOCX-adapter |
| `source_ofdata.py` | основной API-источник Фазы 1: `/search` → `/finances` → порог ≥ 1 млрд → `/company` |
| `test_source_ofdata.py` | офлайн-тест схем OfData, порога, контактов и безопасной передачи ключа |
| `source_rusprofile.py` | поиск RusProfile по ОКВЭД+выручке (uc); карта отраслей `INDUSTRY` (21 отрасль) |
| `rusprofile_session.py` | сессия платного аккаунта RusProfile, контакты с карточек |
| `browser_util.py` | `chrome_major()` — подбор ChromeDriver под установленный Chrome |
| `pipeline.py` | сборка/отбор лидов (`_select`), полная цепочка RusProfile→ГИР БО→Checko |
| `build_excel.py` | сборка xlsx по шаблону `D:\лиды` |
| `revenue_enrich.py` | выручка из ГИР БО ФНС (бесплатно, без ключа) |
| `dadata_enrich.py` | Dadata `findById` → ЛПР/реквизиты |
| `checko_enrich.py` | Checko: контакты по ИНН (free 100/день) |
| `inn_util.py` | валидация/поиск ИНН/ОГРН |
| `site_verify.py`, `harvest_inn_site.py`, `webutil.py`, `email_finder.py` | контакт-верификация и сбор email с сайтов (зависимости `pipeline`) |
| `disk_organize.py` | раскладка по Яндекс Диску через CLI `yacli` |
| `run_kimi_orchestrator.cmd` | desktop-лаунчер Kimi + OfData (двойной клик / passthrough) |

## Запуск

```sh
# локально создать D:\lead_gen\env\.env (вся папка env исключена из Git):
# KIMI_API_KEY=ваш-kimi-ключ
# OFDATA_API_KEY=ваш-ключ

# сбор + ресёрч по отрасли (Kimi K2.7 — дефолт)
py orchestrator.py --industries mining --count 10

# только ресёрч по готовому JSON
py orchestrator.py path/to/leads.json

# через Kimi K2.7 controller (естественный язык)
py orchestrator_agent.py "собери 10 по mining, dry-run"
```

Полезные флаги: `--min-revenue 1e9`, `--region "ХМАО"`, `--workers N`, `--dry-run`, `--no-upload`, `--show-browser`.

## Зависимости и окружение

См. `requirements.txt`. Для штатного пути нужны `KIMI_API_KEY` (либо fallback
`GPLLM_API_KEY`) и `OFDATA_API_KEY` из `env/.env`. Платный `/finances` используется напрямую;
на бесплатном тарифе выручка автоматически добирается из официального ГИР БО ФНС.
`DADATA_TOKEN` и `CHECKO_TOKEN` опциональны, Checko/RusProfile используются только при явном
переключении `LEAD_SOURCE`.

`OFDATA_REVENUE_SOURCE=auto` — дефолт: платный `/finances`, а при бесплатном тарифе с балансом
0 ₽ — автоматический переход на официальный ГИР БО ФНС. Значения `ofdata` и `girbo` закрепляют
источник явно. Для просмотра результата smoke без записи файла: `source_ofdata.py ... --print-json`.

Единый и зафиксированный ID всех модельных стадий: `KIMI_MODEL_NAME=kimi-k2.7-code`.
Попытка подменить его в Kimi-only режиме завершает запуск до API-вызова. Проверка маршрута без
сети: `py test_kimi_only.py`.

OfData фильтрует включённый регион серверно по двухзначному коду. Отрицательный фильтр
(`НЕ Москва`) применяется клиентски и поэтому требует больше API-запросов.

> Подробная история решений и неочевидные тонкости — в памяти проекта `lead-finder`.
