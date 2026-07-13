# lead_orchestrator_kimi — третий деливерабл на Kimi Agent SDK (one-pager HTML → PDF)

Изолированная папка: **рабочий `lead_orchestrator/` не тронут.** Здесь — новая версия
ТРЕТЬЕЙ стадии Фазы 2: вместо брендированной `.pptx` (Claude Agent SDK + скилл pptx)
делается **редакционный one-pager**: модель по системному промпту выдаёт самодостаточный
HTML, который рендерится в **PDF**. LLM — через **Kimi Agent SDK** (Kimi CLI ходит на
OpenAI-совместимый `/v1`, поэтому кастомный провайдер — его родной режим, роутер не нужен).

## Файлы

| Файл | Роль | Статус |
|---|---|---|
| `onepager_system.py` | Системный промпт (VERBATIM) + токен фото спикера | — |
| `html_to_pdf.py` | Извлечение листа из ответа модели, встраивание фото (data-URI), рендер PDF через Playwright | ✅ **проверено вживую** (PDF рендерится, стиль верный) |
| `onepager_kimi.py` | Сборка контента из данных компании + вызов Kimi + склейка в PDF; CLI | сборка ✅ / Kimi API сверен вживую, импортится (после патча) — нужен endpoint |
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

## ⚠️ Баг kimi-cli под Python 3.12 — обойдён локальным патчем (применён)

`kimi-agent-sdk` на PyPI застыл на **0.0.5**; «из коробки» `import kimi_agent_sdk` падал на
Python 3.12: `TypeError: super(type, obj)` в `kimi_cli/utils/slashcmd.py`. Причина —
`SlashCommand` объявлен как `@dataclass(frozen=True, slots=True)` **и** PEP 695-generic
(`class SlashCommand[F]`), а инстанцировался параметризованно `SlashCommand[F](...)`. Это
заставляет `typing` записать `__orig_class__` на frozen+slots-инстанс (нет слота, нет права
записи) → падение. Воспроизводится и на `kimi-cli` 1.12, и на 1.48.

**Патч (уже применён в `.venv_kimi`):** в `slashcmd.py`, метод `_register`, убрана
параметризация в вызове:

```python
cmd = SlashCommand[F](   # было — падает
cmd = SlashCommand(      # стало — тип-параметр в рантайме не нужен, инстанс идентичен
```

⚠️ Патч живёт в `.venv_kimi\Lib\site-packages\` и **не переживёт переустановку `kimi-cli`** —
после `pip install --upgrade kimi-cli` повторить. После патча `import` и весь API работают
(проверено: `prompt`/`Session`/`Message.extract_text` доступны, рендер PDF из venv идёт).

## Установка (изолированный venv — НЕ глобально)

⚠️ Ставить kimi **только в отдельный venv**: глобально его зависимости конфликтуют с рабочим
пайплайном (kimi тянет `pydantic-core 2.41.5`, а `claude-agent-sdk`/`anthropic` требуют 2.46.4 —
глобальная установка **ломает** рабочий `lead_orchestrator/`; так и случилось при первой
установке — окружение восстановлено).

```
py -m venv .venv_kimi
.venv_kimi\Scripts\python.exe -m pip install kimi-agent-sdk playwright
# chromium переиспользуется глобальный (общий ms-playwright), качать заново не нужно.
# ЗАТЕМ применить патч slashcmd.py (см. выше) — иначе import упадёт.

# Kimi CLI — OpenAI-совместимый endpoint (кастомный провайдер — родной режим, роутер не нужен):
set KIMI_API_KEY=<ключ провайдера>
set KIMI_BASE_URL=<endpoint /v1>
set KIMI_MODEL_NAME=<ID модели K2.7 у провайдера>

# разовый прогон (всё готово, кроме endpoint — задай KIMI_* выше):
.venv_kimi\Scripts\python.exe onepager_kimi.py "АО «Рязаньавтодор»" \
    --industry "дорожное строительство" --website avtodor-rzn.ru --out out.pdf
```

Проверить только рендер (без Kimi) — скормить готовый HTML напрямую:

```
py html_to_pdf.py input.html output.pdf --photo ..\lead_orchestrator\assets\bulat_zamaliev.png
```

## Как это встроить в полный пайплайн (следующий шаг)

Стадия самодостаточна: `make_onepager(company_name, out_pdf, industry, pain, contacts)`.
В боевом оркестраторе она заменит `_presentation_one`: на вход — `lead` + находки движка
(боль через `_main_pain`), на выход — `.pdf` в папку компании (имя третьего файла:
`<Компания>_презентация_Telepatt.pdf`), гард по размеру и заливка — как у остальных.
Перенос ВСЕГО пайплайна (обёртка, писатель, движок) на Kimi Agent SDK — отдельная работа;
здесь сделана только третья стадия.

## Что переиспользуется из рабочего пайплайна

- Фото спикера — `..\lead_orchestrator\assets\bulat_zamaliev.png` (бинарник не дублируется).
- Логотип отправителя в этом дизайне — **текстовый словомарк** (не PNG), картинка не нужна.
