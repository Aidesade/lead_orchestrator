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
set KIMI_MODEL_NAME=<ID модели K2.7 у провайдера>

# разовый прогон (всё готово, кроме endpoint — задай KIMI_* выше):
.venv_kimi\Scripts\python.exe onepager_kimi.py "АО «Рязаньавтодор»" \
    --industry "дорожное строительство" --out out.pdf
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
