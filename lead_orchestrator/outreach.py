# -*- coding: utf-8 -*-
r"""Outreach-пайплайн: сбор -> адрес ЛПР -> письмо с ящика @tatar.ru.

Порядок стадий держит КОД, а не модель — как и в основном оркестраторе. Модель
работает ровно в одном месте (текст письма), всё остальное детерминировано.

  1. Реестр отработанных   — outreach_registry: кому уже писали и по кому есть материалы
  2. Парсинг RusProfile    — название, сайт, телефоны, ИНН, руководитель, учредители
  3. One-pager по отрасли  — готовый файл из assets/onepagers либо генерация стадией Kimi/Claude
  4. Верификатор ЦИТ РТ    — ПРЕКОНДИШЕН: email_verify --serve на хосте с PTR и SPF
  5. Ящик @tatar.ru        — ПРЕКОНДИШЕН: Outlook Desktop и аккаунт отправителя
  6. Адрес ЛПР             — email_guess (гипотезы по схеме домена) + проверка без отправки
  7. Письмо                — текст моделью, подпись кодом, отправка через Outlook
  8. Проверка прогона      — какая стадия по какой компании пропущена и почему

Стадии 4 и 5 — не шаги цикла, а прекондишены: проверяются ОДИН раз до первой компании
и падают жёсткой ошибкой. Пайплайн, который отработал сбор и подбор адресов, а потом
не смог отправить, потратил время и лимиты RusProfile впустую.

⚠️ По умолчанию письма СОХРАНЯЮТСЯ В ЧЕРНОВИКИ. Реальная отправка — только явным
``--send``. Отправка необратима, а ошибка в шаблоне на сотне компаний стоит домена.

Запуск:
  py outreach.py --industries mining --count 10        # сбор + весь цикл до черновиков
  py outreach.py "D:\лиды\leads_mining.json"           # по готовому JSON лидов
  py outreach.py --check                               # только стадия 8
  py outreach.py --industries mining --count 5 --send --limit 5 --pace 45
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    # Именно ВЫЗОВ, а не голый импорт: сам по себе модуль ничего не делает, и
    # прежний `import project_env` оставлял пайплайн без env/.env — секреты
    # (EMAIL_VERIFIER_URL, EMAIL_MEV_*, OUTREACH_*) молча подменялись дефолтами.
    from project_env import load_project_env
    load_project_env()
except Exception:
    pass

import outreach_registry as REG                     # noqa: E402
import outreach_letter as LETTER                    # noqa: E402
# Транспорт выбирается переменной, а не правкой кода: рассылка может идти как с
# ящика в профиле Outlook, так и напрямую по SMTP с ящика, которого в профиле нет.
# Контракт у модулей одинаковый (check_ready / send_message), стадии не ветвятся.
if (os.environ.get("OUTREACH_TRANSPORT") or "outlook").strip().lower() == "yandex":
    import yandex_send as MAIL                     # noqa: E402
else:
    import outlook_send as MAIL                    # noqa: E402

REPO_ROOT = os.path.dirname(SCRIPTS)
KIMI_DIR = os.environ.get("KIMI_DIR") or os.path.join(REPO_ROOT, "lead_orchestrator_kimi")
ONEPAGER_CLI = os.path.join(KIMI_DIR, "onepager_kimi.py")
ONEPAGER_DIR = (os.environ.get("OUTREACH_ONEPAGER_DIR")
                or os.path.join(SCRIPTS, "assets", "onepagers"))
# Реальный one-pager — сотни КБ; заготовка disk_organize._make_pdf ~10 КБ.
# Порог тот же, что у резюма оркестратора (REAL_PDF_MIN), чтобы «готово» значило
# одно и то же в обоих пайплайнах.
REAL_PDF_MIN = 60_000
ONEPAGER_TIMEOUT = float(os.environ.get("ORQ_ONEPAGER_TIMEOUT", "900"))


def log(message):
    print(message, flush=True)


def _flag(name, default="1"):
    return (os.environ.get(name, default) or "").strip().lower() not in (
        "0", "false", "no", "off", "нет")


# ============================================================ ПРЕКОНДИШЕНЫ ====
def check_verifier(required=True):
    """Стадия 4: верификатор почты на сервере ЦИТ РТ.

    Проверка ящиков идёт SMTP-диалогом до RCPT TO. Приёмная сторона смотрит на PTR
    и SPF отправителя, поэтому с обычной рабочей машины почти всё возвращается как
    «не смогли проверить» (это и показал verify_host_check). Отсюда требование
    гонять пробу с хоста ЦИТ РТ: там PTR и SPF настроены.

    На сервере: py email_verify.py --serve 8080
    Локально:   set EMAIL_VERIFIER_URL=http://<хост>:8080"""
    mev = _check_mev()
    url = (os.environ.get("EMAIL_VERIFIER_URL") or "").strip()
    if not url:
        # Облачный добор — самостоятельный слой: если он поднят, проверять ящики
        # есть чем и без верификатора ЦИТ РТ, просто не для всех компаний.
        if required and not mev["alive"]:
            raise SystemExit(
                "[стадия 4] не задан EMAIL_VERIFIER_URL — проверять ящики неоткуда.\n"
                "  На сервере ЦИТ РТ (там PTR и SPF): py email_verify.py --serve 8080\n"
                "  Здесь: set EMAIL_VERIFIER_URL=http://<хост>:8080\n"
                "  Пропустить осознанно: --no-verify-server (адреса пойдут как гипотезы)")
        if not mev["alive"]:
            log("[стадия 4] верификатор не настроен — адреса пойдут как непроверенные гипотезы")
        return {"url": "", "alive": False, "mev": mev}

    import email_guess as EG
    # Контрольный адрес на домене, который заведомо принимает почту: нам важен не
    # его вердикт, а сам факт, что сервис отвечает. «Ящика нет» — тоже ответ.
    probe = EG.external_verify("postmaster@yandex.ru", url=url)
    unreachable = (probe.get("verdict") == "unknown"
                   and "недоступен" in (probe.get("note") or ""))
    if unreachable:
        raise SystemExit(f"[стадия 4] верификатор {url} не отвечает: {probe.get('note')}")
    log(f"[стадия 4] верификатор: {url} — отвечает")
    return {"url": url, "alive": True, "mev": mev}


def _check_mev():
    """Облачный добор MyEmailVerifier: остаток кредитов и блок-лист — ДО первой компании.

    Не жёсткая ошибка: слой опциональный, и прогон без него просто отдаёт больше
    непроверенных гипотез. Но остаток квоты надо видеть заранее — на 100 бесплатных
    кредитов в сутки прогон на 200 компаний не влезает, и узнать об этом на сотой
    компании хуже, чем до старта."""
    import email_guess as EG

    if not EG.mev_enabled():
        return {"alive": False, "credits": None}
    left, why = EG.mev_credits()
    if left is None:
        log(f"[стадия 4] MyEmailVerifier включён, но баланс не получен: {why} — "
            f"добор работать не будет")
        return {"alive": False, "credits": None}
    policy = EG.mev_policy()
    log(f"[стадия 4] MyEmailVerifier: {left} кредитов, "
        f"суточный лимит {policy['daily_limit']}; не выпускаются отрасли "
        f"{', '.join(policy['deny_industries'])} "
        f"и домены {', '.join(policy['deny_domains'])}")
    return {"alive": True, "credits": left}


def check_mailbox(required=True):
    """Стадия 5: ящик отправителя (Outlook или SMTP — по OUTREACH_TRANSPORT)."""
    error_type = getattr(MAIL, "OutlookError", None) or getattr(MAIL, "YandexSendError")
    try:
        info = MAIL.check_ready()
    except error_type as exc:
        if required:
            raise SystemExit(f"[стадия 5] {exc}") from exc
        log(f"[стадия 5] почта недоступна: {exc}")
        return {"account": "", "alive": False}
    # у Outlook ключ «account», у SMTP — «from»: адрес отправителя один и тот же смысл
    account = info.get("account") or info.get("from") or ""
    log(f"[стадия 5] отправитель: {account}")
    return {"account": account, "alive": True}


# ============================================================== СТАДИЯ 2 ======
def collect_leads(args):
    """Стадия 2: лиды из готового JSON либо свежий сбор с RusProfile.

    В отличие от основного пайплайна карточки открываются РАДИ ЛПР (need_facts) и
    обход не прерывается при закрытых контактах: руководитель и его ИНН лежат в
    бесплатной части карточки, а телефоны — в платной."""
    if args.leads:
        with open(args.leads, "r", encoding="utf-8") as fh:
            leads = json.load(fh)
        leads = [lead for lead in leads if lead.get("name")]
        log(f"[стадия 2] лиды из файла: {len(leads)} — {args.leads}")
        return leads

    import source_rusprofile as RP
    import rusprofile_session as RPS
    from rusprofile_playwright import RusProfilePlaywrightSession

    inds = [s.strip() for s in (args.industries or "").split(",") if s.strip()]
    unknown = [s for s in inds if s not in RP.INDUSTRY]
    if unknown:
        raise SystemExit(f"[стадия 2] неизвестные отрасли: {', '.join(unknown)}. "
                         f"Доступные: {', '.join(sorted(RP.INDUSTRY))}")
    if not os.path.exists(RPS.COOKIES_FILE):
        raise SystemExit(f"[стадия 2] нет cookie RusProfile ({RPS.COOKIES_FILE}) — "
                         f"разовый вход: py rusprofile_session.py --login")

    per_ind = args.per_industry or max(1, args.count // max(1, len(inds)))
    with RusProfilePlaywrightSession(headless=False, offscreen=not args.show_browser) as rs:
        leads = RP.harvest(inds, min_revenue=args.min_revenue, per_industry=per_ind,
                           region=args.region, session=rs)
        res = rs.enrich_leads(leads, only_missing=True, log=log,
                              need_facts=True, stop_when_locked=False,
                              with_founders=args.founders)
    if res.get("locked"):
        log("[стадия 2] ⚠️ профессиональный доступ RusProfile не активен: телефоны, почта "
            "и учредители закрыты. Руководители собраны. Обновить: py rusprofile_session.py --login")
    log(f"[стадия 2] собрано {len(leads)} компаний, ЛПР прочитан у {res.get('facts', 0)}")
    if args.out:
        RP._save(leads, args.out)
        log(f"[стадия 2] сохранено: {args.out}")
    return leads


# ============================================================== СТАДИЯ 3 ======
async def stage_onepager(lead, idx, tmp_dir, generate=False):
    """One-pager: готовый файл по отрасли либо генерация. Возвращает (путь, причина)."""
    industry = (lead.get("_industry") or "").strip()
    for candidate in (f"{industry}.pdf", "default.pdf"):
        path = os.path.join(ONEPAGER_DIR, candidate)
        if os.path.isfile(path) and os.path.getsize(path) > REAL_PDF_MIN:
            return path, ""
    if not generate:
        return "", (f"нет готового one-pager для отрасли «{industry or '—'}» в {ONEPAGER_DIR} "
                    f"(--generate-onepager, чтобы сгенерировать)")
    if not os.path.isfile(ONEPAGER_CLI):
        return "", f"нет стадии генерации: {ONEPAGER_CLI}"

    out = os.path.join(tmp_dir, f"onepager_{idx}.pdf")
    cmd = [sys.executable, ONEPAGER_CLI, lead.get("name") or "", "--out", out]
    cfg = industry_cfg(lead)
    if cfg.get("label"):
        cmd += ["--industry", cfg["label"]]
    if cfg.get("pain"):
        cmd += ["--pain", cfg["pain"]]
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=KIMI_DIR, env=dict(os.environ),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=ONEPAGER_TIMEOUT)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        try:
            proc.kill()                             # иначе висит осиротевший python+chromium
        except Exception:
            pass
        return "", f"генерация one-pager дольше {ONEPAGER_TIMEOUT:g} с"
    if proc.returncode != 0 or not os.path.isfile(out):
        tail = (stdout or b"").decode("utf-8", "replace").strip()[-300:]
        return "", f"генерация one-pager не удалась: {tail or proc.returncode}"
    size = os.path.getsize(out)
    if size < REAL_PDF_MIN:
        return "", f"one-pager подозрительно мал ({size} байт) — не прикладываем"
    return out, ""


# ============================================================== СТАДИЯ 6 ======
def _rank_rows(rows):
    """Кандидаты по убыванию пригодности. Отвергнутые сервером не возвращаются вовсе."""
    good = []
    for row in rows or []:
        verdict = row.get("verdict")
        confidence = row.get("confidence") or ""
        if verdict == "no" or confidence.startswith("НЕ отправлять"):
            continue
        if row.get("scheme") == "опубликован":
            weight = 3                              # опубликован компанией — факт, не гипотеза
        elif verdict == "ok":
            weight = 2                              # подтверждён почтовым сервером
        else:
            weight = 1
        good.append((weight, row.get("score") or 0, row))
    good.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [row for _w, _s, row in good]


def pick_recipient(guess, lead):
    """Выбрать адрес ЛПР. Возвращает dict или None.

    Приоритет — человек, совпавший с руководителем из ЕГРЮЛ: письмо адресное, и
    отправлять его закупщику вместо директора смысла нет."""
    import email_guess as EG

    ceo = (lead.get("contact_person") or "").strip()
    people = list(guess.get("people") or [])
    people.sort(key=lambda p: 0 if (ceo and EG._same_person(p.get("fio", ""), ceo)) else 1)
    for person in people:
        rows = _rank_rows(person.get("emails"))
        if not rows:
            continue
        best = rows[0]
        return {
            "email": best["email"],
            "fio": person.get("fio", ""),
            "position": person.get("position", ""),
            "confidence": best.get("confidence") or "",
            "verdict": best.get("verdict") or "не проверялся",
            "confirmed": best.get("scheme") == "опубликован" or best.get("verdict") == "ok",
            "alternates": [r["email"] for r in rows[1:3]],
        }
    return None


async def stage_email(lead, idx, dry_run=False, use_lead_email=False):
    """Стадия 6: адрес ЛПР — гипотезы по схеме домена + проверка без отправки письма.

    В dry-run ни DNS, ни SMTP, ни обход сайта не выполняются: прогон проверяет
    цепочку стадий, а не доступность чужих серверов (и ничего им не стоит).

    ``use_lead_email`` — брать адрес прямо из выгрузки и не строить гипотез вовсе.
    Нужен, когда почта уже собрана из карточек ЕГРЮЛ: подбор в этом случае не только
    лишний, но и вредный. Он тратит минуты на обход сайта и облачные проверки, а
    затем предлагает РАСЧЁТНЫЙ адрес вместо официального — на боевом прогоне письмо
    ушло на выдуманный `nu@tatneft.ru`, хотя в выгрузке лежал настоящий `tnr@`."""
    if use_lead_email:
        addr = (lead.get("email") or "").strip()
        if not addr:
            return None, "в выгрузке нет адреса, а подбор отключён (--use-lead-email)"
        source = lead.get("_email_src") or "выгрузка"
        picked = {"email": addr, "confirmed": True,
                  "confidence": f"адрес из выгрузки ({source})"}
        log(f"    [{idx}] адрес: {addr} — из выгрузки, подбор пропущен")
        return picked, ""

    import email_guess as EG

    online = not dry_run
    guess = await asyncio.to_thread(
        EG.guess_for_company, lead,
        check_mx=online, smtp=online, per_person=3,
        use_site=online and _flag("EMAIL_GUESS_SITE"),
        log=lambda *a: None)
    picked = pick_recipient(guess, lead)
    if not picked:
        note = "; ".join(guess.get("notes") or []) or "адресов не построено"
        return None, f"адрес ЛПР не найден ({note[:120]})"
    log(f"    [{idx}] адрес: {picked['email']} — {picked['confidence'][:60]}")
    return picked, ""


# ============================================================== СТАДИЯ 7 ======
def industry_cfg(lead):
    """Карточка отрасли (label/pain/offer) из карты сбора — или пустой dict.

    Импорт ленивый и под except: при работе по готовому JSON лидов Фаза 1 не нужна
    вовсе, а тянуть ради трёх строк источник с playwright — нет."""
    try:
        import source_rusprofile as RP
        return RP.INDUSTRY.get((lead.get("_industry") or "").strip()) or {}
    except Exception:                               # noqa: BLE001 — карта отраслей не критична
        return {}


def check_recipient(address, lead):
    """Стадия 6б: годится ли адрес для делового письма. -> (ok, причина).

    Появилась после боевого прогона: письмо ушло на `corruption@` — ящик для
    сообщений о коррупции. Адрес был взят из официальной карточки ЕГРЮЛ и
    формально безупречен, но канал не тот: коммерческое предложение там читать
    никто не будет, а выглядит оно неуместно.

    Проверяем три вещи, каждая отсеивает свой класс брака:
      * синтаксис — адрес с опечаткой не доставится;
      * КАНАЛ — комплаенс, кадры, техподдержка, роботы: письмо уйдёт не тем людям;
      * домен принимает почту (MX) — иначе гарантированный отбойник, а отбойники
        с нового ящика портят его репутацию сильнее, чем польза от попытки.

    Существование самого ящика тут не проверяется: для этого нужна SMTP-сессия
    с хоста с корректными PTR/SPF, и это отдельная стадия (email_verify)."""
    import checko_enrich as CE
    import email_verify as EV

    address = (address or "").strip()
    syntax = EV.check_syntax(address)
    if not syntax["is_valid_syntax"]:
        return False, f"адрес не проходит синтаксическую проверку: {address}"

    kind, _is_target = CE._classify_email(address)
    if kind == "служебная":
        return False, (f"адрес «{address}» — служебный канал (техподдержка, комплаенс, "
                       f"кадры или робот): деловое письмо туда адресовать нельзя")

    mx = EV.check_mx(syntax["domain"])
    if mx.get("_state") is False:
        return False, f"домен {syntax['domain']} не принимает почту (нет MX)"
    if mx.get("_state") is None:
        # молчание DNS — это «не проверили», а не «домена нет»: отправляем, но
        # честно помечаем в реестре, чтобы отбойник потом не выглядел загадкой
        return True, f"домен {syntax['domain']} не проверен: DNS не ответил"
    return True, ""


def build_recipients(picked, lead):
    """Кому уходит письмо. Возвращает (to, cc, пометка).

    Неподтверждённая гипотеза дублируется на общую почту компании: так письмо
    дойдёт даже если схема адреса угадана неверно, и это честнее, чем выдавать
    расчётный адрес за найденный."""
    to = picked["email"]
    general = (lead.get("email") or "").strip()
    if picked["confirmed"] or not general or general.lower() == to.lower():
        return to, "", ""
    return to, general, f"копия на общую почту {general} — адрес ЛПР расчётный"


async def stage_letter(lead, idx, onepager="", dry_run=False):
    """Стадия 7а: текст письма.

    ``onepager`` нужен не для отправки, а для промпта: без файла модели прямо
    запрещается писать «во вложении» — иначе получатель ищет несуществующий файл."""
    if dry_run:
        return {"subject": f"[dry-run] {lead.get('name')}",
                "body": "Заглушка dry-run.\n\n" + LETTER.signature_block()}, ""
    cfg = industry_cfg(lead)
    try:
        letter = await LETTER.write_letter(
            lead, pain=cfg.get("pain", ""), industry_cfg=cfg,
            has_attachment=bool(onepager), log=lambda m: log(f"  {m}"))
    except LETTER.LetterError as exc:
        return None, f"письмо не написано: {exc}"
    except Exception as exc:                        # noqa: BLE001 — модель не должна валить прогон
        return None, f"письмо не написано: {type(exc).__name__}: {str(exc)[:100]}"
    log(f"    [{idx}] тема: {letter['subject']}")
    return letter, ""


def stage_send(lead, idx, picked, letter, onepager, send=False, dry_run=False):
    """Стадия 7б: черновик или отправка. Единственное необратимое действие пайплайна."""
    to, cc, note = build_recipients(picked, lead)
    msg = {"to": to, "cc": cc, "subject": letter["subject"], "body": letter["body"],
           "attachments": [onepager] if onepager else []}
    if dry_run:
        return {"action": "dry-run", "to": to, "subject": letter["subject"]}, note
    try:
        res = MAIL.send_message(msg, draft=not send)
    except (MAIL.OutlookError, ValueError) as exc:
        return None, f"отправка не удалась: {exc}"
    log(f"    [{idx}] [{res['action']}] -> {res['to']}"
        + (f" (копия {cc})" if cc else "")
        + (f", вложений {res['attachments']}" if res["attachments"] else ", без вложения"))
    return res, note


# ============================================================== ОБРАБОТКА =====
# Как называется остановка в логе и чем она оборачивается для счётчиков прогона.
_STOP = {REG.SKIP: ("пропуск", "skip"), REG.FAIL: ("сбой", "fail")}


async def process(lead, idx, reg, args, tmp_dir):
    """Одна компания: стадии 3, 6, 7. Сбой любой из них не валит прогон."""
    inn = str(lead.get("_inn") or "").strip()
    log(f"[{idx}] {lead.get('name') or ''} (ИНН {inn or '—'})")
    if not inn:
        log("    пропуск: у лида нет ИНН — в реестр писать нечего")
        return "skip"
    reg.upsert(lead)

    def stop(stage, status, reason):
        """Остановиться на стадии. Причина уходит и в реестр (её читает стадия 8),
        и в лог — и это ОДНА причина: два разных объяснения одного пропуска
        расходились бы по формулировкам."""
        word, outcome = _STOP[status]
        reg.mark(inn, stage, status, reason)
        reg.save()
        log(f"    {word}: {reason}")
        return outcome

    # стадия 2 засчитывается по факту наличия адресата
    if not lead.get("contact_person"):
        return stop("parsed", REG.SKIP, "руководитель не определён — писать некому")
    reg.mark(inn, "parsed", REG.OK)

    onepager, why = await stage_onepager(lead, idx, tmp_dir, generate=args.generate_onepager)
    reg.mark(inn, "onepager", REG.OK if onepager else REG.SKIP, "" if onepager else why)
    if not onepager:
        log(f"    one-pager: {why}")

    picked, why = await stage_email(lead, idx, dry_run=args.dry_run,
                                    use_lead_email=getattr(args, "use_lead_email", False))
    if not picked:
        return stop("email", REG.SKIP, why)

    # Стадия 6б: проверка ДО генерации письма — незачем платить модели за текст,
    # который уйдёт в комплаенс-ящик или на домен без MX.
    if not args.dry_run:
        ok, note = check_recipient(picked["email"], lead)
        if not ok:
            log(f"    [{idx}] адрес отклонён: {note}")
            return stop("email", REG.SKIP, note)
        if note:
            log(f"    [{idx}] предупреждение: {note}")
    else:
        note = ""

    reg.mark(inn, "email", REG.OK,
             note or ("" if picked["confirmed"] else "адрес расчётный, не подтверждён"))

    letter, why = await stage_letter(lead, idx, onepager=onepager, dry_run=args.dry_run)
    if not letter:
        return stop("letter", REG.FAIL, why)
    reg.mark(inn, "letter", REG.OK)

    res, note = stage_send(lead, idx, picked, letter, onepager,
                           send=args.send, dry_run=args.dry_run)
    if not res:
        return stop("sent", REG.FAIL, note)
    if args.dry_run:
        reg.mark(inn, "sent", REG.SKIP, "dry-run: письмо не создавалось")
    else:
        reg.mark_sent(inn, res["to"], res["subject"], draft=not args.send, note=note)
    reg.save()
    return "ok"


async def run(args):
    reg = REG.Registry(args.registry)
    log(f"[стадия 1] реестр: {reg.path}")
    if args.check:
        print(REG.format_audit(reg.audit()))
        return 0
    reg.backfill_deliverables(log=log)
    reg.save()

    leads = collect_leads(args)
    if not args.redo:
        before = len(leads)
        leads = reg.filter_new(leads)
        if before != len(leads):
            log(f"[стадия 1] отсеяно уже отработанных: {before - len(leads)}")
    if args.limit:
        leads = leads[:args.limit]
    if not leads:
        log("[стадия 1] новых компаний нет — работать не над чем")
        print(REG.format_audit(reg.audit()))
        return 0
    log(f"[стадия 1] к обработке: {len(leads)}")

    if not args.dry_run:
        check_verifier(required=not args.no_verify_server)
        check_mailbox(required=True)
    if args.send:
        log(f"⚠️ РЕАЛЬНАЯ ОТПРАВКА включена: до {len(leads)} писем, пауза {args.pace:g} с")
    else:
        log("режим черновиков: письма лягут в «Черновики» Outlook (--send — отправить)")

    tmp_dir = os.path.join(REG._work_base("orq_tmp"), f"outreach_{int(time.time())}")
    os.makedirs(tmp_dir, exist_ok=True)
    stats = {"ok": 0, "skip": 0, "fail": 0}
    for idx, lead in enumerate(leads, 1):
        try:
            outcome = await process(lead, idx, reg, args, tmp_dir)
        except KeyboardInterrupt:
            raise
        except Exception as exc:                    # noqa: BLE001 — одна компания не валит прогон
            log(f"    [{idx}] сбой компании: {type(exc).__name__}: {str(exc)[:140]}")
            outcome = "fail"
        stats[outcome] += 1
        if outcome == "ok" and idx < len(leads) and args.pace > 0 and not args.dry_run:
            await asyncio.sleep(args.pace)

    log(f"\n[итог] писем: {stats['ok']} | пропущено: {stats['skip']} | сбоев: {stats['fail']}")
    print(REG.format_audit(reg.audit()))
    return 0 if stats["ok"] or not stats["fail"] else 3


def main():
    ap = argparse.ArgumentParser(
        description="outreach: сбор -> адрес ЛПР -> письмо с ящика @tatar.ru")
    ap.add_argument("leads", nargs="?", default=None, help="готовый JSON лидов")
    ap.add_argument("--industries", default=None, help="ключи отраслей через запятую")
    ap.add_argument("--count", type=int, default=20, help="сколько компаний ВСЕГО (20)")
    ap.add_argument("--per-industry", dest="per_industry", type=int, default=None)
    ap.add_argument("--min-revenue", dest="min_revenue", type=float, default=1e9)
    ap.add_argument("--region", default=None)
    ap.add_argument("--out", default=None, help="куда сохранить собранные лиды")
    ap.add_argument("--show-browser", dest="show_browser", action="store_true")
    ap.add_argument("--founders", action="store_true",
                    help="дополнительно открывать страницу учредителей (ещё один переход)")
    ap.add_argument("--generate-onepager", dest="generate_onepager", action="store_true",
                    help="генерировать one-pager, если готового по отрасли нет")
    ap.add_argument("--send", action="store_true",
                    help="ОТПРАВИТЬ письма (по умолчанию — черновики)")
    ap.add_argument("--limit", type=int, default=None, help="максимум компаний за прогон")
    ap.add_argument("--pace", type=float, default=30.0, help="пауза между письмами, с (30)")
    ap.add_argument("--redo", action="store_true", help="не отсеивать уже отработанных")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="без модели и без Outlook — проверить цепочку")
    ap.add_argument("--use-lead-email", dest="use_lead_email", action="store_true",
                    help="брать адрес прямо из выгрузки, не строить гипотез: почта уже "
                         "собрана из карточек ЕГРЮЛ, а подбор предложит расчётный адрес")
    ap.add_argument("--no-verify-server", dest="no_verify_server", action="store_true",
                    help="работать без верификатора ЦИТ РТ (адреса — непроверенные гипотезы)")
    ap.add_argument("--check", action="store_true", help="стадия 8: только отчёт по реестру")
    ap.add_argument("--registry", default=None, help="путь к файлу реестра")
    a = ap.parse_args()

    if not (a.leads or a.industries or a.check):
        ap.error("нужен JSON лидов, --industries или --check")
    if a.send and a.dry_run:
        ap.error("--send и --dry-run несовместимы")
    try:
        return asyncio.run(run(a))
    except KeyboardInterrupt:
        log("\n[прервано] реестр сохранён — повторный запуск продолжит с этого места")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
