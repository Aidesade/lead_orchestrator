# -*- coding: utf-8 -*-
"""Замер: какой поиск лучше выводит на ОФИЦИАЛЬНЫЙ САЙТ компании и её контакты.

Зачем: после прозвона 200 компаний ~40 контактов оказались мёртвыми. Контакты в
пайплайне приходят из перепечатки ЕГРЮЛ-данных (RusProfile/Checko), у которой нет
даты. Гипотеза — сайт самой компании свежее. Прежде чем менять приоритет
источников, надо померить: доводит ли поиск до настоящего сайта и что с него
реально снимается.

Сравниваются ПОИСКОВЫЕ движки, а не парсеры: этап «домен -> контакты» одинаков
для всех и кэшируется по домену, поэтому вся разница в цифрах — это разница
поиска.

  claude  — поиск делает Claude в живой сессии (домены приезжают через --domains);
            запросы уходят с инфраструктуры Anthropic, IP пользователя не светится
  ddg     — html.duckduckgo.com, тот же скрейпер, что внутри deep_research_engine
  bing    — www.bing.com, он же
  google  — с этой машины без JS отдаёт пустую оболочку (замерено 2026-08-24)
  yandex  — с этой машины отдаёт SmartCaptcha (замерено 2026-08-24)

Проверка принадлежности домена — по ИНН на странице, а не по «похоже на название»:
на сайте-агрегаторе название компании тоже есть, а ИНН в разделе реквизитов есть
только у настоящего сайта.

  py contact_source_probe.py --sample 20 --engines ddg,bing     # скрейпер-базлайн
  py contact_source_probe.py --queries                          # список запросов для Claude
  py contact_source_probe.py --domains claude_domains.json       # прогнать домены от Claude
  py contact_source_probe.py --report                            # сводная статистика
"""
import argparse
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import harvest_inn_site as HIS
import person_enrich as PE

STATE_DIR = os.environ.get("ORQ_PROBE_DIR") or os.path.join(
    os.environ.get("ORQ_DATA_ROOT") or "D:\\", "orq_probe")
SAMPLE_FILE = os.path.join(STATE_DIR, "sample.json")
RESULT_FILE = os.path.join(STATE_DIR, "results.json")
DOMAIN_CACHE = os.path.join(STATE_DIR, "domain_cache.json")

# Каталоги и справочники: на них ИНН компании тоже есть, но это не её сайт.
# Берём общий список движка и дополняем тем, что реально лезет в выдачу по названию.
from deep_research_engine import AGGREGATORS as _DR_AGGREGATORS

AGGREGATORS = _DR_AGGREGATORS + (
    "wikipedia.", "wikimedia.", "yell.ru", "orgpage", "spravker", "rusbase",
    "vc.ru", "habr.com", "interfax.ru", "tass.ru", "kommersant.ru", "vedomosti.ru",
    "prom.ua", "flamp.", "zoon.", "yandex.ru", "google.", "2gis.",
    "seldon", "kartoteka", "sbis.ru", "companies.rbc", "vypiska-nalog",
    "primavista", "gks.ru", "nalog.", "gosuslugi.", "bo.nalog",
)


def log(msg):
    print(msg, flush=True)


def _digits(value):
    return re.sub(r"\D", "", str(value or ""))


def _tails(value):
    """Строка с телефонами -> множество последних 10 цифр (сравнимая форма)."""
    out = set()
    for chunk in re.split(r"[,;/]| и ", str(value or "")):
        d = _digits(chunk)
        if len(d) >= 10:
            out.add(d[-10:])
    return out


# ---------------------------------------------------------------- выборка ----
def crm_companies():
    """Индекс лидов CRM -> [{inn, name}]. Только чтение, тем же токеном стадии 9."""
    from project_env import load_project_env
    load_project_env()
    import crm_push

    if not (crm_push.base_url() and crm_push.token()):
        raise SystemExit("CRM не настроена: нет CRM_URL / CRM_INGEST_TOKEN")
    req = urllib.request.Request(
        crm_push.base_url() + "/api/leads/ingest/index", method="GET")
    req.add_header("Accept", "application/json")
    req.add_header("X-Ingest-Token", crm_push.token())
    with crm_push._urlopen(req, timeout=40) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return [
        {"inn": str(row.get("inn") or ""), "name": row.get("company_name") or ""}
        for row in data.get("items") or []
        if row.get("inn") and row.get("company_name")
    ]


def baseline_contacts(leads_dir=None):
    """Контакты, которые пайплайн уже отдал в CRM: {ИНН: {phone, email}}.

    Берутся из JSON-выгрузок ФАЗЫ 1 — GET-индекс CRM отдаёт только ИНН и название,
    самих контактов через ingest-токен не достать."""
    import glob

    leads_dir = leads_dir or os.environ.get("ORQ_LEADS_DIR") or "D:\\лиды"
    out = {}
    for path in glob.glob(os.path.join(leads_dir, "**", "*.json"), recursive=True):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        for lead in data:
            if not isinstance(lead, dict):
                continue
            inn = str(lead.get("_inn") or lead.get("inn") or "")
            if not inn:
                continue
            cur = out.setdefault(inn, {"phone": "", "email": "", "src": ""})
            for key in ("phone", "email"):
                if not cur[key] and lead.get(key):
                    cur[key] = str(lead[key])
                    cur["src"] = os.path.basename(path)
    return out


def build_sample(count, seed=20260824):
    """Детерминированная выборка компаний CRM, у которых ЕСТЬ контакт-базлайн.

    Без базлайна компанию нельзя зачесть в «подтвердил/опроверг» — а это половина
    смысла замера, поэтому такие в выборку не идут."""
    companies = crm_companies()
    base = baseline_contacts()
    usable = [c for c in companies
              if base.get(c["inn"], {}).get("phone") or base.get(c["inn"], {}).get("email")]
    rnd = random.Random(seed)
    rnd.shuffle(usable)
    picked = usable[:count] if count else usable
    for c in picked:
        c["baseline"] = base.get(c["inn"], {})
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(SAMPLE_FILE, "w", encoding="utf-8") as fh:
        json.dump(picked, fh, ensure_ascii=False, indent=1)
    log(f"[выборка] в CRM {len(companies)}, с контакт-базлайном {len(usable)}, "
        f"взято {len(picked)} -> {SAMPLE_FILE}")
    return picked


def load_sample():
    with open(SAMPLE_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def query_for(company):
    """Один и тот же запрос для всех движков — иначе сравнение нечестное."""
    name = company["name"].replace('"', " ").strip()
    return f"{name} официальный сайт"


# --------------------------------------------------------- поиск-скрейпер ----
def _serp(engine, query, timeout=20):
    """Конкретный движок без ротации: замер сравнивает движки, а не каскад."""
    import deep_research_engine as DR

    backends = {
        "ddg": ("https://html.duckduckgo.com/html/?",
                {"q": query, "kl": "ru-ru"}, DR._ddg_parse),
        "bing": ("https://www.bing.com/search?",
                 {"q": query, "setlang": "ru", "count": "20"}, DR._bing_parse),
        "brave": ("https://search.brave.com/search?", {"q": query}, DR._brave_parse),
        "google": ("https://www.google.com/search?",
                   {"q": query, "hl": "ru", "num": "20"}, DR._bing_parse),
        "yandex": ("https://yandex.ru/search/?",
                   {"text": query, "lr": "43"}, DR._bing_parse),
    }
    if engine not in backends:
        raise SystemExit(f"неизвестный движок: {engine}")
    base, params, parser = backends[engine]
    url = base + urllib.parse.urlencode(params)
    try:
        html = DR._search_fetch(url, timeout=timeout)
    except Exception as exc:
        return None, f"{type(exc).__name__} {getattr(exc, 'code', '')}".strip()
    low = html.lower()
    if any(m in low for m in ("showcaptcha", "smartcaptcha", "unusual traffic",
                              "/sorry/index", "are you a robot")):
        return None, "captcha"
    try:
        rows = [r for r in parser(html, 10)
                if r.get("url", "").startswith("http") and DR._not_search_engine(r["url"])]
    except Exception:
        rows = []
    if not rows:
        return None, "пустая выдача (JS-оболочка или сменилась вёрстка)"
    return rows, ""


def pick_domain(rows):
    """Первый в выдаче кандидат, похожий на собственный сайт компании."""
    for row in rows or []:
        url = row.get("url") or ""
        host = urllib.parse.urlparse(url).netloc.lower()
        if not host or not HIS.is_own_site(url):
            continue
        if any(a in host for a in AGGREGATORS):
            continue
        return host.replace("www.", "")
    return ""


# ---------------------------------------------- домен -> проверка и съём ----
_DOMAIN_CACHE = None


def _cache():
    global _DOMAIN_CACHE
    if _DOMAIN_CACHE is None:
        try:
            with open(DOMAIN_CACHE, encoding="utf-8") as fh:
                _DOMAIN_CACHE = json.load(fh)
        except Exception:
            _DOMAIN_CACHE = {}
    return _DOMAIN_CACHE


def _cache_save():
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(DOMAIN_CACHE, "w", encoding="utf-8") as fh:
        json.dump(_cache(), fh, ensure_ascii=False, indent=1)


def probe_domain(domain, inn, max_pages=5, timeout=8):
    """Домен -> {verified, inn_found, emails, phones}. Кэш по паре домен+ИНН.

    Кэш важен не для скорости, а для честности: если два движка привели на один
    домен, они обязаны получить одинаковый результат по контактам.

    Страница качается РОВНО ОДИН раз, и из неё сразу берутся и реквизиты, и
    контакты: раздельные проходы по ИНН и по почте удваивали и время, и нагрузку
    на чужой сайт."""
    key = f"{domain}|{inn}"
    cached = _cache().get(key)
    if cached is not None:
        return cached

    res = {"verified": False, "inn_found": "", "emails": [], "phones": [], "error": ""}
    base = HIS._norm_url(domain).rstrip("/")
    emails, phones, seen = [], set(), set()
    tried = 0
    for path in HIS.PATHS:
        if tried >= max_pages:
            break
        try:
            html = HIS._fetch(base + path, timeout=timeout)
        except Exception as exc:
            if not tried and not res["error"]:
                res["error"] = type(exc).__name__
            continue
        tried += 1
        found_inn, _ogrn = HIS._scan(html)
        if found_inn and not res["inn_found"]:
            res["inn_found"] = found_inn
            res["verified"] = found_inn == inn
        import email_finder as EF

        for cand in EF.extract_candidates(html, path or "/"):
            addr = cand["email"]
            if addr in seen or EF.is_junk(addr):
                continue
            seen.add(addr)
            emails.append(addr)
        for raw in PE._PHONE_RE.findall(re.sub(r"<[^>]+>", " ", html)):
            digits = _digits(raw)
            if len(digits) >= 11:
                phones.add("+7" + digits[-10:])
    if tried:
        res["error"] = ""
        res["emails"] = emails[:20]
        res["phones"] = sorted(phones)[:20]
    _cache()[key] = res
    _cache_save()
    return res


def score_row(company, domain, probe):
    """Строка результата одного движка по одной компании."""
    base = company.get("baseline") or {}
    base_phones = _tails(base.get("phone"))
    base_emails = {e.strip().lower() for e in re.split(r"[,;\s]+", base.get("email") or "") if "@" in e}
    site_phones = _tails(",".join(probe.get("phones") or []))
    site_emails = {e.lower() for e in probe.get("emails") or []}
    return {
        "inn": company["inn"],
        "name": company["name"],
        "domain": domain,
        "verified": bool(probe.get("verified")),
        "inn_found": probe.get("inn_found") or "",
        "n_phones": len(probe.get("phones") or []),
        "n_emails": len(probe.get("emails") or []),
        "phone_confirms": bool(base_phones & site_phones),
        "email_confirms": bool(base_emails & site_emails),
        "phone_differs": bool(site_phones and base_phones and not (base_phones & site_phones)),
        "error": probe.get("error") or "",
    }


# -------------------------------------------------------------- прогоны ----
def run_scraper(engines, pause=2.0):
    sample = load_sample()
    results = load_results()
    for engine in engines:
        rows, blocked = [], 0
        log(f"\n=== движок {engine}: {len(sample)} компаний ===")
        for i, company in enumerate(sample, 1):
            serp, err = _serp(engine, query_for(company))
            if serp is None:
                blocked += 1
                rows.append({"inn": company["inn"], "name": company["name"],
                             "domain": "", "verified": False, "inn_found": "",
                             "n_phones": 0, "n_emails": 0, "phone_confirms": False,
                             "email_confirms": False, "phone_differs": False,
                             "error": f"serp:{err}"})
                log(f"  {i:>3}/{len(sample)} {company['name'][:44]:<44} SERP: {err}")
            else:
                domain = pick_domain(serp)
                probe = probe_domain(domain, company["inn"]) if domain else {}
                row = score_row(company, domain, probe)
                rows.append(row)
                mark = "ИНН✓" if row["verified"] else ("ИНН✗" if domain else "нет домена")
                log(f"  {i:>3}/{len(sample)} {company['name'][:44]:<44} "
                    f"{domain or '—':<28} {mark:<10} тел={row['n_phones']} почт={row['n_emails']}")
            time.sleep(pause)
        results[engine] = rows
        log(f"[{engine}] SERP не отдал результат: {blocked}/{len(sample)}")
    save_results(results)


def run_domains(path, engine="claude"):
    """Домены, найденные Claude в живой сессии -> та же проверка, что у скрейпера.

    Формат файла: {"ИНН": "domain.ru", ...}; пустая строка = Claude сайта не нашёл."""
    with open(path, encoding="utf-8") as fh:
        domains = json.load(fh)
    sample = load_sample()
    results = load_results()
    rows = []
    log(f"\n=== движок {engine}: {len(sample)} компаний ===")
    for i, company in enumerate(sample, 1):
        domain = (domains.get(company["inn"]) or "").strip().lower().replace("www.", "")
        probe = probe_domain(domain, company["inn"]) if domain else {}
        row = score_row(company, domain, probe)
        rows.append(row)
        mark = "ИНН✓" if row["verified"] else ("ИНН✗" if domain else "нет домена")
        log(f"  {i:>3}/{len(sample)} {company['name'][:44]:<44} "
            f"{domain or '—':<28} {mark:<10} тел={row['n_phones']} почт={row['n_emails']}")
    results[engine] = rows
    save_results(results)


def load_results():
    try:
        with open(RESULT_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_results(results):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(RESULT_FILE, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1)
    log(f"\n[сохранено] {RESULT_FILE}")


# --------------------------------------------------------------- отчёт ----
def _mail_domains(sample):
    """{ИНН: домен почты, которая уже лежит в CRM}.

    Независимая от ИНН-на-странице проверка «тот ли сайт»: если пайплайн знает
    адрес info@tnpsrt.ru, то домен tnpsrt.ru принадлежит компании, и сверять это
    можно даже с сайтом, который реквизитов не публикует."""
    out = {}
    for company in sample:
        for addr in re.split(r"[,;\s]+", (company.get("baseline") or {}).get("email") or ""):
            if "@" in addr:
                out[company["inn"]] = addr.split("@")[-1].strip().lower().replace("www.", "")
                break
    return out


def report():
    results = load_results()
    if not results:
        raise SystemExit("нет результатов: сначала прогони --engines / --domains")
    mail_dom = _mail_domains(load_sample())
    for rows in results.values():
        for row in rows:
            want = mail_dom.get(row["inn"], "")
            row["mail_domain_hit"] = bool(want and row["domain"] and row["domain"] == want)
            row["mail_domain_miss"] = bool(want and row["domain"] and row["domain"] != want)
    total = max(len(rows) for rows in results.values())
    head = (f"{'движок':<9}{'SERP ок':>9}{'домен':>8}{'ИНН✓':>7}{'контакты':>10}"
            f"{'подтв.тел':>11}{'др.телефон':>12}{'подтв.почта':>13}")
    log("\n" + "=" * len(head))
    log(f"ЗАМЕР КАЧЕСТВА ПОИСКА САЙТА — {total} компаний из CRM")
    log("=" * len(head))
    log(head)
    log("-" * len(head))
    for engine, rows in results.items():
        n = len(rows) or 1
        serp_ok = sum(1 for r in rows if not str(r.get("error", "")).startswith("serp:"))
        dom = sum(1 for r in rows if r["domain"])
        ver = sum(1 for r in rows if r["verified"])
        con = sum(1 for r in rows if r["n_phones"] or r["n_emails"])
        ph = sum(1 for r in rows if r["phone_confirms"])
        pd = sum(1 for r in rows if r["phone_differs"])
        em = sum(1 for r in rows if r["email_confirms"])
        log(f"{engine:<9}{serp_ok:>6}/{n:<2}{dom:>5}/{n:<2}{ver:>4}/{n:<2}"
            f"{con:>7}/{n:<2}{ph:>8}/{n:<2}{pd:>9}/{n:<2}{em:>10}/{n:<2}")
    log("-" * len(head))

    # Отдельная полоса: сверка домена с доменом почты, уже известной пайплайну.
    # Считается только по компаниям, где такая почта есть, — иначе знаменатель врёт.
    log("")
    log(f"{'движок':<9}{'домен = домен почты из CRM':>32}{'домен ДРУГОЙ':>16}")
    log("-" * len(head))
    for engine, rows in results.items():
        checkable = [r for r in rows if r.get("mail_domain_hit") or r.get("mail_domain_miss")]
        hit = sum(1 for r in checkable if r["mail_domain_hit"])
        miss = sum(1 for r in checkable if r["mail_domain_miss"])
        log(f"{engine:<9}{hit:>29}/{len(checkable):<2}{miss:>13}/{len(checkable):<2}")
    log("-" * len(head))
    log("ИНН✓     — на найденном сайте в реквизитах стоит ИНН именно этой компании")
    log("подтв.тел — телефон с сайта совпал с тем, что пайплайн уже отдал в CRM")
    log("др.телефон — сайт даёт телефоны, и ни один не совпал с тем, что в CRM "
        "(кандидат на замену)")

    # Расхождения — то, ради чего замер и делался.
    log("\n--- РАСХОЖДЕНИЯ: сайт подтверждён по ИНН, а телефон в CRM другой ---")
    seen = set()
    for engine, rows in results.items():
        for r in rows:
            if r["verified"] and r["phone_differs"] and r["inn"] not in seen:
                seen.add(r["inn"])
                log(f"  {r['inn']}  {r['name'][:46]:<46} {r['domain']}  "
                    f"(движок {engine}, тел на сайте: {r['n_phones']})")
    if not seen:
        log("  нет")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, help="собрать выборку из N компаний CRM")
    ap.add_argument("--engines", help="скрейпер-движки через запятую: ddg,bing,google,yandex")
    ap.add_argument("--domains", help="JSON {ИНН: домен} от Claude из живой сессии")
    ap.add_argument("--engine-name", default="claude", help="как назвать движок из --domains")
    ap.add_argument("--queries", action="store_true", help="напечатать запросы для Claude")
    ap.add_argument("--report", action="store_true", help="сводная статистика")
    ap.add_argument("--pause", type=float, default=2.0, help="пауза между SERP, с")
    args = ap.parse_args()

    if args.sample:
        build_sample(args.sample)
    if args.queries:
        for c in load_sample():
            print(json.dumps({"inn": c["inn"], "query": query_for(c)}, ensure_ascii=False))
    if args.engines:
        run_scraper([e.strip() for e in args.engines.split(",") if e.strip()], args.pause)
    if args.domains:
        run_domains(args.domains, args.engine_name)
    if args.report:
        report()
    if not any((args.sample, args.engines, args.domains, args.queries, args.report)):
        ap.print_help()


if __name__ == "__main__":
    main()
