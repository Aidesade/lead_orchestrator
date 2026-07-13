# lead_orchestrator

Оркестратор полной цепочки лидогенерации для агентства «ИИ для бизнеса»: **сбор крупного бизнеса по ОКВЭД+выручке (RusProfile) → ресёрч по каждой компании (LLM + WebSearch) → раскладка досье/стратегии по Яндекс Диску**. Одна команда запускает обе фазы.

Собрано из рабочего скилла `lead-finder` (`~/.claude/skills/lead-finder/scripts/`) — только модули, относящиеся к оркестратору. B2C-ветка (2ГИС/Яндекс-карты, `run.py` и т.п.), тестовые данные, Chrome-профиль и секреты намеренно не включены.

## Архитектура

**Фаза 1 — сбор** (`orchestrator._collect`):
`source_rusprofile.harvest` (поиск по ОКВЭД+выручке через undetected-chromedriver, обход Cloudflare) → `rusprofile_session.RusProfileAuth.enrich_leads` (контакты с карточек платного аккаунта; cookie в `.rp_cookies.json`, разовый `--login`) → `pipeline._select` (отбор) → `build_excel.build` (xlsx) → `disk_organize.organize_to_disk` (папки + заготовки .docx на Диске).

**Фаза 2 — ресёрч** (детерминированный async, `asyncio.gather` + `Semaphore`):
по каждой компании `company_research_agent` (Claude Agent SDK, opus/sonnet + WebSearch/WebFetch) собирает досье и стратегию, перезаписывает заготовки в тех же папках на Диске. Официальная база — `deep_research`: ГИР БО (`revenue_enrich.girbo_revenue`, бесплатно) + Dadata (`dadata_enrich`) + Checko (`checko_enrich`).

## Файлы

| Модуль | Роль |
|---|---|
| `orchestrator.py` | **точка входа CLI** — обе фазы одной командой |
| `orchestrator_agent.py` | SDK-обёртка над `orchestrator.py` (NL-запрос → подпроцесс) |
| `company_research_agent.py` | агент-досье по одной компании (фаза 2) |
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
| `run_orchestrator.cmd` | desktop-лаунчер (двойной клик / passthrough) |

## Запуск

```sh
# сбор + ресёрч по отрасли (по умолчанию count=200; ~$1–2/компания)
py orchestrator.py --industries mining --count 10

# только ресёрч по готовому JSON
py orchestrator.py path/to/leads.json

# через SDK-агента (естественный язык)
py orchestrator_agent.py "собери 10 по mining, dry-run"
```

Полезные флаги: `--min-revenue 1e9`, `--region "ХМАО"`, `--model opus|sonnet`, `--workers N`, `--dry-run`, `--no-upload`, `--show-browser`.

## Зависимости и окружение

См. `requirements.txt`. Кроме pip-пакетов нужны: реальный Chrome, CLI `yacli` (Яндекс Диск, разовый `yacli login disk`), переменные `ANTHROPIC_API_KEY` (фаза research) и опционально `DADATA_TOKEN` / `CHECKO_TOKEN`.

Регион у RusProfile фильтруется **только клиентски** (серверного фильтра нет). Сбор идёт в **headed offscreen** по умолчанию — true-headless не проходит антибот Cloudflare.

> Подробная история решений и неочевидные тонкости — в памяти проекта `lead-finder`.
