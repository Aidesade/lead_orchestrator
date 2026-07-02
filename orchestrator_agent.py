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
    TextBlock,
    ToolUseBlock,
)

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import source_rusprofile as RP  # noqa: E402  -> карта отраслей INDUSTRY

ORCH = os.path.join(SCRIPTS, "orchestrator.py")
VALID = sorted(RP.INDUSTRY)
# карта «ключ — название» для модели: сопоставить запрос ('добыча угля') с ключом ('mining')
INDUSTRY_HINT = "; ".join(f"{k} — {RP.INDUSTRY[k]['label']}" for k in VALID)


def _txt(t):
    return {"content": [{"type": "text", "text": t}]}


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
        inds = [s.strip() for s in industries.split(",") if s.strip() in RP.INDUSTRY]
        if not inds:
            return None, "Не распознаны отрасли. Доступно: " + ", ".join(VALID)
        cmd += ["--industries", ",".join(inds)]

    cmd += ["--count", str(int(args.get("count") or 200))]   # прод-дефолт 200 (если не задано иное)
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
        cmd.append("--no-presentation")              # 3-я стадия (.pptx) иначе ВКЛ по умолчанию
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
    "контактов (.docx) + one-page презентацию Telepath (.pptx) — в папки Яндекс Диска. Боевой "
    "режим дорог (~$3–4.5 за компанию: ресёрч + презентация). Chrome при сборе скрыт. "
    "Доступные отрасли: " + ", ".join(VALID),
    {
        "industries": str,    # отрасли через запятую (или пусто, если задан leads_json)
        "leads_json": str,    # путь к готовому JSON -> ТОЛЬКО ресёрч ("" = собрать заново)
        "count": int,         # сколько компаний (по умолчанию 200)
        "min_revenue": float, # порог выручки в рублях (по умолч. 1e9 = 1 млрд)
        "region": str,        # регион названием/аббревиатурой ("" = вся РФ); "НЕ <регион>" = исключить (напр. "НЕ Москва")
        "model": str,         # opus (качество) | sonnet (дешевле)
        "show_browser": bool, # True = показать окно Chrome (по умолч. скрыт/headless)
        "dry_run": bool,      # True = без LLM и без трат (только заготовки)
        "no_upload": bool,    # True = не грузить результат на Яндекс Диск
        "no_presentation": bool,  # True = НЕ делать .pptx-презентацию (по умолчанию ДЕЛАЕТСЯ)
        "workers": int,       # параллелизм ресёрча (по умолч. 2; при нехватке RAM авто-снижается до 1)
    },
)
async def run_full_chain(args):
    return await _do_full_chain(args)


# In-process MCP-сервер из нашего инструмента (без отдельного процесса)
server = create_sdk_mcp_server(name="orchestrator", version="1.0.0",
                               tools=[run_full_chain])


SYSTEM_PROMPT = (
    "Ты — оператор полной цепочки лидогенерации. Инструмент run_full_chain делает ВСЁ за один "
    "вызов: собирает компании по отрасли (RusProfile, выручка выше порога) и по КАЖДОЙ компании "
    "готовит 3 файла — карту бизнес-процессов + карту ролей и контактов (.docx) + one-page "
    "презентацию Telepath (.pptx) — в папки Яндекс Диска. Все три файла делаются ПО УМОЛЧАНИЮ.\n"
    "Подбери ОДИН ключ-отрасль (или несколько через запятую) под запрос из списка "
    "«ключ — название»: " + INDUSTRY_HINT + ". Достаточно, чтобы пользователь назвал ТОЛЬКО отрасль. "
    "Регион, если назван, передавай в параметр region КАК ЕСТЬ — названием/аббревиатурой "
    "('ХМАО', 'Югра', 'Татарстан'), НЕ кодом. Если регион нужно ИСКЛЮЧИТЬ — передавай с приставкой "
    "'НЕ' ('НЕ Москва' = вся РФ кроме Москвы); можно смешивать через запятую ('Урал, НЕ Москва').\n"
    "ОБЪЁМ: если пользователь НЕ назвал число компаний — бери count=200 (прод-дефолт). Если назвал — "
    "используй его.\n"
    "ДЕНЬГИ: боевой прогон ~$3–4.5 за компанию (ресёрч + презентация на opus), 200 компаний ≈ ~$800 и "
    "несколько часов. Перед боевым (не dry_run) запуском назови ОДНОЙ строкой оценку (count × ~$4) и "
    "примерное время, затем спроси КОРОТКОЕ подтверждение ('Запускаю N по <отрасль>, ~$X, ~Y ч — да?') "
    "и запускай ТОЛЬКО после 'да'. dry_run=true (бесплатная проверка связки) запускай сразу.\n"
    "Презентацию (.pptx) делаем ПО УМОЛЧАНИЮ; ставь no_presentation=true только если пользователь "
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
        # имя = mcp__<ключ сервера>__<имя инструмента>
        allowed_tools=["mcp__orchestrator__run_full_chain"],
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
