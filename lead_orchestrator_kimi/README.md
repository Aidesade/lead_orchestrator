# lead_orchestrator_kimi — Kimi-агенты Фазы 2 и one-pager

Изолированная папка со своим venv содержит две части Kimi-ветки pipeline:

1. Во второй стадии после базового deep research запускаются пять специализированных ролей:
   `official_sources → (corporate_contour + secondary_sources) → role_candidates → candidate_contacts`.
2. Третий деливерабл — редакционный one-pager: Kimi выдаёт HTML, Playwright рендерит PDF.

Kimi CLI ходит на OpenAI-совместимый `/v1`; основной и Kimi venv не смешиваются.

## Файлы

| Файл | Роль | Статус |
|---|---|---|
| `onepager_system.py` | Системный промпт (VERBATIM) + токен фото спикера | — |
| `html_to_pdf.py` | Извлечение листа из ответа модели, встраивание фото (data-URI), рендер PDF через Playwright | ✅ **проверено вживую** (PDF рендерится, стиль верный) |
| `onepager_kimi.py` | Сборка контента из данных компании + вызов Kimi + склейка в PDF; CLI | сборка ✅ / Kimi API сверен вживую, импортится (после патча) — нужен endpoint |
| `research_enrichment_agent.py` | Dependency-aware runner пяти ролей, schema v3 runtime validation и атомарный checkpoint с evidence trace | ✅ offline + spec selftest |
| `kimi_agent/{official_sources,corporate_contour,secondary_sources,role_candidates,candidate_contacts}.{md,yaml}` | Контракты и tool-policy пяти ролей | ✅ загружаются Kimi CLI |
| `leadgen_tools.py` | Read-only LeadSearch/LeadFetch/LeadCrawl через основной venv | ✅ URL guard/selftest |
| `writer_kimi_agent.py` | Агентный писатель DOCX с динамическими scout/critic/verifier | ✅ selftest |
| `requirements.txt` | Лок окружения: пара SDK 0.0.5 + kimi-cli 1.12.0 и всё транзитивное | ✅ ставится без конфликтов |
| `patches/apply_patches.py` | Патчи `site-packages` — общий скрипт для Docker и локальной установки | ✅ идемпотентен, есть `--check` |
| `README.md` | Этот файл | — |

## Статус по частям (честно)

- **Рендер HTML→PDF (`html_to_pdf.py`)** — работает, проверен на мок-вёрстке: лист 800px,
  Source Serif 4 + Archivo, бордовый акцент, линейки, фото спикера вшивается как data-URI,
  высота PDF по факту контента. Playwright + chromium уже стоят (краул движка) — новых
  тяжёлых зависимостей нет.
- **Сборка контента (`build_user_content`)** — чистый Python, протестирована.
- **Вызов Kimi (`generate_onepager_html`)** — API **сверен вживую** на установленном
  `kimi-agent-sdk` **0.0.5**: `prompt()` (сигнатура, `yolo=True`, `final_message_only`) и
  извлечение текста `Message.extract_text()` — совпали, код под них поправлен. Модуль
  импортится в venv (после патча ниже). Нативного `system` в `prompt()` нет — системную
  инструкцию кладём префиксом в `user_input`. Для реального **прогона** не хватает только
  endpoint провайдера.
- **Пять enrichment-ролей** — scheduler, контракты, cache invalidation, source hierarchy и
  передача candidates→contacts проверены офлайн fake-SDK тестом. Каждый факт требует открытого
  **точного** URL (поисковый сниппет и произвольный path crawled-домена не доказательство),
  redirect хранит requested/final URL; `observed_at` ставит код. Schema v3 требует стабильные
  gap ID, связный корпоративный граф, покрытие всех 17 функций и каждого кандидата. Контакт несёт
  `contact_kind`, `source_context`, `best_use` и машинный `outreach_policy`; вакансии и агрегаторы
  нельзя повысить до прямого cold outreach. Критические таблицы
  корпоративного графа, кандидатов и каналов «Контакт / Тип / Лучшее применение» вставляются
  в DOCX детерминированно, а не оставляются на усмотрение писателя.

- **Аудит и безопасность инструментов** — per-role evidence trace входит в checkpoint и output hash,
  cache hit повторно проверяет exact URL/provenance. TTL считается от неизменяемого времени создания
  и не продлевается чтением. Kimi web tools работают только через HTTP crawler: соединение закреплено
  за заранее проверенным публичным IP с сохранением Host/TLS SNI, каждый redirect проверяется заново;
  browser crawl для model-controlled URL запрещён. Каждому параллельному Kimi-процессу выдаётся
  отдельный `KIMI_SHARE_DIR`, чтобы SDK не портил общий metadata-файл.

## ⚠️ Версии: пара SDK + kimi-cli закреплена жёстко

`kimi-agent-sdk` на PyPI застыл на **0.0.5** и объявляет **`kimi-cli>=1.12.0,<1.13.0`**. Эту
границу надо соблюдать: SDK зовёт `KimiCLI.create(..., skills_dir=...)` — имя параметра из
1.12.x. В `kimi-cli >=1.4x` параметр переименован в `skills_dirs` (список), и SDK 0.0.5 туда
уже не попадает — падает `TypeError` на **первом же вызове модели**.

Раньше в `.venv_kimi` стоял `kimi-cli 1.48.0` (подняли руками, за границей совместимости), а
рассинхрон закрывали вторым патчем прямо в `site-packages`. Сейчас закреплена пара, которую SDK
сам объявляет рабочей — **1.12.0**. Кастомный провайдер (`KIMI_BASE_URL` / `KIMI_API_KEY` /
`KIMI_MODEL_NAME`) она поддерживает точно так же, как 1.48.0 (сверено по исходникам обеих).

⚠️ **Не поднимать `kimi-cli`, не обновив `kimi-agent-sdk`.** `pip install --upgrade kimi-cli`
вернёт ту самую поломку. Все версии — в `requirements.txt`.

### Патч под Python 3.12 (нужен и на 1.12, и на 1.48)

`import kimi_agent_sdk` «из коробки» падает на Python 3.12: `TypeError: super(type, obj)` в
`kimi_cli/utils/slashcmd.py`. `SlashCommand` объявлен как `@dataclass(frozen=True, slots=True)`
**и** PEP 695-generic (`class SlashCommand[F]`), а инстанцируется параметризованно
`SlashCommand[F](...)` — `typing` пытается записать `__orig_class__` на frozen+slots-инстанс
(нет слота, нет права записи) → падение.

```python
cmd = SlashCommand[F](   # было — падает
cmd = SlashCommand(      # стало — тип-параметр в рантайме не нужен, инстанс идентичен
```

Применяется скриптом `patches/apply_patches.py` — **тем же самым, что зовёт Dockerfile**, чтобы
образ и локальная машина не разъезжались. Скрипт идемпотентен, а решение про `skills_dir` он
принимает **интроспекцией сигнатуры установленного `kimi-cli`**, а не по номеру версии: на
1.12.0 он честно сообщит, что правка не нужна, и не тронет рабочий вызов.

⚠️ Патчи живут в `.venv_kimi\Lib\site-packages\` и **не переживают переустановку пакетов** —
после любого `pip install` прогнать `apply_patches.py` заново.

## Установка (изолированный venv — НЕ глобально)

⚠️ Ставить kimi **только в отдельный venv**: глобально его зависимости конфликтуют с рабочим
пайплайном (kimi тянет `pydantic-core 2.41.5`, а `claude-agent-sdk`/`anthropic` требуют 2.46.4 —
глобальная установка **ломает** рабочий `lead_orchestrator/`; так и случилось при первой
установке — окружение восстановлено).

```
py -m venv .venv_kimi
.venv_kimi\Scripts\python.exe -m pip install -r requirements.txt
.venv_kimi\Scripts\python.exe patches\apply_patches.py     # ОБЯЗАТЕЛЬНО, иначе import упадёт
# chromium переиспользуется общий (ms-playwright): playwright тут той же версии 1.60.0,
# что и в основном venv, поэтому ревизия браузера совпадает и качать заново нечего.

# Проверить, что окружение согласовано (тот же вызов делает сборка Docker):
.venv_kimi\Scripts\python.exe patches\apply_patches.py --check

# Kimi CLI — OpenAI-совместимый endpoint (кастомный провайдер — родной режим, роутер не нужен):
set KIMI_API_KEY=<ключ провайдера>
set KIMI_BASE_URL=<endpoint /v1>
set KIMI_MODEL_NAME=kimi-k2.7-code

# разовый прогон (всё готово, кроме endpoint — задай KIMI_* выше):
.venv_kimi\Scripts\python.exe onepager_kimi.py "АО «Рязаньавтодор»" \
    --industry "дорожное строительство" --out out.pdf
```

Проверить только рендер (без Kimi) — скормить готовый HTML напрямую:

```
py html_to_pdf.py input.html output.pdf --photo ..\lead_orchestrator\assets\bulat_zamaliev.png
```

## Как это встроено в полный pipeline

При `orchestrator.py --model kimi` функция `_research_one` выполняет два DRE-прохода, затем
запускает `writer_kimi.run_research_subagents`, и только после полного валидного досье вызывает
писатель двух DOCX. Checkpoint хранится как `orq_cache/enrichment_<ИНН>.json`; cache hit всё равно
проходит проверку schema/prompt/input/dependency/output/evidence-trace hash и неизменяемого TTL.
One-pager вызывается следующим
подпроцессом через `make_onepager(...)`. Штатный CLI и веб теперь работают с
`ORQ_KIMI_ONLY=1`; legacy-ветка Claude недоступна без явного аварийного opt-out.

## Что переиспользуется из рабочего пайплайна

- Фото спикера — `..\lead_orchestrator\assets\bulat_zamaliev.png` (бинарник не дублируется).
- Логотип отправителя в этом дизайне — **текстовый словомарк** (не PNG), картинка не нужна.
