# -*- coding: utf-8 -*-
"""ССЧ (среднесписочная численность) лидов CRM: свод, дозаполнение, чистка по порогу.

Показатель — RusProfile (данные ФНС). Источники по возрастанию цены:
  1. партии ФАЗЫ 1 в `ORQ_LEADS_DIR` (`_staff_count`/`_staff_year`, сняты с карточки RusProfile);
  2. кэш прежних прогонов этого скрипта — `<ORQ_DATA_ROOT>/orq_cache/staff_index.json`;
  3. RusProfile по ИНН: `card_by_inn` (advanced-search XHR, поле `sshr`), а если XHR
     показателя за нужный год не отдал — сама карточка (`card_facts_by_url`).

Вердикт — правило отбора лидгена (`source_rusprofile.staff_verdict`, порог `LEAD_MIN_STAFF`):
  ok        — ССЧ за 2025 не ниже порога;
  below     — ниже порога;
  unknown   — показателя за 2025 нет (в том числе есть только за более ранний год);
  not_found — RusProfile не знает ДЕЙСТВУЮЩЕЙ компании с таким ИНН (ликвидирована и т.п.).

    py crm_staff_sync.py                      # свод + отчёт; CRM не меняется
    py crm_staff_sync.py --push               # проставить ССЧ в CRM (PUT /ingest/staff)
    py crm_staff_sync.py --purge              # удалить из CRM «below» (POST /ingest/purge)
    py crm_staff_sync.py --purge --purge-unknown   # ...и «unknown»/«not_found» тоже
    py crm_staff_sync.py --no-rusprofile      # только партии и кэш, без браузера

Удаление необратимо: только по явному флагу и по умолчанию лишь подтверждённое «below».
Что трогать нельзя (ответ, звонок, отказ, сделка, ручной лид) — решает CRM и возвращает
в «пропущено» с причиной; здесь это печатается, а не переспрашивается."""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import crm_push  # noqa: E402
import source_rusprofile as SR  # noqa: E402

VERDICTS = ("ok", "below", "unknown", "not_found")


def leads_dir():
    return os.environ.get("ORQ_LEADS_DIR") or r"D:\лиды"


def cache_path():
    """Тот же корень служебных папок, что у реестра рассылки и кэша MEV."""
    from outreach_registry import _work_base
    return os.path.join(_work_base("orq_cache"), "staff_index.json")


def cache_ttl_days():
    try:
        return max(0, int(os.environ.get("STAFF_INDEX_TTL_D", "30") or 30))
    except ValueError:
        return 30


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ источники --
def staff_from_leads_dir(path):
    """{ИНН: {count, year, name, source}} по всем JSON-партиям в папке.

    Одна компания в нескольких партиях — берётся значение за самый свежий год;
    партия без ССЧ у компании имя всё равно даёт (для отчёта)."""
    found = {}
    for file in sorted(glob.glob(os.path.join(path, "*.json"))):
        try:
            with open(file, "r", encoding="utf-8") as fh:
                rows = json.load(fh)
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            inn = crm_push._digits(row.get("_inn") or row.get("inn"))
            if not crm_push._valid_org_inn(inn):
                continue
            count = SR.integer_value(row.get("_staff_count"))
            year = SR.integer_value(row.get("_staff_year"))
            name = str(row.get("name") or "").strip()
            entry = found.get(inn)
            if entry is None:
                found[inn] = {"count": count, "year": year, "name": name,
                              "source": os.path.basename(file)}
                continue
            if name and not entry.get("name"):
                entry["name"] = name
            if count is not None and (
                    entry["count"] is None or (year or 0) > (entry["year"] or 0)):
                entry.update(count=count, year=year, source=os.path.basename(file))
    return found


def load_cache(path=None):
    path = path or cache_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cache(cache, path=None):
    path = path or cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def _fresh(entry, ttl_days):
    try:
        when = datetime.strptime(str((entry or {}).get("checked_at") or ""),
                                 "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - when).days < ttl_days


def best_known(inn, local, cache):
    """Лучшее известное значение без сети: есть численность > нет, свежий год > старый.

    Кэш «RusProfile не нашёл» проигрывает любой численности из партии — «не нашлось
    сегодня» не отменяет показатель, снятый с карточки раньше."""
    local_entry = local.get(inn)
    cached = cache.get(inn)
    candidates = []
    if local_entry and local_entry.get("count") is not None:
        candidates.append(dict(local_entry, found=True))
    if cached:
        candidates.append(dict(cached))
    if not candidates:
        return None
    best = max(candidates, key=lambda e: (e.get("count") is not None, e.get("year") or 0))
    if not best.get("name"):
        best["name"] = (local_entry or {}).get("name") or (cached or {}).get("name") or ""
    return best


def needs_lookup(inn, local, cache, ttl_days):
    """В RusProfile идём, только если показателя за STAFF_YEAR нет ниоткуда и
    свежий кэш не говорит, что RusProfile уже спрашивали."""
    best = best_known(inn, local, cache)
    if best and best.get("count") is not None and best.get("year") == SR.STAFF_YEAR:
        return False
    return not _fresh(cache.get(inn), ttl_days)


def verdict_for(entry, min_staff):
    if entry is None:
        return "unknown"
    if entry.get("found") is False and entry.get("count") is None:
        return "not_found"
    return SR.staff_verdict(
        {"_staff_count": entry.get("count"), "_staff_year": entry.get("year")}, min_staff)


def lookup_rusprofile(session, inn, *, log=print):
    """ССЧ по ИНН из RusProfile -> {count, year, name, found, source} | None при сбое.

    Сначала advanced-search XHR (`sshr`/`sshr_year` — без открытия страницы); если
    там показателя за STAFF_YEAR нет, открывается карточка. Сбой сети/страницы —
    None и НЕ кэшируется: временная ошибка не должна на месяц стать «неизвестно»."""
    try:
        item = session.card_by_inn(inn)
    except Exception as exc:  # noqa: BLE001 — один ИНН не валит свод
        log(f"  [warn] RusProfile {inn}: {type(exc).__name__}: {str(exc)[:120]}")
        return None
    if not item:
        return {"count": None, "year": None, "name": "", "found": False,
                "source": "rusprofile:none"}
    name = str(item.get("name") or item.get("raw_name") or "").strip()
    count = SR.integer_value(item.get("sshr"))
    year = SR.integer_value(item.get("sshr_year"))
    source = "rusprofile:xhr"
    link = item.get("link") or item.get("url")
    if (count is None or year != SR.STAFF_YEAR) and link and hasattr(session, "card_facts_by_url"):
        try:
            facts = session.card_facts_by_url(link, expected_inn=inn) or {}
        except Exception as exc:  # noqa: BLE001
            log(f"  [warn] карточка {inn}: {type(exc).__name__}")
            facts = {}
        card_count = SR.integer_value(facts.get("_staff_count"))
        card_year = SR.integer_value(facts.get("_staff_year"))
        if card_count is not None and (card_year or 0) >= (year or 0):
            count, year, source = card_count, card_year, "rusprofile:card"
    return {"count": count, "year": year, "name": name, "found": True, "source": source}


# ------------------------------------------------------------------ свод --
def build_rows(inns, local, cache, min_staff):
    """{ИНН: {name, count, year, source, verdict}} — итог для отчёта, push и purge."""
    rows = {}
    for inn in inns:
        entry = best_known(inn, local, cache)
        rows[inn] = {
            "name": (entry or {}).get("name") or (local.get(inn) or {}).get("name") or "",
            "count": (entry or {}).get("count"),
            "year": (entry or {}).get("year"),
            "source": (entry or {}).get("source") or "",
            "verdict": verdict_for(entry, min_staff),
        }
    return rows


def purge_targets(rows, *, include_unknown=False):
    kinds = {"below"} | ({"unknown", "not_found"} if include_unknown else set())
    return [inn for inn, row in rows.items() if row["verdict"] in kinds]


def main(argv=None):
    ap = argparse.ArgumentParser(description="ССЧ лидов CRM: свод, --push, --purge")
    ap.add_argument("--push", action="store_true", help="проставить ССЧ в CRM (PUT /ingest/staff)")
    ap.add_argument("--purge", action="store_true",
                    help="удалить из CRM лиды с вердиктом below (POST /ingest/purge)")
    ap.add_argument("--purge-unknown", action="store_true",
                    help="вместе с --purge удалять и unknown/not_found")
    ap.add_argument("--no-rusprofile", action="store_true",
                    help="не ходить в RusProfile: только партии JSON и кэш")
    ap.add_argument("--pace", type=float, default=1.0, help="пауза между запросами RusProfile, с")
    ap.add_argument("--limit", type=int, default=0,
                    help="не больше N обращений к RusProfile за прогон (0 = все)")
    ap.add_argument("--show-browser", action="store_true", help="видимое окно Chromium")
    ap.add_argument("--min-staff", type=int, default=None, help="порог (дефолт LEAD_MIN_STAFF=50)")
    ap.add_argument("--report", default=None,
                    help="куда писать отчёт (дефолт <ORQ_LEADS_DIR>/staff_report.json)")
    a = ap.parse_args(argv)

    import project_env
    project_env.load_project_env()
    min_staff = SR.lead_min_staff() if a.min_staff is None else max(0, int(a.min_staff))
    if min_staff <= 0:
        raise SystemExit("порог ССЧ выключен (0) — сводить не по чему")
    print(f"[ССЧ] порог отбора: >={min_staff} чел. за {SR.STAFF_YEAR}")

    try:
        index = crm_push.fetch_existing_leads()
    except crm_push.CRMIndexError as exc:
        raise SystemExit(f"CRM: {exc}") from None
    inns = sorted(index.inns)
    print(f"[CRM] лидов с ИНН: {len(inns)} (всего записей {index.total})")

    local = staff_from_leads_dir(leads_dir())
    cache = load_cache()
    ttl = cache_ttl_days()
    pending = [inn for inn in inns if needs_lookup(inn, local, cache, ttl)]
    from_json = sum(1 for inn in inns if (local.get(inn) or {}).get("count") is not None)
    print(f"[свод] ССЧ из партий JSON: {from_json} | в кэше: "
          f"{sum(1 for inn in inns if inn in cache)} | нужен RusProfile: {len(pending)}")

    if pending and a.no_rusprofile:
        print(f"[RusProfile] пропущено по --no-rusprofile: {len(pending)} ИНН останутся без ССЧ")
    elif pending:
        if a.limit:
            pending = pending[:a.limit]
        from rusprofile_playwright import RusProfilePlaywrightSession
        done = 0
        with RusProfilePlaywrightSession(offscreen=not a.show_browser, log_fn=print) as session:
            for inn in pending:
                got = lookup_rusprofile(session, inn, log=print)
                if got is not None:
                    got["checked_at"] = _now()
                    cache[inn] = got
                done += 1
                if done % 25 == 0:
                    save_cache(cache)
                    print(f"  [RusProfile] {done}/{len(pending)}")
                time.sleep(max(0.0, a.pace))
        save_cache(cache)
        print(f"[RusProfile] опрошено {done}; кэш: {cache_path()}")

    rows = build_rows(inns, local, cache, min_staff)
    counts = collections.Counter(row["verdict"] for row in rows.values())
    print("[вердикты] " + " | ".join(f"{kind} {counts.get(kind, 0)}" for kind in VERDICTS))
    report = a.report or os.path.join(leads_dir(), "staff_report.json")
    os.makedirs(os.path.dirname(report), exist_ok=True)
    with open(report, "w", encoding="utf-8") as fh:
        json.dump({"min_staff": min_staff, "year": SR.STAFF_YEAR, "generated_at": _now(),
                   "rows": rows}, fh, ensure_ascii=False, indent=1)
    print(f"[отчёт] {report}")
    below = sorted(((row["count"], inn, row["name"]) for inn, row in rows.items()
                    if row["verdict"] == "below"), key=lambda t: (t[0], t[1]))
    for count, inn, name in below[:30]:
        print(f"  below: {count:>4} чел. | {inn} | {name}")
    if len(below) > 30:
        print(f"  ... ещё {len(below) - 30} в отчёте")

    exit_code = 0
    if a.push:
        rows_push = [(inn, row["count"], row["year"]) for inn, row in rows.items()
                     if row["count"] is not None]
        updated, missing, why = crm_push.push_staff(rows_push)
        print(f"[CRM] ССЧ проставлена: {updated} из {len(rows_push)}"
              + (f" | не найдено {len(missing)}" if missing else "")
              + (f" | ⚠ {why}" if why else ""))
        if why:
            exit_code = 1

    if a.purge:
        targets = purge_targets(rows, include_unknown=a.purge_unknown)
        if not targets:
            print("[чистка] удалять нечего")
        else:
            reason = (f"ССЧ за {SR.STAFF_YEAR} ниже {min_staff} по RusProfile"
                      + (" либо не известна" if a.purge_unknown else ""))
            print(f"[чистка] к удалению {len(targets)} лидов: {reason}")
            deleted, skipped, missing, why = crm_push.purge_leads(targets, reason)
            print(f"[CRM] удалено {len(deleted)} | пропущено CRM {len(skipped)} | "
                  f"не найдено {len(missing)}" + (f" | ⚠ {why}" if why else ""))
            for row in skipped:
                name = rows.get(str(row.get("inn")), {}).get("name", "")
                print(f"  пропущен: {row.get('inn')} | {name} | {row.get('reason')}")
            if why:
                exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
