# -*- coding: utf-8 -*-
r"""
Третий деливерабл на Kimi Agent SDK: редакционный one-pager (HTML → PDF).

Заменяет прежнюю .pptx-стадию (Claude Agent SDK + скилл pptx). Здесь модель Kimi по
ONEPAGER_SYSTEM генерит самодостаточный HTML, а html_to_pdf.py рендерит его в PDF.

⚠️  СТАТУС ЧАСТЕЙ:
  - build_user_content / сборка контента — чистый Python, ПРОВЕРЕНО (тест).
  - html_to_pdf (рендер) — ПРОВЕРЕНО вживую (Playwright + chromium уже в стеке).
  - generate_onepager_html (вызов Kimi) — API СВЕРЕН ВЖИВУЮ на установленном
    kimi-agent-sdk 0.0.5: prompt()/Message.extract_text()/yolo совпали, модуль импортится.
    import заработал после локального патча бага kimi-cli под Python 3.12 (см. README →
    «Патч»). Для реального ПРОГОНА не хватает только endpoint провайдера (KIMI_*).
    Всё в изолированном .venv_kimi (kimi + playwright) — глобальное окружение не задето.

Окружение для реального запуска (Kimi CLI ходит на OpenAI-совместимый /v1 — кастомный
провайдер это его РОДНОЙ режим, роутер не нужен):
    set KIMI_API_KEY=<ключ провайдера>
    set KIMI_BASE_URL=<endpoint /v1>
    set KIMI_MODEL_NAME=<ID модели K2.7 у провайдера>

CLI (разовый прогон одной компании):
    py onepager_kimi.py "АО «Рязаньавтодор»" --industry "дорожное строительство" --out out.pdf
"""
import argparse
import asyncio
import os
import pathlib
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from onepager_system import ONEPAGER_SYSTEM, SPEAKER_PHOTO_TOKEN  # noqa: E402
from html_to_pdf import onepager_html_to_pdf                       # noqa: E402

# --- Фиксированная сторона отправителя (как в деке рабочего пайплайна: слайды 1–2 неизменны) ---
# ⚠️ Блоки 1–3, 6 и 8 листа — КАНОН: они одинаковы у всех компаний и передаются модели
# ДОСЛОВНО. Уникален только блок 4 («Одна проблема — одно решение») и подписи метрик.
# Без этого модель каждый прогон переписывает шапку/фичи заново и ВЫДУМЫВАЕТ регалии
# живому человеку — так и было в первых прогонах.
VENDOR_NAME = "АО «ЦИТ РТ»"
VENDOR_WORDMARK = "ЦИТ РТ"
PRODUCT = ("NewTeleprompt — корпоративная LLM-платформа в защищённом on-premises контуре: "
           "RAG-базы знаний, автономные ИИ-агенты, ИИ-Коуч «Наставник».")

# Контакты для CTA — ОТПРАВИТЕЛЯ (клиент по ним звонит НАМ). Не путать с контактами лида
# из Фазы 1: те принадлежат клиенту и в блок «назначьте демо» попасть не должны.
VENDOR_PHONE = "+7 917 876 7741"
VENDOR_CONTACT_PERSON = "Шабанов Али Магомедович"   # владелец телефона — строкой ПОД номером
VENDOR_EMAIL = "Ali.Shabanov@tatar.ru"
VENDOR_SITE = "citrt.ru"

# Блок 1 (шапка) и блок 2 (герой) — дословно.
HERO_KICKER = "Разработчик платформы NewTeleprompt"
HERO_TITLE = "АО «ЦИТ РТ»"
HERO_LEAD = ("Государственный Центр информационных технологий Республики Татарстан — "
             "разработчик платформы NewTeleprompt: корпоративная LLM-платформа в защищённом "
             "on-premises контуре. RAG-базы знаний, автономные ИИ-агенты и ИИ-Коуч «Наставник».")

# Блок 3 — 4 фичи (преимущества отправителя, канон слайда 1 рабочего дека).
FEATURES = [
    ("Суверенность данных", "Всё работает on-premises, в контуре заказчика. Соответствие 152-ФЗ."),
    ("Импортозамещение", "Российская разработка, автономный контур без внешних облаков."),
    ("Модель «гос-к-гос»", "Госкомпания РТ — доверенный партнёр для предприятий с госучастием."),
    ("Отраслевой опыт", "Энергетика, нефтегаз, ритейл, госсектор."),
]

SPEAKER_NAME = "Булат Замалиев"
# ⚠️ Публично подтверждено: «Уполномоченный по технологиям ИИ при Минцифры РТ».
# Связь с ЦИТ РТ как «руководителя направления» публично НЕ подтверждена — не утверждать.
SPEAKER_ROLE = "Уполномоченный по технологиям ИИ при Минцифры РТ"
# Блок 6 — регалии ТОЛЬКО из этого проверенного списка (канон рабочего дека). Модели
# запрещено добавлять свои: это факты о живом человеке.
SPEAKER_FACTS = [
    "Уполномоченный по технологиям ИИ в Республике Татарстан — первый в России (с 2019).",
    "Номинант рейтинга Forbes «30 до 30» (2020), категория «Управление» "
    "(именно номинант/лонг-лист, НЕ победитель).",
    "Победитель всероссийского хакатона «Цифровой прорыв» (2019), грант 3 млн ₽.",
    "Идеолог ИИ-сервиса «Госпромпт» для госслужащих и проекта «Цифровая деревня».",
]

# Блок 8 — футер: реквизиты отправителя (канон рабочего дека).
FOOTER = "АО «ЦИТ РТ» · ИНН 1655505808 · ОГРН 1241600056829 · ПАО «АК БАРС» Банк"

# Фото спикера (переиспользуем ассет рабочего пайплайна, не дублируем бинарник).
_DEFAULT_PHOTO = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "lead_orchestrator", "assets", "bulat_zamaliev.png")


def build_user_content(company_name, industry="", pain="", contacts=None):
    """Собрать контент для промпта (что ждёт ONEPAGER_SYSTEM: отправитель, продукт,
    клиент-получатель, проблема/решение, метрики, спикер, контакты). Фикс-сторона —
    константы выше; переменная — клиент/боль. Чистая функция, тестируется.

    contacts — контакты ОТПРАВИТЕЛЯ для блока CTA (по умолчанию — константы VENDOR_*);
    передавать сюда контакты лида НЕЛЬЗЯ, иначе клиента зовут звонить самому себе."""
    contacts = contacts or {}
    lines = [
        "Свёрстай one-pager СТРОГО по системному промпту (каркас x-dc, 8 блоков, токены).",
        "",
        "ГЛАВНОЕ ПРАВИЛО КОНТЕНТА. Блоки 1, 2, 3, 6, 8 — КАНОНИЧЕСКИЕ: они одинаковы у всех "
        "заказчиков. Их текст дан ниже ДОСЛОВНО — воспроизведи его без изменений, не "
        "перефразируй, не сокращай, не добавляй своего. Под конкретного заказчика пишутся "
        "ТОЛЬКО блок 4 («Одна проблема — одно решение») и подписи метрик в блоке 5.",
        "",
        "БЛОК 1 — ШАПКА (дословно):",
        f"- Кикер слева: {HERO_KICKER}",
        f"- Словомарк справа (заглавными): {VENDOR_WORDMARK}",
        "",
        "БЛОК 2 — ГЕРОЙ (дословно):",
        f"- Заголовок H1: {HERO_TITLE}",
        f"- Абзац с акцент-линией: {HERO_LEAD}",
        "",
        "БЛОК 3 — 4 ФИЧИ (дословно, порядок и формулировки не менять):",
    ]
    for i, (title, desc) in enumerate(FEATURES, 1):
        lines.append(f"- {i:02d} {title} — {desc}")
    lines += [
        "",
        f"КЛИЕНТ-ПОЛУЧАТЕЛЬ (для кикера «Персонально для …» в блоке 4): {company_name}"
        + (f"; отрасль/деятельность: {industry}" if industry else ""),
        "",
        "БЛОК 4 — «Одна проблема — одно решение» — ЕДИНСТВЕННЫЙ УНИКАЛЬНЫЙ блок. Сформулируй "
        "одну ключевую боль ИМЕННО этого заказчика (слева, пункты 01–04) и одно решение на "
        "платформе NewTeleprompt (справа: RAG-база / ИИ-агенты / ИИ-Коуч / on-premises). Опирайся "
        "на факты ниже; чего нет — оцени по отрасли и подай как отраслевую оценку, без "
        "выдуманных цифр по самому заказчику.",
    ]
    if pain:
        lines += ["Известные боли из ресёрча:", pain]
    lines += [
        "",
        "БЛОК 5 — МЕТРИКИ: значения фиксированы — «≈8×», «15–30», «Секунды», «On-prem» "
        "(последнее в акценте). Под отрасль заказчика адаптируй только мелкие подписи под ними. "
        "Это отраслевые оценки эффекта, не замеры у заказчика.",
        "  ⚠️ ОТСТУПЫ БЛОКА МЕТРИК (частая ошибка — первая ячейка уезжает за край листа): "
        "у самой полосы метрик ОБЯЗАТЕЛЬНО padding:28px 56px — те же поля 56px слева и справа, "
        "что у остальных блоков. Делители между ячейками ставь через border-left у 2-й–4-й ячеек "
        "(+ padding-left:20px у них), а НЕ отрицательными отступами и не padding'ом на первой "
        "ячейке. Ни цифра, ни подпись не должны касаться края листа или линейки.",
        "  ⚠️ ЗНАЧЕНИЕ МЕТРИКИ — ВСЕГДА В ОДНУ СТРОКУ. «On-prem» НЕ ДОЛЖЕН разрываться на «On-» и "
        "«prem» (частая ошибка). Каждому значению ставь white-space:nowrap. Если «On-prem» при "
        "этом не влезает в ячейку — уменьши размер шрифта ИМЕННО этого значения (до 30–34px), "
        "но перенос запрещён. Подписи под значениями переноситься могут — ограничение только "
        "на сами значения.",
        "  ⚠️ ВОЗДУХ В ЯЧЕЙКАХ МЕТРИК: у каждой ячейки padding-right:20px (у 2-й–4-й ещё и "
        "padding-left:20px рядом с их border-left). Ни значение, ни подпись НЕ должны касаться "
        "вертикальной линейки-делителя — между текстом и линейкой всегда просвет.",
        "",
        "БЛОК 6 — СПИКЕР (дословно):",
        f"- Имя: {SPEAKER_NAME}",
        f"- Должность: {SPEAKER_ROLE}",
        f"- В <img> фото спикера ОБЯЗАТЕЛЬНО используй src=\"{SPEAKER_PHOTO_TOKEN}\" "
        "(его подставит рендерер).",
        "- Регалии — ТОЛЬКО из этого проверенного списка, дословно. Ничего не добавляй и не "
        "придумывай: это факты о живом человеке.",
    ]
    lines += [f"  — {f}" for f in SPEAKER_FACTS]
    lines += [
        "",
        "БЛОК 7 — CTA. Контакты ОТПРАВИТЕЛЯ (по ним заказчик звонит нам) — только данные ниже, "
        "строками в этом порядке:",
        f"- Телефон: {contacts.get('phone') or VENDOR_PHONE}",
        f"- Сразу ПОД телефоном, отдельной строкой — ФИО владельца номера: {VENDOR_CONTACT_PERSON}. "
        "Тот же шрифт и тот же размер, что у телефона (Archivo, как остальные строки контактов); "
        "не курсив, не в акцентном цвете, не мельче.",
        f"- E-mail: {contacts.get('email') or VENDOR_EMAIL}",
        f"- Сайт (последней строкой, в акценте): {contacts.get('website') or VENDOR_SITE}",
        "",
        f"БЛОК 8 — ФУТЕР (дословно, одной строкой): {FOOTER}",
    ]
    return "\n".join(lines)


# Агент без тулов: дефолтный агент kimi-cli требует kimi_cli.tools.web:SearchWeb/FetchURL,
# которые у кастомного провайдера не грузятся -> InvalidToolError ещё до вызова модели.
_AGENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "kimi_agent", "onepager.yaml")


async def _generate_html_claude(user_content, model=None):
    """Claude-runtime той же стадии: ONEPAGER_SYSTEM + контент -> HTML-текст ответа.

    Агент без тулов, как и kimi-вариант: стадия чисто текстовая. Запускается
    python'ом ОСНОВНОГО окружения (claude-agent-sdk там; CLI бандлится в пакет)."""
    try:
        from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                      ResultMessage, TextBlock, query)
    except ImportError as e:
        raise RuntimeError(
            "claude-agent-sdk не установлен: claude-runtime one-pager запускается "
            "python'ом основного окружения") from e

    model = model or os.environ.get("ORQ_ONEPAGER_MODEL") or "opus"
    # max_turns=1 требовал уместить весь самодостаточный HTML листа в ОДИН ход:
    # если модель не успевала закрыть разметку, сессия падала с error_max_turns и
    # стадия возвращала пустой PDF. Тулов у агента нет, лишние ходы взяться неоткуда —
    # это запас на продолжение длинного ответа, а не свобода действий.
    try:
        max_turns = max(1, int(os.environ.get("ORQ_ONEPAGER_MAX_TURNS") or 4))
    except ValueError:
        max_turns = 4
    options = ClaudeAgentOptions(
        model=model, system_prompt=ONEPAGER_SYSTEM, max_turns=max_turns,
        allowed_tools=[],
        disallowed_tools=["Bash", "Edit", "Write", "NotebookEdit",
                          "WebSearch", "WebFetch", "Read", "Glob", "Grep"],
        permission_mode="bypassPermissions", setting_sources=[])
    text = ""
    async for message in query(prompt=user_content, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text += block.text
        elif isinstance(message, ResultMessage):
            if message.is_error:
                raise RuntimeError(
                    f"Claude one-pager завершился ошибкой: {message.subtype}")
            if message.result:
                text = message.result
    return text.strip()


async def generate_onepager_html(user_content, model=None, thinking=False):
    """Вызвать модель активного runtime по ONEPAGER_SYSTEM -> вернуть HTML-текст ответа.

    Сигнатура prompt() и Message.extract_text() СВЕРЕНЫ по исходникам установленного
    kimi-agent-sdk 0.0.5. Нативного параметра system в prompt() НЕТ (задаётся через
    agent_file/config), поэтому системную инструкцию кладём префиксом в user_input —
    работает независимо от механизма; при желании можно вынести в agent_file.
    Проверено вживую: kimi_agent_sdk импортится (после локального патча venv, см. README),
    Message.extract_text() существует. Не хватает только endpoint провайдера для прогона.

    При ORQ_LLM_RUNTIME=claude тот же контент уходит в Claude Agent SDK — контракт CLI
    (аргументы, exit-коды, файл по --out) не меняется."""
    if (os.environ.get("ORQ_LLM_RUNTIME") or "").strip().lower() == "claude":
        return await _generate_html_claude(user_content, model=model)
    try:
        from kimi_agent_sdk import prompt   # [СВЕРИТЬ] точка входа
    except ImportError as e:
        raise RuntimeError(
            "kimi-agent-sdk не установлен: pip install kimi-agent-sdk. "
            "Также задай KIMI_API_KEY / KIMI_BASE_URL / KIMI_MODEL_NAME."
        ) from e

    model = model or os.environ.get("KIMI_MODEL_NAME")
    full_input = f"{ONEPAGER_SYSTEM}\n\n===== ВХОДНЫЕ ДАННЫЕ =====\n\n{user_content}"

    chunks = []
    # Сверено по исходникам kimi-agent-sdk 0.0.5: prompt() — async-генератор Message;
    # yolo=True авто-аппрувит вызовы (без yolo и без approval_handler_fn — PromptValidationError);
    # final_message_only=True отдаёт только финальный Message. Текст достаём штатным
    # Message.extract_text() (официальный пример из docstring пакета).
    async for message in prompt(full_input, model=model, thinking=thinking,
                                yolo=True, final_message_only=True,
                                agent_file=pathlib.Path(_AGENT_FILE)):
        chunks.append(message.extract_text())
    return "".join(chunks).strip()


async def make_onepager(company_name, out_pdf, industry="", pain="",
                        contacts=None, model=None, photo=None, html_out=None):
    """Полная стадия: собрать контент -> Kimi -> HTML -> PDF. Возвращает путь к PDF.
    html_out — куда сложить сырой ответ модели (отладка вёрстки без повторного вызова)."""
    content = build_user_content(company_name, industry, pain, contacts)
    html = await generate_onepager_html(content, model=model)
    if html_out:
        with open(html_out, "w", encoding="utf-8") as f:
            f.write(html)
    photo = photo or (_DEFAULT_PHOTO if os.path.exists(_DEFAULT_PHOTO) else None)
    await onepager_html_to_pdf(html, out_pdf, photo)
    return out_pdf


def _cli(argv):
    ap = argparse.ArgumentParser(description="One-pager (HTML->PDF) на Kimi Agent SDK")
    ap.add_argument("company", help="название компании-клиента")
    ap.add_argument("--industry", default="", help="отрасль/деятельность клиента")
    ap.add_argument("--pain", default="", help="известная боль (иначе модель оценит по отрасли)")
    ap.add_argument("--phone", default="")
    ap.add_argument("--email", default="")
    ap.add_argument("--website", default="")
    ap.add_argument("--model", default=None, help="ID модели (иначе env KIMI_MODEL_NAME)")
    ap.add_argument("--photo", default=None, help="фото спикера (иначе ассет пайплайна)")
    ap.add_argument("--out", default="onepager.pdf")
    ap.add_argument("--html-out", default=None, help="сохранить сырой HTML модели (отладка)")
    a = ap.parse_args(argv)
    contacts = {"phone": a.phone, "email": a.email, "website": a.website}
    out = asyncio.run(make_onepager(a.company, a.out, a.industry, a.pain,
                                    contacts, a.model, a.photo, a.html_out))
    size = os.path.getsize(out) if os.path.exists(out) else 0
    print(f"PDF: {out} ({size} байт)")
    return 0 if size > 5000 else 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
