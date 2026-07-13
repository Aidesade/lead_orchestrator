# -*- coding: utf-8 -*-
r"""
Claude Agent SDK-обёртка ПОЛНОЙ ЦЕПОЧКИ (оркестратора).

NL-запрос -> агент подбирает параметры -> инструмент run_full_chain ЗАПУСКАЕТ
orchestrator.py: ФАЗА 1 сбор лидов (RusProfile, выручка>порога) + ФАЗА 2 ресёрч
по каждой компании (досье + стратегия .docx) -> папки Яндекс Диска.

Почему обёртка ЗАПУСКАЕТ orchestrator.py ПОДПРОЦЕССОМ, а не зовёт его функции
in-process: фаза 2 оркестратора сама поднимает по ClaudeSDKClient на компанию.
Поднимать SDK-агентов ВНУТРИ обработчика инструмента ДРУГОГО SDK-агента — лишний
риск (вложенные клиенты/циклы). Подпроцесс изолирует их в дочернем процессе и
переиспользует уже проверенный CLI оркестратора. stdout наследуется -> прогресс
оркестратора (включая строку [ГОТОВО]) виден в консоли вживую; stderr копим для
отчёта об ошибке.

Chrome при сборе по умолчанию БЕЗ окна (headless). Показать окно — show_browser=true.

Запуск:
  py C:/Users/abalb/.claude/skills/lead-finder/scripts/orchestrator_agent.py
  py .../orchestrator_agent.py "собери 10 по майнингу, dry-run"
Зависимости: pip install claude-agent-sdk (+ всё, что нужно orchestrator.py).
Платный аккаунт RusProfile нужен для сбора — один раз:
  py .../rusprofile_session.py --login
"""
import os
import subprocess
import sys
import warnings

# Глушим косметический RequestsDependencyWarning (chardet 7.x вне диапазона requests) ДО импорта requests.
warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

import anyio
from claude_agent_sdk import (
    tool,
    create_sdk_mcp_server,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    TextBlock,
    ToolUseBlock,
)

try:                                   # рамка меню и ₽ ломаются в cp1251-консоли
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import source_rusprofile as RP  # noqa: E402  -> карта отраслей INDUSTRY

ORCH = os.path.join(SCRIPTS, "orchestrator.py")
VALID = sorted(RP.INDUSTRY)
# карта «ключ — название» для модели: сопоставить запрос ('добыча угля') с ключом ('mining')
INDUSTRY_HINT = "; ".join(f"{k} — {RP.INDUSTRY[k]['label']}" for k in VALID)

# имя тула для can_use_tool: mcp__<ключ сервера>__<имя инструмента>
TOOL_NAME = "mcp__orchestrator__run_full_chain"
COUNT_CHOICES = (5, 10, 200)      # пункты меню «лидов НА КАЖДУЮ отрасль»
DEFAULT_PER_INDUSTRY = 200        # прод-дефолт (вся отрасль), если пользователь не назвал число
USD_PER_COMPANY = 4               # ~$3–4.5 за компанию: ресёрч (2 .docx) + презентация
BIG_RUN_COMPANIES = 50            # выше — боевой прогон переспрашивается отдельно


def _txt(t):
    return {"content": [{"type": "text", "text": t}]}


def parse_industries(raw):
    """'opk, Mining; water' -> ['opk','mining','water'] — только валидные ключи, без дублей."""
    out = []
    for s in str(raw or "").replace(";", ",").split(","):
        k = s.strip().lower()
        if k in RP.INDUSTRY and k not in out:
            out.append(k)
    return out


def per_industry(args):
    """Сколько лидов на КАЖДУЮ отрасль (>=1). Не задано -> вся отрасль."""
    try:
        n = int(args.get("count_per_industry") or DEFAULT_PER_INDUSTRY)
    except (TypeError, ValueError):
        n = DEFAULT_PER_INDUSTRY
    return max(1, n)


def _build_cmd(args):
    """Собрать argv для orchestrator.py из параметров инструмента.
    Возвращает (cmd|None, error_text|None). Чистая функция — тестируется без запуска."""
    industries = (args.get("industries") or "").strip()
    leads_json = (args.get("leads_json") or "").strip()
    if not industries and not leads_json:
        return None, ("Нужно указать industries (отрасли через запятую) ИЛИ "
                      "leads_json (путь к готовому JSON). Отрасли: " + ", ".join(VALID))

    cmd = [sys.executable, ORCH]
    if leads_json:
        cmd.append(leads_json)                       # позиционный аргумент: ТОЛЬКО ресёрч
    if industries:
        inds = parse_industries(industries)
        if not inds:
            return None, "Не распознаны отрасли. Доступно: " + ", ".join(VALID)
        cmd += ["--industries", ",".join(inds)]
        # Объём ВСЕГДА «N на КАЖДУЮ отрасль». --count (ВСЕГО по всем отраслям) здесь
        # сознательно не используется: orchestrator.py делит его как ceil(count/K), и
        # «10 на отрасль» по трём отраслям молча превращалось в 4 на отрасль (и в 8/8/8/6
        # после _select). Флаг --count остался только для ручного запуска CLI.
        cmd += ["--per-industry", str(per_industry(args))]
    if args.get("min_revenue"):
        cmd += ["--min-revenue", str(float(args["min_revenue"]))]
    region = (args.get("region") or "").strip()
    if region:
        cmd += ["--region", region]
    cmd += ["--model", (args.get("model") or "opus").strip()]
    cmd += ["--workers", str(int(args.get("workers") or 2))]
    if args.get("show_browser"):
        cmd.append("--show-browser")                 # иначе Chrome скрыт (по умолчанию)
    if args.get("dry_run"):
        cmd.append("--dry-run")
    if args.get("no_upload"):
        cmd.append("--no-upload")
    if args.get("no_presentation"):
        cmd.append("--no-presentation")              # 3-я стадия (one-pager .pdf) иначе ВКЛ по умолчанию
    return cmd, None


async def _do_full_chain(args):
    """Чистая реализация инструмента (тестируется напрямую, без LLM)."""
    cmd, err = _build_cmd(args)
    if cmd is None:
        return _txt(err)

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    def _run():
        # stdout НАСЛЕДУЕТСЯ -> прогресс оркестратора виден вживую; stderr копим
        p = subprocess.run(cmd, stderr=subprocess.PIPE, text=True,
                           encoding="utf-8", errors="replace", env=env)
        return p.returncode, (p.stderr or "")

    rc, errout = await anyio.to_thread.run_sync(_run)
    shown = "orchestrator.py " + " ".join(cmd[2:])    # без python и без пути к скрипту
    if rc == 0:
        return _txt(f"Готово (rc=0). Запускал: {shown}\n"
                    "Прогресс и итоговая строка [ГОТОВО] — выше в консоли.")
    return _txt(f"Оркестратор завершился с ошибкой (rc={rc}). Команда: {shown}\n"
                f"stderr (хвост):\n{(errout.strip() or '(пусто)')[-1200:]}")


@tool(
    "run_full_chain",
    "ПОЛНАЯ ЦЕПОЧКА (по умолчанию ВСЁ): собрать B2B-лиды по отрасли (RusProfile, выручка выше "
    "порога) и по КАЖДОЙ компании сделать 3 файла — карту бизнес-процессов + карту ролей и "
    "контактов (.docx) + one-pager Telepatt (.pdf) — в папки Яндекс Диска. Боевой "
    "режим дорог (~$1–2 за компанию: ресёрч; one-pager считает провайдер Kimi отдельно). "
    "Chrome при сборе скрыт. "
    "Доступные отрасли: " + ", ".join(VALID),
    {
        "industries": str,    # отрасли через запятую (или пусто, если задан leads_json)
        "leads_json": str,    # путь к готовому JSON -> ТОЛЬКО ресёрч ("" = собрать заново)
        # ЕДИНСТВЕННАЯ мера объёма: сколько компаний НА КАЖДУЮ отрасль (итог = N × число отраслей).
        # Режима «N всего по всем отраслям» у обёртки нет. Значение — лишь ПРЕДЛОЖЕНИЕ:
        # перед запуском оно выносится пользователю в меню (can_use_tool) и может быть заменено.
        "count_per_industry": int,  # «по 10 на отрасль» -> 10; не задано -> 200 (вся отрасль)
        "min_revenue": float, # порог выручки в рублях (по умолч. 1e9 = 1 млрд)
        "region": str,        # регион названием/аббревиатурой ("" = вся РФ); "НЕ <регион>" = исключить (напр. "НЕ Москва")
        "model": str,         # opus (качество) | sonnet (дешевле)
        "show_browser": bool, # True = показать окно Chrome (по умолч. скрыт/headless)
        "dry_run": bool,      # True = без LLM и без трат (только заготовки)
        "no_upload": bool,    # True = не грузить результат на Яндекс Диск
        "no_presentation": bool,  # True = НЕ делать one-pager .pdf (по умолчанию ДЕЛАЕТСЯ)
        "workers": int,       # параллелизм ресёрча (по умолч. 2; при нехватке RAM авто-снижается до 1)
    },
)
async def run_full_chain(args):
    return await _do_full_chain(args)


# In-process MCP-сервер из нашего инструмента (без отдельного процесса)
server = create_sdk_mcp_server(name="orchestrator", version="1.0.0",
                               tools=[run_full_chain])


# ===========================================================================
# ГЕЙТ ОБЪЁМА (can_use_tool) — SDK-аналог AskUserQuestion.
#   Колбэк перехватывает вызов run_full_chain ДО запуска и возвращает либо
#   PermissionResultAllow(updated_input=...) с ПЕРЕПИСАННЫМИ аргументами, либо
#   PermissionResultDeny. Молчаливая подмена объёма становится невозможной:
#   что бы модель ни предложила, «N на отрасль» подтверждает человек.
#   Требует streaming-режим — ClaudeSDKClient.connect(None) его и даёт.
# ===========================================================================
def _box(title, lines, pad=1):
    """Рамка с заголовком. Кириллица моноширинная -> len() = ширина."""
    w = max([len(title) + 4] + [len(s) + pad * 2 for s in lines])
    out = ["┌" + ("─ " + title + " ").ljust(w, "─") + "┐"]
    out += ["│" + (" " * pad + s).ljust(w) + "│" for s in lines]
    out.append("└" + "─" * w + "┘")
    return "\n".join(out)


def _volume_lines(inds, choices, proposed, dry_run):
    lines = [f"Отрасли: {', '.join(inds)}",
             "Режим:   N на КАЖДУЮ отрасль", ""]
    for i, n in enumerate(choices, 1):
        total = n * len(inds)
        cost = "бесплатно (dry-run)" if dry_run else f"~${total * USD_PER_COMPANY}"
        mark = "  ←" if n == proposed else ""
        lines.append(f"{i}) {n:>3} на отрасль = {total:>4} комп., {cost}{mark}")
    lines += ["и) изменить отрасли", "0) отмена"]
    return lines


async def _confirm_total(inds, n, dry_run):
    """Страховка на дорогую сторону: цифры 1..3 — это НОМЕРА пунктов, и опечатка «3»
    вместо «3 компании» даёт 200 на отрасль. Крупный боевой прогон переспрашиваем."""
    total = n * len(inds)
    if dry_run or total <= BIG_RUN_COMPANIES:
        return True
    try:
        raw = await anyio.to_thread.run_sync(
            input, f"Боевой прогон: {total} компаний, ~${total * USD_PER_COMPANY}. Продолжить? [y/N]: ")
    except (EOFError, KeyboardInterrupt):
        return False
    return raw.strip().lower() in ("y", "yes", "д", "да")


async def _ask_volume(inds, proposed, dry_run):
    """Меню в консоли. -> (отрасли, N на отрасль) либо None, если пользователь отменил.
    input() блокирующий -> уводим в поток, чтобы не вешать событийный цикл."""
    while True:
        choices = sorted(set(COUNT_CHOICES) | {proposed})
        print("\n" + _box("Подтверди объём", _volume_lines(inds, choices, proposed, dry_run)))
        default = choices.index(proposed) + 1
        try:
            raw = (await anyio.to_thread.run_sync(input, f"Выбор [{default}]: ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if raw in ("0", "отмена", "n", "нет"):
            return None
        if raw in ("и", "i", "отрасли"):
            try:
                got = await anyio.to_thread.run_sync(
                    input, f"Отрасли через запятую ({', '.join(VALID)}): ")
            except (EOFError, KeyboardInterrupt):
                return None
            new_inds = parse_industries(got)
            if new_inds:
                inds = new_inds
            else:
                print("  не распознал ни одной отрасли — оставляю прежние")
            continue

        if not raw:                                        # Enter — предложение модели
            chosen = proposed
        elif raw.isdigit() and 1 <= int(raw) <= len(choices):
            chosen = choices[int(raw) - 1]                 # номер пункта меню
        elif raw.isdigit() and int(raw) > len(choices):    # своё число: «50»
            chosen = int(raw)
        else:
            print(f"  не понял — введи номер пункта (1–{len(choices)}), "
                  f"своё число больше {len(choices)}, «и» или 0")
            continue

        if await _confirm_total(inds, chosen, dry_run):
            return inds, chosen
        print("  отменил — выбери объём заново")


async def _volume_gate(tool_name, input_data, ctx):
    """can_use_tool: объём подтверждает пользователь, а не модель."""
    if tool_name != TOOL_NAME:
        return PermissionResultAllow()
    args = dict(input_data)
    inds = parse_industries(args.get("industries"))
    if not inds:
        return PermissionResultAllow()   # только ресёрч по leads_json (объёма нет) либо ошибка в _build_cmd
    proposed = per_industry(args)
    if not sys.stdin.isatty():           # неинтерактивный запуск: не на чем спрашивать
        print(f"[объём] не интерактивно — беру {proposed} на отрасль без подтверждения")
        return PermissionResultAllow()
    decision = await _ask_volume(inds, proposed, bool(args.get("dry_run")))
    if decision is None:
        return PermissionResultDeny(message="Пользователь отменил запуск.", interrupt=True)
    inds, n = decision
    args["industries"] = ",".join(inds)
    args["count_per_industry"] = n
    print(f"[объём] {n} на отрасль × {len(inds)} отрасл. = {n * len(inds)} компаний")
    return PermissionResultAllow(updated_input=args)


SYSTEM_PROMPT = (
    "Ты — оператор полной цепочки лидогенерации. Инструмент run_full_chain делает ВСЁ за один "
    "вызов: собирает компании по отрасли (RusProfile, выручка выше порога) и по КАЖДОЙ компании "
    "готовит 3 файла — карту бизнес-процессов + карту ролей и контактов (.docx) + "
    "one-pager Telepatt (.pdf) — в папки Яндекс Диска. Все три файла делаются ПО УМОЛЧАНИЮ.\n"
    "Подбери ОДИН ключ-отрасль (или несколько через запятую) под запрос из списка "
    "«ключ — название»: " + INDUSTRY_HINT + ". Достаточно, чтобы пользователь назвал ТОЛЬКО отрасль. "
    "Регион, если назван, передавай в параметр region КАК ЕСТЬ — названием/аббревиатурой "
    "('ХМАО', 'Югра', 'Татарстан'), НЕ кодом. Если регион нужно ИСКЛЮЧИТЬ — передавай с приставкой "
    "'НЕ' ('НЕ Москва' = вся РФ кроме Москвы); можно смешивать через запятую ('Урал, НЕ Москва').\n"
    "ОБЪЁМ: единственная мера — count_per_industry, лидов НА КАЖДУЮ отрасль (итог = N × число "
    "отраслей). Режима «N всего по всем отраслям» у инструмента НЕТ. «по 10 на отрасль» => 10; "
    "«30 лидов по трём отраслям» => 10; число не названо — поле не задавай (возьмётся вся отрасль). "
    "Твоё значение — лишь ПРЕДЛОЖЕНИЕ: перед запуском объём и отрасли подтверждает пользователь "
    "в меню. Не спрашивай число текстом и не считай арифметику в чате — это делает меню.\n"
    "ДЕНЬГИ: боевой прогон ~$3–4.5 за компанию (ресёрч + презентация на opus), 200 компаний ≈ ~$800 и "
    "несколько часов. Подтверждение объёма и стоимости берёт на себя меню — НЕ спрашивай «да?» в чате "
    "и не жди ответа, просто вызывай инструмент. Если пользователь отменит в меню, вызов вернёт отказ: "
    "сообщи об этом и НЕ повторяй вызов. dry_run=true — бесплатная проверка связки.\n"
    "One-pager (.pdf) делаем ПО УМОЛЧАНИЮ; ставь no_presentation=true только если пользователь "
    "явно просит без презентации.\n"
    "Chrome при сборе по умолчанию СКРЫТ. show_browser=true — только если просят видеть браузер.\n"
    "ПАРАЛЛЕЛИЗМ: workers держи низким (1–2) — каждый ресёрч/презентация поднимает claude CLI + "
    "LibreOffice (сотни МБ); при нехватке RAM падает 0xC0000409, оркестратор сам снизит до 1. НЕ "
    "поднимай workers без явной просьбы.\n"
    "Если пользователь дал путь к готовому JSON лидов — клади его в leads_json (тогда сбор "
    "пропускается, только ресёрч+презентация). После завершения коротко отчитайся по результату."
)


async def _ask(client, prompt):
    """Отправить запрос и напечатать ответ агента человекочитаемо."""
    await client.query(prompt)
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    print(block.text)
                elif isinstance(block, ToolUseBlock):
                    print(f"  → запускаю {getattr(block, 'name', 'инструмент')} …")


async def main():
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"orchestrator": server},
        allowed_tools=[TOOL_NAME],
        # объём (отрасли + N на отрасль) утверждает пользователь, а не модель
        can_use_tool=_volume_gate,
    )
    # запрос из аргументов: py orchestrator_agent.py "собери 10 по майнингу, dry-run"
    cli_prompt = " ".join(sys.argv[1:]).strip()
    async with ClaudeSDKClient(options=options) as client:
        if cli_prompt:                               # одноразовый режим
            await _ask(client, cli_prompt)
            return
        # интерактивный режим: несколько запросов в одной сессии (контекст копится)
        print("Оркестратор-агент (сбор + ресёрч → Яндекс Диск). "
              "Chrome при сборе скрыт. Пустая строка или 'exit' — выход.")
        while True:
            try:
                prompt = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not prompt or prompt.lower() in ("exit", "quit", "выход"):
                break
            await _ask(client, prompt)


if __name__ == "__main__":
    anyio.run(main)
