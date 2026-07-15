# -*- coding: utf-8 -*-
"""Писатель двух пресейл-.docx на Kimi.

Чем отличается от писателя на Claude (orchestrator._research_one): тот — АГЕНТ с
инструментами (in-process MCP + WebSearch/WebFetch, до 80 ходов), он сам решает, когда
сохранить документ. Kimi здесь работает проще и детерминированнее: инструментов нет,
ресёрч уже выполнен движком deep_research (и person_enrich), поэтому модель ОДНИМ вызовом
возвращает JSON по ТОЙ ЖЕ схеме, а .docx рендерят ТЕ ЖЕ функции CRA._write_*_docx.
Формат документов от смены модели не меняется — меняется только автор текста.

Почему не kimi-agent-sdk: он конфликтует с claude-agent-sdk по pydantic-core и потому
живёт в отдельном venv (см. комментарий в Dockerfile). Тащить его в основной процесс
нельзя. Здесь обычный OpenAI-совместимый HTTP через `openai`, который и так закреплён
в requirements.txt (openai==2.44.0) — новых зависимостей ноль.

Следствие, которое надо знать: у писателя на Kimi НЕТ веб-инструментов, поэтому он не
может добрать пробелы «на лету». Всё, что попадёт в документы, приходит из находок
дипресёрча. На качество это влияет ровно настолько, насколько полон движок.
"""
from __future__ import annotations

import asyncio
import json
import os


def _cra():
    """company_research_agent тянет за собой claude-agent-sdk и python-docx. Импортируем
    лениво: orchestrator зовёт is_kimi()/kimi_model() ещё на этапе разбора аргументов,
    и платить за этот импорт там незачем."""
    import company_research_agent as CRA
    return CRA


BASE_URL_DEFAULT = "https://gpllmkeeper.dtc.tatar/v1"
MODEL_DEFAULT = "kimi-k2.7-code"

MAX_TOKENS = int(os.environ.get("KIMI_WRITER_MAX_TOKENS", "16000"))
TIMEOUT = float(os.environ.get("KIMI_WRITER_TIMEOUT", "600"))
ATTEMPTS = int(os.environ.get("KIMI_WRITER_ATTEMPTS", "3"))


def kimi_key() -> str:
    """Ключ провайдера. Тот же порядок, что у стадии one-pager (orchestrator._kimi_key)."""
    return (os.environ.get("KIMI_API_KEY") or os.environ.get("GPLLM_API_KEY") or "").strip()


def kimi_model(model: str | None = None) -> str:
    """Имя модели у провайдера. UI/CLI передаёт 'kimi' — это псевдоним, а не имя модели:
    реальное имя берём из env (как и стадия one-pager), иначе дефолт."""
    m = (model or "").strip()
    if m and m.lower() not in ("kimi", "kimi-writer"):
        return m                                   # явное имя модели передали как есть
    return (os.environ.get("KIMI_WRITER_MODEL")
            or os.environ.get("KIMI_MODEL_NAME")
            or MODEL_DEFAULT)


def kimi_base_url() -> str:
    return (os.environ.get("KIMI_BASE_URL") or BASE_URL_DEFAULT).strip()


def is_kimi(model: str | None) -> bool:
    return str(model or "").strip().lower().startswith("kimi")


# Дополнение к PRESALE_SYSTEM: тот промпт написан под агента с инструментами и содержит
# порядок «вызови deep_research, потом WebSearch». Для Kimi этот порядок неприменим —
# переопределяем его явно, иначе модель будет просить инструменты, которых нет.
SYSTEM_TAIL = """

--- РЕЖИМ РАБОТЫ (ПЕРЕОПРЕДЕЛЯЕТ ПОРЯДОК ВЫШЕ) ---
Инструментов у тебя НЕТ. deep_research уже отработал отдельным движком, его находки
целиком даны в сообщении пользователя. Ничего не вызывай, ничего не проси, не пиши
«я не могу выполнить поиск» — весь нужный материал уже перед тобой.

ОТВЕТ: строго ОДИН JSON-объект по схеме из запроса. Без markdown, без ```-заборов,
без пояснений до и после. Никаких комментариев внутри JSON.

ДАННЫЕ: бери из находок. Ничего не выдумывай: если данных по полю нет — оставь пустую
строку или пустой список. Но и не пиши «не подтверждено» там, где в находках данные ЕСТЬ
(филиалы с директорами и телефонами, соцсети, официальные контакты, прямые контакты ЛПР) —
их надо перенести вместе с источником.
"""


def _user_prompt(title: str, schema: dict, name: str, inn: str,
                 findings: str, extra: str = "") -> str:
    return (
        f"КОМПАНИЯ: {name}\n"
        f"ИНН: {inn}\n\n"
        "НАХОДКИ ДИПРЕСЁРЧА (у строк проставлен source URL):\n"
        "-----8<-----\n"
        f"{findings}\n"
        "----->8-----\n\n"
        f"ЗАДАЧА: на основе ЭТИХ находок заполни документ «{title}» и верни ОДИН JSON-объект "
        "строго по приведённой схеме.\n"
        + (extra + "\n" if extra else "")
        + "\nСХЕМА (JSON Schema):\n"
        + json.dumps(schema, ensure_ascii=False)
    )


PROCESS_EXTRA = (
    "Обязательно: разложи ключевые процессы as-is с болями и точкой внедрения ИИ "
    "(RAG-база знаний / автономные агенты / ИИ-Коуч), опирайся на профиль, финансы и "
    "контракты из находок. У фактов проставляй source."
)

ROLES_EXTRA = (
    "Обязательно перенеси из находок: таблицу филиалов (директор + телефон), соцсети, "
    "официальные контакты, блок «Экосистема и вертикаль» -> ecosystem_table, а если есть "
    "блок «ПРЯМЫЕ КОНТАКТЫ ЛПР» — прямой email/телефон ЛПР с источником и уровнем доверия."
)


def _client():
    from openai import AsyncOpenAI          # локальный импорт: не тянем SDK, если Kimi не выбран

    key = kimi_key()
    if not key:
        raise RuntimeError("нет ключа Kimi: задай KIMI_API_KEY или GPLLM_API_KEY")
    return AsyncOpenAI(base_url=kimi_base_url(), api_key=key,
                       timeout=TIMEOUT, max_retries=0)


async def _ask_json(client, model: str, system: str, user: str, idx: int, label: str):
    """Спросить у Kimi JSON. Возвращает (payload, usage)."""
    last = ""
    prompt = user
    for attempt in range(1, ATTEMPTS + 1):
        kwargs = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "max_tokens": MAX_TOKENS,
            "temperature": 0.2,
        }
        # JSON-режим поддерживают не все OpenAI-совместимые шлюзы. Просим его только на
        # первой попытке: если шлюз ругнётся — идём дальше без него, а не валим стадию.
        if attempt == 1:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            r = await client.chat.completions.create(**kwargs)
        except Exception as e:                     # noqa: BLE001 — сеть/шлюз/лимиты
            last = f"{type(e).__name__}: {str(e)[:140]}"
            print(f"    [{idx}] [kimi:{label}] попытка {attempt}/{ATTEMPTS}: {last}")
            await asyncio.sleep(2 * attempt)
            continue

        text = (r.choices[0].message.content or "").strip()
        try:
            return _cra()._extract_json(text), getattr(r, "usage", None)
        except (ValueError, json.JSONDecodeError) as e:
            last = f"не JSON: {str(e)[:120]}"
            print(f"    [{idx}] [kimi:{label}] попытка {attempt}/{ATTEMPTS}: {last}")
            prompt = (user + "\n\nПРЕДЫДУЩИЙ ОТВЕТ НЕ РАЗОБРАЛСЯ КАК JSON. "
                             "Верни ТОЛЬКО JSON-объект по схеме, без единого лишнего символа.")

    raise RuntimeError(f"Kimi не вернул валидный JSON за {ATTEMPTS} попыток ({label}): {last}")


def _tokens(usage) -> int:
    return int(getattr(usage, "total_tokens", 0) or 0) if usage else 0


async def write_two_docx(lead: dict, idx: int, findings: str,
                         d_tmp: str, s_tmp: str, model: str | None = None) -> float:
    """Сделать оба .docx на Kimi. Возвращает стоимость (0.0 — шлюз цену не отдаёт).

    Ретраи и проверку размеров файлов делает вызывающий (orchestrator: 3 попытки по факту
    отсутствия/малого размера .docx) — здесь не дублируем.
    """
    CRA = _cra()
    name = (lead.get("name") or "").strip()
    inn = str(lead.get("_inn") or "").strip()
    api_model = kimi_model(model)
    system = CRA.PRESALE_SYSTEM + SYSTEM_TAIL

    jobs = (
        ("карта бизнес-процессов", CRA.PROCESS_MAP_SCHEMA, PROCESS_EXTRA,
         CRA._write_process_map_docx, d_tmp, "процессы"),
        ("карта ролей и контактов · пресейл", CRA.ROLES_CONTACTS_SCHEMA, ROLES_EXTRA,
         CRA._write_roles_contacts_docx, s_tmp, "роли"),
    )

    client = _client()
    total_tokens = 0
    try:
        for title, schema, extra, render, path, label in jobs:
            payload, usage = await _ask_json(
                client, api_model, system,
                _user_prompt(title, schema, name, inn, findings, extra),
                idx, label)
            total_tokens += _tokens(usage)
            # Рендер ждёт org_name: модель иногда кладёт company/название в другое поле.
            payload.setdefault("org_name", name)
            await asyncio.to_thread(render, dict(payload), path)
            print(f"    [{idx}] → kimi:{label} сохранён ({api_model})")
    finally:
        await client.close()

    print(f"    [{idx}] [kimi] писатель: {total_tokens} токенов ({api_model})")
    return 0.0          # gpllmkeeper цену за вызов не возвращает — считать нечего
