# -*- coding: utf-8 -*-
r"""Текст письма ЛПР — стадия 7 outreach-пайплайна.

Разделение ответственности здесь такое же, как в one-pager, и по той же причине:
**модель пишет только осмысленный текст, а всё проверяемое дописывает код.**
Подпись, телефон, почта, реквизиты и строка об отказе от рассылки собираются из
констант — в прошлых прогонах модель выдумывала живым людям регалии и контакты,
и в письме это стоило бы дороже, чем в презентации.

Модели отдаются ТОЛЬКО факты, которые пайплайн уже проверил: название, отрасль,
ОКВЭД, регион, выручка, ФИО и должность адресата, типовая боль и оффер отрасли из
карты ``source_rusprofile.INDUSTRY``. Тулов у агента нет — то, что он «донайдёт» сам,
попало бы в письмо живому человеку без проверки.

Ответ модели — строгий JSON ``{"subject", "body"}``; разбор детерминированный.

Отладка (без обращения к модели):
  py outreach_letter.py --demo         # показать промпт и собранное письмо на заглушке
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import email_guess as EG                            # noqa: E402 — split_fio/is_person

# lead_orchestrator/app -> корень репозитория на два уровня выше.
KIMI_DIR = os.environ.get("KIMI_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(SCRIPTS)), "lead_orchestrator_kimi")
AGENT_FILE = os.path.join(KIMI_DIR, "kimi_agent", "outreach_letter.yaml")

# Подписывает письмо один человек, а связываться предлагается с двумя: адресат
# должен видеть, кому звонить, независимо от того, кто нажал «отправить».
SIGNER_NAME = os.environ.get("OUTREACH_SIGNER_NAME") or "Булат Замалиев"
SIGNER_POST = os.environ.get("OUTREACH_SIGNER_POST") or ""
# Первый контакт — тот же, что напечатан в one-pager (VENDOR_* в onepager_kimi):
# в письме и во вложении обязан стоять один и тот же телефон.
CONTACT1_NAME = os.environ.get("OUTREACH_CONTACT1_NAME") or "Шабанов Али Магомедович"
CONTACT1_EMAIL = os.environ.get("OUTREACH_CONTACT1_EMAIL") or "Ali.Shabanov@tatar.ru"
CONTACT1_PHONE = os.environ.get("OUTREACH_CONTACT1_PHONE") or "+7 917 876 7741"
# Второй — ящик, с которого письмо уходит: ответ адресата придёт именно сюда,
# и он должен видеть, чьё это имя.
CONTACT2_NAME = os.environ.get("OUTREACH_CONTACT2_NAME") or "Байрашев Артур"
CONTACT2_EMAIL = os.environ.get("OUTREACH_CONTACT2_EMAIL") or "Artur.Bayrashev@tatar.ru"
CONTACT2_PHONE = os.environ.get("OUTREACH_CONTACT2_PHONE") or "+7 986 846 8748"
VENDOR_NAME = "АО «ЦИТ РТ»"
VENDOR_SITE = "citrt.ru"
VENDOR_REQUISITES = "ИНН 1655505808 · ОГРН 1241600056829"

# Строка отказа обязательна и добавляется КОДОМ, а не моделью: без явного и рабочего
# способа отказаться холодное письмо превращается в спам-рассылку и по смыслу
# ст. 18 ФЗ «О рекламе», и по мнению получателя.
OPT_OUT = ("Если такие письма вам не нужны — ответьте одним словом «отписаться», "
           "и мы больше не напишем.")

SUBJECT_MAX = 120
BODY_MIN, BODY_MAX = 300, 2600

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_ANY_EMAIL = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.I)
_ANY_PHONE = re.compile(r"(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")


class LetterError(RuntimeError):
    """Модель не вернула пригодного письма."""


# ----------------------------------------------------------- чистая логика ----
def salutation(fio):
    """«Иванов Сергей Александрович» -> «Сергей Александрович».

    Отчество без имени и имя без отчества не годятся: «Сергей,» от незнакомого
    отправителя читается фамильярно, а «Александрович,» — просто странно.
    Не разобрали — здороваемся без имени, это честнее выдуманного склонения."""
    parts = EG.split_fio(fio or "")
    if not parts:
        return ""
    if parts.get("name") and parts.get("patronymic"):
        return f"{parts['name']} {parts['patronymic']}"
    return ""


def _money(value):
    """Выручка -> «4,3 млрд ₽». Нечисло и меньше миллиарда — пустая строка: в промпт
    идут только величины, которые пайплайн подтвердил (порог отбора и так 1 млрд)."""
    try:
        billions = float(value) / 1e9
    except (TypeError, ValueError):
        return ""
    if billions < 1:
        return ""
    return f"{billions:.1f} млрд ₽".replace(".", ",")


def build_prompt(lead, pain="", industry_cfg=None, has_attachment=True):
    """Факты для модели. Ровно те, что проверены — ни одного «примерно» и «вероятно».

    ``has_attachment`` обязателен: one-pager по отрасли может отсутствовать, а фраза
    «во вложении — описание платформы» в письме БЕЗ вложения выглядит как ошибка
    отправителя и провоцирует ответ «а где файл?»."""
    cfg = industry_cfg or {}
    fio = (lead.get("contact_person") or "").strip()
    person_ok = EG.is_person(fio)
    rows = [
        ("Компания", lead.get("name") or ""),
        ("ИНН", str(lead.get("_inn") or "")),
        ("Отрасль", cfg.get("label") or lead.get("_industry") or ""),
        ("Основной вид деятельности", lead.get("_okved_descr") or ""),
        ("Регион", lead.get("_region") or ""),
        ("Годовая выручка", _money(lead.get("_revenue"))),
        ("Адресат — ФИО", fio if person_ok else ""),
        ("Адресат — должность", lead.get("_ceo_post") or ""),
        ("Обращение", salutation(fio) if person_ok else ""),
        ("Типовая боль отрасли", pain or cfg.get("pain") or lead.get("pain") or ""),
        ("Что предлагаем по отрасли", cfg.get("offer") or lead.get("offer") or ""),
    ]
    known = "\n".join(f"- {key}: {value}" for key, value in rows if value)
    unknown = [key for key, value in rows if not value]
    if has_attachment:
        attachment = ("есть — одной нейтральной фразой упомяни, что во вложении "
                      "короткое описание платформы на одну страницу.")
    else:
        attachment = "НЕТ — не упоминай вложение, файл, презентацию и «во вложении»."
    lines = ["Напиши письмо по этим фактам.", "", "ИЗВЕСТНО:", known,
             "", "ВЛОЖЕНИЕ: " + attachment]
    if unknown:
        lines += ["", "НЕ ИЗВЕСТНО (не упоминай и не домысливай): " + ", ".join(unknown)]
    if not person_ok and fio:
        lines += ["", f"Поле «руководитель» содержит «{fio}» — это не ФИО человека "
                      f"(управляющая организация или должность). Обращайся без имени."]
    return "\n".join(lines)


def _strip_contacts(body):
    """Убрать строки с телефоном/почтой, если модель всё-таки подписалась.

    Промпт это запрещает, но нарушение стоит дорого: в письме оказались бы два
    разных набора контактов — выдуманный моделью и настоящий из подписи."""
    kept = []
    for line in body.split("\n"):
        if _ANY_EMAIL.search(line) or _ANY_PHONE.search(line):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def parse_reply(text):
    """Ответ модели -> {"subject", "body"}. Бросает LetterError, если письма нет."""
    raw = (text or "").strip()
    fence = _JSON_FENCE.search(raw)
    if fence:
        raw = fence.group(1)
    else:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
    try:
        data = json.loads(raw)
    except Exception as exc:                       # noqa: BLE001 — любой мусор = нет письма
        raise LetterError(f"ответ модели не разобран как JSON: {str(exc)[:80]}") from exc
    if not isinstance(data, dict):
        raise LetterError("модель вернула не объект")
    subject = " ".join(str(data.get("subject") or "").split()).strip()
    body = str(data.get("body") or "").replace("\r", "").strip()
    if not subject:
        raise LetterError("в ответе нет темы письма")
    if len(subject) > SUBJECT_MAX:
        raise LetterError(f"тема длиннее {SUBJECT_MAX} знаков: {len(subject)}")
    body = _strip_contacts(body)
    if not BODY_MIN <= len(body) <= BODY_MAX:
        raise LetterError(f"длина письма {len(body)} вне диапазона {BODY_MIN}–{BODY_MAX}")
    return {"subject": subject, "body": body}


def signature_block():
    """Подпись и отказ — детерминированно, из констант.

    Два контакта, а не один: письмо уходит с ящика Байрашева (туда придёт ответ),
    а в приложенном one-pager напечатан телефон Шабанова. Один контакт в подписи
    означал бы, что адресат звонит одному, пишет другому и не понимает, кто есть кто."""
    lines = ["—", SIGNER_NAME]
    if SIGNER_POST:
        lines.append(SIGNER_POST)
    lines += [
        VENDOR_NAME,
        "",
        "Контакты для связи:",
        f"{CONTACT1_NAME} · тел. {CONTACT1_PHONE} · {CONTACT1_EMAIL}",
        f"{CONTACT2_NAME} · тел. {CONTACT2_PHONE} · {CONTACT2_EMAIL}",
        VENDOR_SITE,
        VENDOR_REQUISITES,
        "",
        OPT_OUT,
    ]
    return "\n".join(lines)


def assemble(letter):
    """Тело модели + подпись кода -> итоговое письмо."""
    return {"subject": letter["subject"],
            "body": letter["body"].rstrip() + "\n\n" + signature_block()}


# ------------------------------------------------------------------ модель ----
async def write_letter(lead, pain="", industry_cfg=None, model=None, log=print,
                       has_attachment=True):
    """Сходить к модели за письмом. Возвращает {"subject", "body"} уже с подписью."""
    try:
        import kimi_config as KC
        runtime = KC.runtime()
    except Exception:                              # noqa: BLE001 — нет конфига = дефолт ветки
        runtime = "claude"
    if runtime != "claude":
        raise LetterError(
            "стадия письма реализована для claude-runtime; для kimi запусти прогон с "
            "ORQ_LLM_RUNTIME=claude либо добавь подпроцесс в .venv_kimi")
    if KIMI_DIR not in sys.path:
        sys.path.insert(0, KIMI_DIR)
    try:
        import claude_kimi_adapter as adapter
    except ImportError as exc:
        raise LetterError(f"адаптер Claude SDK недоступен: {str(exc)[:90]}") from exc

    prompt = build_prompt(lead, pain=pain, industry_cfg=industry_cfg,
                          has_attachment=has_attachment)
    chunks = []
    async for message in adapter.prompt(
            prompt, model=model or os.environ.get("ORQ_LETTER_MODEL") or "sonnet",
            agent_file=AGENT_FILE, final_message_only=True):
        try:
            text = message.extract_text() or ""
        except Exception:                          # noqa: BLE001 — служебные сообщения без текста
            continue
        if text:
            chunks.append(text)
    reply = "\n".join(chunks).strip()
    if not reply:
        raise LetterError("модель не вернула текста")
    log(f"    письмо: получено {len(reply)} знаков ответа")
    return assemble(parse_reply(reply))


def _demo_lead():
    return {
        "name": "ООО «Регион-Нефть»", "_inn": "3443933330",
        "_industry": "mining", "_region": "Волгоградская область",
        "_okved_descr": "Торговля оптовая твердым, жидким и газообразным топливом",
        "_revenue": 4_300_000_000,
        "contact_person": "Гребнева Татьяна Николаевна",
        "_ceo_post": "Генеральный директор",
    }


def main():
    ap = argparse.ArgumentParser(description="письмо ЛПР: промпт и сборка")
    ap.add_argument("--demo", action="store_true",
                    help="показать промпт и подпись на заглушке (единственный режим CLI)")
    ap.parse_args()
    print("=== ПРОМПТ ===")
    print(build_prompt(_demo_lead(), pain="Аварийность и простои оборудования"))
    print("\n=== ПОДПИСЬ (дописывает код) ===")
    print(signature_block())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
