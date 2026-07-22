# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Изолированные **Kimi-агенты Фазы 2** лидген-пайплайна. Папка начиналась с третьей стадии
(редакционный one-pager: Kimi выдаёт самодостаточный HTML → Playwright рендерит PDF), а теперь
также содержит dependency-aware enrichment и агентного писателя двух `.docx`. Python 3.12,
LLM — через **Kimi Agent SDK** / Kimi CLI и OpenAI-совместимый `/v1`. Подробности для человека —
в `README.md`; этот файл — про инварианты и подводные камни.

**Статус: Kimi включён end-to-end** (2026-07-21): controller → research extract → enrichment →
два `.docx` → HTML/PDF. One-pager отдельно проверен вживую 2026-07-13.
Пробный результат: `disk:/Лиды/_kimi_test/АО Рязаньавтодор/` (тестовая папка, вне боевого дерева лидов).

## Главные инварианты (НЕ сломать)

1. **Интеграционный контракт с `../lead_orchestrator/` уже боевой.** Основной оркестратор не
   импортирует Kimi SDK напрямую: он вызывает скрипты этой папки её venv-питоном. Меняя CLI,
   схемы JSON или коды возврата, одновременно проверять parent-мост и `test_kimi_only.py`.
2. **Kimi ставится ТОЛЬКО в `.venv_kimi`, никогда не глобально.** Глобальная установка ломает
   рабочий пайплайн: `kimi-agent-sdk` тянет `pydantic-core 2.41.5`, а `claude-agent-sdk`/`anthropic`
   требуют `2.46.4` — конфликт версий. (Это уже случалось: глобальная установка снесла `anthropic`
   и понизила `pydantic-core`; окружение пришлось восстанавливать вручную.)
3. **Запускать venv-питоном:** `.venv_kimi\Scripts\python.exe`, не глобальным `py`. Глобальный `py`
   не видит `kimi_agent_sdk`; venv не видит рабочих модулей — это норма, окружения раздельны.
   ⚠️ Докстринги внутри `onepager_kimi.py` местами говорят `py onepager_kimi.py` — устарели,
   авторитет здесь.
4. **Патчи `.venv_kimi` управляются только `patches/apply_patches.py`.** На текущем пине нужен
   `slashcmd.py`; `_session.py` был нужен лишь для несовместимого kimi-cli 1.48. После любой
   переустановки обязательны `apply_patches.py` и затем `--check`.
5. **Канон блоков листа.** Блоки 1–3, 6, 8 (шапка, герой, 4 фичи, спикер, футер) — ОДИНАКОВЫ у всех
   заказчиков и передаются модели ДОСЛОВНО из констант `onepager_kimi.py`. Уникален только блок 4
   («Одна проблема — одно решение») + подписи метрик. Иначе модель каждый прогон переписывает шапку
   заново и ВЫДУМЫВАЕТ регалии живому человеку (так и было в первых прогонах).

## Карта файлов

| Файл | Роль | Статус |
|---|---|---|
| `onepager_system.py` | Системный промпт one-pager (VERBATIM, прислан пользователем) + `SPEAKER_PHOTO_TOKEN` | не менять текст промпта осознанно |
| `html_to_pdf.py` | Ответ модели → извлечь самодостаточный лист (баланс `<div>`), вшить фото data-URI, Playwright → PDF | ✅ проверено вживую |
| `onepager_kimi.py` | Канон-константы + `build_user_content` + `generate_onepager_html` (вызов Kimi) + `make_onepager` (полная стадия) + CLI | ✅ проверено вживую |
| `kimi_agent/onepager.yaml` + `system.md` | Агент one-pager **без тулов** (обязателен, см. ниже) | ✅ |
| `writer_kimi_agent.py` | Агентный писатель ОДНОГО JSON-документа; прямой read-only веб + нативный Task → динамические scout/critic/verifier | ✅ selftest |
| `research_enrichment_agent.py` | Пять dependency-aware enrichment-субагентов Фазы 2: `official → (contour + secondary) → roles → contacts`; schema v3, exact-URL/redirect provenance, coverage/outreach contracts, persisted evidence trace, schema/prompt/input/dependency/output hashes и non-sliding TTL | ✅ офлайн-контракт + selftest |
| `leadgen_tools.py` | Read-only мост LeadSearch/LeadFetch/LeadCrawl в основной venv; HTTP(S)-only, private/loopback/link-local URL и небезопасные redirect блокируются в `deep_research_engine` | ✅ selftest |
| `kimi_agent/writer.yaml`, `scout.yaml`, `critic.yaml`, `verifier.yaml` | Фиксированные роли Kimi CLI 1.12 | ✅ selftest |
| `README.md` | Человеко-документация: статус, установка, патчи | — |
| `.venv_kimi/` | Изолированное окружение (kimi-agent-sdk 0.0.5 + kimi-cli 1.12.0 + playwright). Пин 1.12.0 не снимать. | — |

## Реальный API Kimi (сверено вживую на 0.0.5)

- `kimi_agent_sdk.prompt(user_input, *, model, thinking, yolo, final_message_only, agent_file,
  mcp_configs, skills_dir, approval_handler_fn, ...)` — async-генератор `Message`.
- **Обязательно** `yolo=True` (или `approval_handler_fn`) — иначе `PromptValidationError`.
- Текст из `Message` — методом **`message.extract_text()`** (НЕ `.text`/`.content`).
- **Нативного `system=` нет** — системный промпт кладётся префиксом в `user_input`.
- **Обязателен `agent_file`** с агентом без тулов — см. подводные камни.

## Запуск

```
# один раз: окружение (venv + ДВА патча, см. «Подводные камни»)
py -m venv .venv_kimi
.venv_kimi\Scripts\python.exe -m pip install kimi-agent-sdk playwright

# endpoint провайдера (Kimi CLI — OpenAI-совместимый /v1; все три env читает САМ kimi-cli,
# наш код берёт из env только KIMI_MODEL_NAME как дефолт для --model):
set KIMI_API_KEY=%GPLLM_API_KEY%
set KIMI_BASE_URL=https://gpllmkeeper.dtc.tatar/v1
set KIMI_MODEL_NAME=kimi-k2.7-code

# полный прогон одной компании (проверено):
.venv_kimi\Scripts\python.exe onepager_kimi.py "АО «Рязаньавтодор»" --industry "дороги" --out out.pdf
# --html-out raw.html — сохранить сырой HTML модели: перерендер вёрстки БЕЗ повторного платного вызова
# --pain "..."        — боль из ресёрча (иначе модель оценит по отрасли)

# только рендер, БЕЗ Kimi (проверяемо без endpoint) — глобальным py тоже можно:
py html_to_pdf.py raw.html out.pdf --photo ..\lead_orchestrator\assets\bulat_zamaliev.png
```

**Проверка:** `py -m py_compile onepager_kimi.py html_to_pdf.py onepager_system.py
writer_kimi_agent.py research_enrichment_agent.py leadgen_tools.py` +
`.venv_kimi\Scripts\python.exe writer_kimi_agent.py --selftest` +
`.venv_kimi\Scripts\python.exe research_enrichment_agent.py --selftest` +
`py ..\lead_orchestrator\test_research_enrichment.py` +
`py ..\lead_orchestrator\test_kimi_only.py`. Для one-pager дополнительно
прогон рендера на сохранённом `raw.html`; страницы PDF — `pdfinfo` из Poppler.

## Подводные камни

- **Патч 1 — `slashcmd.py` (баг kimi-cli под Python 3.12).** «Из коробки» `import kimi_agent_sdk`
  падает: `TypeError: super(type, obj)` в `kimi_cli/utils/slashcmd.py`. Причина — `SlashCommand`
  объявлен `@dataclass(frozen=True, slots=True)` И PEP 695-generic (`class SlashCommand[F]`),
  а инстанцируется параметризованно `SlashCommand[F](...)` → `typing` пытается записать
  `__orig_class__` на frozen+slots-объект. Фикс в методе `_register`: `SlashCommand[F](` →
  `SlashCommand(`. Воспроизводится и на kimi-cli 1.12, и на 1.48.
- **Патч 2 для `_session.py` при текущем пине НЕ нужен.** Он существовал только из-за ручной
  установки kimi-cli 1.48 вне диапазона SDK. `apply_patches.py --check` обязан подтвердить
  `skills_dir`; не поднимать kimi-cli отдельно от SDK.
- ⚠️ Патч slashcmd живёт в `.venv_kimi\Lib\site-packages\` и **НЕ переживёт переустановку** —
  после неё всегда запускать `patches/apply_patches.py`, не править site-packages руками.
- **One-pager остаётся агентом без тулов.** Его `onepager.yaml` содержит `tools: []` намеренно.
  Писатель документов использует другой `writer.yaml`: главный агент и scout/verifier имеют
  read-only `LeadSearch`/`LeadFetch`/`LeadCrawl`, главный дополнительно имеет `Task`. Никаких
  shell/file tools; список сайтов не зашит. Число scout выбирает модель, critic обязателен.
- **kimi-agent-sdk на PyPI застыл на 0.0.5** (github-доки с 0.0.6 опережают релиз).
- **Одна страница PDF — следить.** Высота листа дробная (напр. 1953.98 px), а размер страницы
  задаётся целым; при конвертации px→дюймы Chromium теряет доли, контент вылезает на микроскопическую
  величину, и неразрывный блок футера уезжает на 2-ю (пустую) страницу. Лечится запасом
  `_PAGE_SLACK_PX`=2 в `render_pdf` (в PDF не виден) + ожиданием `img.decode()` перед замером высоты.
  После правок вёрстки ПРОВЕРЯТЬ `pdfinfo | grep Pages` = 1.
- **`width:800px` — связь промпта и рендерера.** `html_to_pdf._extract_sheet` находит лист поиском
  ЛИТЕРАЛЬНОЙ строки `width:800px`, а она задана в тексте `onepager_system.py`. Поменяешь ширину в
  промпте — извлечение молча свалится в фолбэк (весь ответ целиком), и PDF выйдет кривым, но БЕЗ ошибки.
- **Рендер требует сети:** шрифты (Source Serif 4 + Archivo) тянутся с fonts.googleapis.com; офлайн
  PDF соберётся, но подставятся дефолтные шрифты — дизайн тихо деградирует.
- **HTML-каркас `x-dc`/`support.js` не самодостаточен** — модель по промпту выдаёт обёртку с
  `<script src="./support.js">`; рендерер вынимает внутренний лист, отбрасывая обёртку.
- **Фото спикера** — модель ставит `<img src="SPEAKER_PHOTO">`, рендерер подменяет токен на data-URI
  из `../lead_orchestrator/assets/bulat_zamaliev.png` (бинарник не дублируется). Логотип отправителя
  в этом дизайне — текстовый словомарк, PNG не нужен.
- **Playwright/chromium:** playwright ставится в venv, но браузер **переиспользуется глобальный**
  (общий каталог `ms-playwright`). `render_pdf` импортит playwright ЛЕНИВО — `import html_to_pdf`
  не требует playwright.
- **Факты о людях/реквизиты/контакты — не отдавать модели на откуп.** Регалии Замалиева
  (`SPEAKER_FACTS`), реквизиты ЦИТ РТ (`FOOTER`: ИНН 1655505808, ОГРН 1241600056829, «АК БАРС» Банк)
  и контакты (`VENDOR_PHONE` + `VENDOR_CONTACT_PERSON` «Шабанов Али Магомедович» строкой ПОД
  телефоном тем же шрифтом, `VENDOR_EMAIL`, `VENDOR_SITE`) захардкожены; модели запрещено
  добавлять свои — она их выдумывала.
- **Вёрстка полосы метрик — три правила, добытые правкой боевых листов** (все в
  `build_user_content`, блок 5; системный промпт VERBATIM не трогаем):
  `padding:28px 56px` у самой полосы (иначе первая ячейка «≈8×» уезжает ЗА КРАЙ листа);
  `white-space:nowrap` у значений (иначе «On-prem» рвётся на «On-» / «prem»);
  `padding-right:20px` у ячеек (иначе «Секунды» упираются в линейку-делитель).
  Модель нарушает их регулярно — при правках блока 5 проверять рендер глазами.
- **Оговорка про Булата Замалиева** (та же, что в рабочем пайплайне): публично подтверждён как
  «Уполномоченный по технологиям ИИ при Минцифры РТ»; связь с ЦИТ РТ как «руководителя направления»
  публично НЕ подтверждена — не утверждать (см. `SPEAKER_ROLE`).
- **Контакты CTA — ОТПРАВИТЕЛЯ, не лида.** В блок «назначьте демо» идут `VENDOR_PHONE`/`VENDOR_EMAIL`/
  `VENDOR_SITE` (ЦИТ РТ). Если при встраивании в оркестратор передать туда контакты лида из Фазы 1,
  заказчика позовут звонить самому себе (уже ловили).

## Встроено в боевой пайплайн (обновлено 2026-07-21) — ГОТОВО

Kimi-ветка заменила модельный runtime всей Фазы 2: enrichment, писатель двух `.docx` и
one-pager. Прежняя `.pptx`-презентация также заменена PDF; её код, LibreOffice/node и скилл
`pptx` из штатного маршрута удалены.

Как именно сведены два SDK: оркестратор НЕ импортирует `kimi_agent_sdk`, а запускает
**подпроцесс venv-питона этой папки** — `orchestrator._onepager_one()`:
`.venv_kimi\Scripts\python.exe onepager_kimi.py <компания> --industry … --pain … --out <p.pdf>`,
`cwd` = эта папка, env = `KIMI_API_KEY` (фолбэк `GPLLM_API_KEY`) + `KIMI_BASE_URL` + `KIMI_MODEL_NAME`
(у последних двух в оркестраторе есть рабочие дефолты). Так изоляция окружений сохраняется:
инвариант 2 не нарушен, глобальный `pydantic-core` не трогается.

Что это значит при правках здесь:
- **CLI `onepager_kimi.py` — контракт с оркестратором.** Переименуешь флаги/аргументы или сменишь
  код возврата — сломается боевая стадия, а не только ручной запуск. Оркестратор ждёт: exit 0 и
  файл по `--out` (>5 КБ; резюм засчитывает one-pager только при >60 КБ — `REAL_PDF_MIN`).
- Боль приходит из находок движка (`_main_pain`) в `--pain`; отрасль — из папки Диска.
- Контакты CTA оркестратор НЕ передаёт (берутся дефолты `VENDOR_*`) — это осознанно, см. выше.
- Таймаут/параллелизм стадии — на стороне оркестратора: `ORQ_ONEPAGER_TIMEOUT` (900с),
  `ORQ_ONEPAGER_CONCURRENCY` (2). Сбой стадии компанию НЕ валит: два `.docx` уже готовы.
- Заготовка третьего файла (Фаза 1 / dry-run) — `disk_organize._make_pdf`, голый stdlib-PDF.
