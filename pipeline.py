# -*- coding: utf-8 -*-
r"""
ЕДИНЫЙ конвейер сбора крупных B2B-лидов одной командой:

  RusProfile (uc, антибот) — поиск по ОКВЭД + ВЫРУЧКА>порога -> ИНН/название/ЛПР/регион
        |
  ГИР БО ФНС (free) — официальная выручка по ИНН + кликабельная ссылка-источник
        |                (для закрытой отчётности/ОПК остаётся выручка RusProfile)
  Checko /v2/company (free 100/день) — контакты по ИНН: телефон/email/сайт
        |
  Excel в формате шаблона D:\лиды (+ JSON-резерв)

Каждый источник бесплатный. Контакты RusProfile платные -> их даёт Checko.
Ключи: CHECKO_TOKEN (env, для контактов). DADATA_TOKEN — опц. (тут не нужен:
ЛПР приходит из RusProfile/Checko).

Запуск:
  py pipeline.py --industries construction,energy,water,opk --count 200 --min-revenue 1e9
  py pipeline.py --industries processing --count 50 --out "D:\лиды\Переработка.xlsx"
  py pipeline.py --industries construction --count 8 --checko-cap 3   # быстрый тест

Откроется окно реального Chrome (uc) — нужно для антибота RusProfile.
"""
import argparse
import collections
import math
import os
import sys
import time
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

# запуск по АБСОЛЮТНОМУ пути без cd (`py C:/.../scripts/pipeline.py`): добавляем
# каталог скрипта в путь, чтобы импорты соседних модулей работали из любой папки.
# cd в составной команде заставляет харнесс спрашивать разрешение — поэтому без cd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import source_rusprofile as RP
from build_excel import build
from revenue_enrich import add_revenue

DEFAULT_DIR = r"D:\лиды"
TITLE = "Крупный бизнес (выручка > {thr} млрд ₽): {inds}"


def log(m):
    print(m, flush=True)


def _select(leads, count, industries):
    """Отобрать count лидов с равномерным миксом по отраслям, внутри — по убыванию выручки."""
    by = collections.defaultdict(list)
    for l in leads:
        by[l.get("_industry")].append(l)
    for k in by:
        by[k].sort(key=lambda l: -(l.get("_revenue") or 0))
    quota = math.ceil(count / max(1, len(industries)))
    picked, used = [], set()
    for ind in industries:
        for l in by.get(ind, [])[:quota]:
            picked.append(l); used.add(id(l))
    if len(picked) < count:  # добить самыми крупными из остатка
        rest = sorted((l for l in leads if id(l) not in used),
                      key=lambda l: -(l.get("_revenue") or 0))
        picked += rest[:count - len(picked)]
    order = {ind: i for i, ind in enumerate(industries)}
    picked.sort(key=lambda l: (order.get(l.get("_industry"), 99), -(l.get("_revenue") or 0)))
    return picked[:count]


def run(industries, count, min_revenue, out_xlsx, region=None, headless=False,
        checko_cap=100, max_pages=20, exclude_regions=None,
        to_disk=True, disk_base="disk:/Лиды", disk_account=None):
    t0 = time.time()
    per_ind = math.ceil(count / max(1, len(industries)))
    json_out = os.path.splitext(out_xlsx)[0] + ".json"

    # 1) RusProfile: компании по ОКВЭД + выручка>порога (одна uc-сессия)
    log(f"[1/5] RusProfile: отрасли {industries} | порог >{min_revenue/1e9:g} млрд "
        f"| ~{per_ind}/отрасль")
    leads = RP.harvest(industries, min_revenue=min_revenue, per_industry=per_ind,
                       region=region, headless=headless, out_path=json_out,
                       exclude_regions=exclude_regions)
    if not leads:
        log("[!] RusProfile ничего не вернул — проверь коды ОКВЭД/доступ. Стоп.")
        raise SystemExit(1)
    log(f"[1/5] собрано {len(leads)} компаний")

    # 2) ГИР БО: официальная выручка + ссылка (перетирает RusProfile там, где есть)
    log(f"[2/5] ГИР БО: сверка выручки по {len(leads)} ИНН ...")
    try:
        add_revenue(leads, workers=5, log=log)  # 5, не 10: 10 параллельных перегружают ГИР БО -> таймауты
    except Exception as e:
        log(f"[2/5] ГИР БО пропущен ({e})")
    _save(leads, json_out)

    # 3) Контакты: RusProfile (твой аккаунт, БЕЗ суточного лимита) -> Checko (фолбэк, free 100/день)
    log("[3/5] Контакты: RusProfile (без лимита) -> остаток добивает Checko")
    try:
        import rusprofile_session as RPS
        if os.path.exists(RPS.COOKIES_FILE):
            with RPS.RusProfileAuth(headless=False) as rs:
                rs.enrich_leads(leads, only_missing=True, log=log)
        else:
            log("  [RusProfile] нет cookie — пропуск (один раз: py rusprofile_session.py --login)")
    except Exception as e:
        log(f"  [RusProfile] пропуск ({e})")
    _save(leads, json_out)
    try:
        import checko_enrich as CK
        client = CK.CheckoClient()
        CK.checko_contacts_pass(client, leads, cap=checko_cap, only_missing=True, log=log)
    except Exception as e:
        log(f"  [Checko] пропуск ({e}) — задай CHECKO_TOKEN")
    _save(leads, json_out)

    # 4) Проверка контактов на ОФИЦИАЛЬНОМ САЙТЕ (сверка email/телефона первоисточником)
    log("[4/5] Проверка контактов на официальных сайтах ...")
    try:
        from site_verify import verify_contacts
        verify_contacts(leads, log=log)
    except Exception as e:
        log(f"  [сайт] пропуск ({e})")
    _save(leads, json_out)

    # 5) отбор count с миксом + Excel
    picked = _select(leads, count, industries)
    thr = f"{min_revenue/1e9:g}"
    title = TITLE.format(thr=thr, inds=", ".join(industries))
    build(picked, title[:90], out_xlsx)
    _save(picked, json_out)

    secs = int(time.time() - t0)
    site = sum(1 for l in picked if l.get("website"))
    ph = sum(1 for l in picked if l.get("phone"))
    em = sum(1 for l in picked if l.get("email"))
    lpr = sum(1 for l in picked if l.get("contact_person"))
    rev_g = sum(1 for l in picked if l.get("_revenue_src") == "girbo")
    by = collections.Counter(l.get("_industry") for l in picked)
    log(f"\n[ГОТОВО] {len(picked)} лидов за {secs//60}м {secs%60}с | отрасли: {dict(by)}")
    log(f"[ГОТОВО] выручка: {len(picked)}/{len(picked)} (офиц. ГИР БО {rev_g}, ост. RusProfile)")
    log(f"[ГОТОВО] сайт {site} | телефон {ph} | email {em} | ЛПР {lpr}")
    log(f"[ГОТОВО] Excel: {out_xlsx}")
    log(f"[ГОТОВО] JSON:  {json_out}")

    # 6) раскладка по Яндекс Диску: <base>/<отрасль>/<полнота контактов>/<компания>/
    #    + 2 .docx (досье, стратегия). По умолчанию включено; Диск-сбой Excel не рушит.
    if to_disk:
        try:
            import disk_organize as DO
            log(f"[6/6] Яндекс Диск: раскладываю {len(picked)} компаний в {disk_base} ...")
            r = DO.organize_to_disk(picked, base=disk_base, account=disk_account, log=log)
            log(f"[ГОТОВО] Диск: {r['companies']} папок, {r['uploaded']} файлов, "
                f"ошибок {len(r['errors'])} | категории: {r['by_category']}")
        except Exception as e:
            log(f"[6/6] Диск пропущен ({e}) — проверь `yacli login disk`")
    return picked


def _save(rows, path):
    import json
    json.dump(rows, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def main():
    ap = argparse.ArgumentParser(description="RusProfile -> ГИР БО -> Checko -> Excel (одна команда)")
    ap.add_argument("--industries", default="construction,energy,water,processing",
                    help="из: " + ", ".join(RP.INDUSTRY.keys()))
    ap.add_argument("--count", type=int, default=200)
    ap.add_argument("--min-revenue", type=float, default=1e9)
    ap.add_argument("--region", default=None,
                    help="регион: название/аббревиатура, КЛИЕНТСКИЙ фильтр "
                         "(напр. ХМАО, Татарстан); пусто = вся РФ")
    ap.add_argument("--exclude-regions", default=None,
                    help="исключить регионы по подстрокам через запятую, напр. 'москва,санкт-петербург'")
    ap.add_argument("--checko-cap", type=int, default=100, help="лимит запросов Checko/день (free=100)")
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--out", default=None)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--no-disk", dest="to_disk", action="store_false",
                    help="не раскладывать результат по Яндекс Диску")
    ap.add_argument("--disk-base", default="disk:/Лиды",
                    help="корень дерева на Диске (по умолч. disk:/Лиды)")
    ap.add_argument("--disk-account", default=None, help="алиас аккаунта yacli")
    a = ap.parse_args()

    inds = [s.strip() for s in a.industries.split(",") if s.strip() in RP.INDUSTRY]
    if not inds:
        log("[!] не распознаны отрасли. Доступно: " + ", ".join(RP.INDUSTRY.keys()))
        raise SystemExit(1)
    out = a.out
    if not out:
        os.makedirs(DEFAULT_DIR, exist_ok=True)
        out = os.path.join(DEFAULT_DIR,
                           f"База_лидов_крупные_{datetime.now():%Y-%m-%d}.xlsx")
    excl = None
    if a.exclude_regions:
        excl = [p.strip().lower() for p in a.exclude_regions.split(",") if p.strip()]
    run(inds, a.count, a.min_revenue, out, region=a.region, headless=a.headless,
        checko_cap=a.checko_cap, max_pages=a.max_pages, exclude_regions=excl,
        to_disk=a.to_disk, disk_base=a.disk_base, disk_account=a.disk_account)


if __name__ == "__main__":
    main()
