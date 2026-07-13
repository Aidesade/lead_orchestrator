# -*- coding: utf-8 -*-
r"""
person_enrich — по ФИО + ИНН собрать ПРЯМОЙ РАБОЧИЙ контакт ЛПР из ЛЕГИТИМНЫХ
источников (реализация варианта A из обсуждения).

Идея: ИНН — сильный ключ (детерминирует личность и её бизнес-след), ФИО — слабый
(тёзки). Поэтому: ИНН → личность и компании (Dadata/Checko) → от них к контактам,
а соц/мессенджер-аккаунты принимаем ТОЛЬКО подтверждённые ИНН-контекстом.

Что достаётся (и только это, честно):
  - подтверждённая личность (ЕГРЮЛ/ЕГРИП): ФИО директора/ИП, ОПФ, регион, ОКВЭД;
  - домен компании (Checko website, с валидацией принадлежности);
  - РАБОЧИЙ email: найденный на сайте и совпавший с ФИО + сгенерированный по шаблону
    домена с проверкой MX (и best-effort SMTP);
  - опубликованные рабочие телефоны (Checko + сайт);
  - соц/мессенджер-профили — best-effort, ТОЛЬКО подтверждённые ИНН-контекстом.

Чего НЕ делаем принципиально (deny-list, DENY_SOURCES): Truecaller/GetContact/
«пробив» номеров, breach/leak-базы, people-search-агрегаторы — краденые/недостоверные
источники и частная жизнь, а не деловой контакт. Их здесь конструктивно нет.

CLI:
  py person_enrich.py "Руденко Сергей Александрович" 6234065445
  py person_enrich.py "<ФИО>" <ИНН> --domain company.ru --no-social
"""
import argparse
import json
import os
import re
import socket
import sys
import urllib.parse
import urllib.request

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import warnings
warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import email_finder as EF
import harvest_inn_site as HIS
from deep_research_engine import _text_belongs   # чистая ф-я: ИНН/название на странице

# Источники, которые МЫ НЕ ИСПОЛЬЗУЕМ (краденое/частная жизнь/недостоверно).
# Список — для прозрачности в выводе; в коде этих источников нет.
DENY_SOURCES = (
    "truecaller", "getcontact", "«пробив» номера", "breach/leak-базы (HIBP/Dehashed)",
    "people-search (Pipl/Spokeo)", "username-профайлинг (Sherlock/Maigret)",
)

_PHONE_RE = re.compile(r"(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-()]*\d{2}[\s\-()]*\d{2}")


def _digits(s):
    return re.sub(r"\D", "", s or "")


# ------------------------------------------------------------- ФИО-матч ----
def _name_tokens(fio):
    return [t.lower() for t in re.split(r"[\s,]+", (fio or "").strip()) if len(t) >= 2]


def _fio_match(a, b):
    """Совпадают ли два ФИО по фамилии+имени (без учёта порядка/отчества)."""
    ta, tb = set(_name_tokens(a)), set(_name_tokens(b))
    if not ta or not tb:
        return False
    common = ta & tb
    return len(common) >= 2  # минимум фамилия+имя совпали


# --------------------------------------------------- e-mail по шаблону ----
def _tr1(token):
    """Первичная (короткая) транслитерация одного токена ФИО."""
    vs = EF._translit(token)
    return sorted(vs, key=len)[0] if vs else ""


def email_patterns(fio, domain):
    """ФИО (Фамилия Имя [Отчество]) + домен -> список кандидатов рабочей почты."""
    parts = _name_tokens(fio)
    if len(parts) < 2 or not domain:
        return []
    sur, nam = _tr1(parts[0]), _tr1(parts[1])
    pat = _tr1(parts[2]) if len(parts) > 2 else ""
    ni, pi = (nam[:1] if nam else ""), (pat[:1] if pat else "")
    raw = [
        sur, nam, f"{ni}.{sur}", f"{ni}{sur}", f"{nam}.{sur}", f"{sur}.{nam}",
        f"{nam}{sur}", f"{sur}{ni}", f"{ni}{pi}.{sur}" if pi else "",
        f"{nam}.{sur}.{pi}" if pi else "",
    ]
    out = []
    for local in raw:
        local = local.strip(".")
        if local and re.fullmatch(r"[a-z0-9.\-]+", local) and f"{local}@{domain}" not in out:
            out.append(f"{local}@{domain}")
    return out


# ------------------------------------------------------------ MX / SMTP ----
def mx_hosts(domain, timeout=8):
    """MX-хосты домена через DNS-over-HTTPS (stdlib urllib, без dnspython)."""
    try:
        u = "https://dns.google/resolve?name=" + urllib.parse.quote(domain) + "&type=MX"
        req = urllib.request.Request(u, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        hosts = []
        for ans in data.get("Answer", []):
            if ans.get("type") == 15:                 # 15 = MX
                parts = (ans.get("data") or "").split()
                if parts:
                    hosts.append(parts[-1].rstrip("."))
        return hosts
    except Exception:
        return []


def smtp_probe(mx, addr, timeout=8):
    """Best-effort SMTP RCPT-проверка: 'ok' | 'no' | 'unknown' (catch-all/блок → unknown).
    Многие серверы грейлистят/блокируют проверку — 'unknown' это норма, не ошибка."""
    import smtplib
    domain = addr.split("@", 1)[1]
    try:
        s = smtplib.SMTP(timeout=timeout)
        s.connect(mx)
        s.helo("mail.example.com")
        s.mail("verify@example.com")
        code_real, _ = s.rcpt(addr)
        # детект catch-all: заведомо несуществующий ящик
        code_fake, _ = s.rcpt(f"nonexistent-xyz-{_digits(addr)[:6] or '0'}@{domain}")
        s.quit()
        if code_real in (250, 251) and code_fake not in (250, 251):
            return "ok"                                # реальный принят, фейк отклонён
        if code_real in (550, 551, 553):
            return "no"
        return "unknown"                               # catch-all или неоднозначно
    except Exception:
        return "unknown"


# --------------------------------------------------- контакты с сайта ----
def site_contacts(domain, fio, max_pages=6, timeout=12):
    """Обойти контактные страницы домена -> (emails[], phones[]) из ОПУБЛИКОВАННОГО.
    Каждый email классифицируется; помечается совпадение с ФИО (личный рабочий)."""
    base = HIS._norm_url(domain).rstrip("/")
    fio_s, fio_w = EF.fio_tokens(fio)
    emails, phones, tried = [], set(), 0
    seen_e = set()
    for path in HIS.PATHS:
        if tried >= max_pages:
            break
        try:
            html = HIS._fetch(base + path, timeout=timeout)
        except Exception:
            continue
        tried += 1
        for c in EF.extract_candidates(html, path or "/"):
            e = c["email"]
            if e in seen_e or EF.is_junk(e):
                continue
            seen_e.add(e)
            cls, _ = EF.classify(e, set(), fio_s, fio_w)
            emails.append({"email": e, "class": cls, "page": c["page"],
                           "fio_match": cls == "personal_lpr"})
        for p in _PHONE_RE.findall(re.sub(r"<[^>]+>", " ", html)):
            d = _digits(p)
            if len(d) >= 11:
                phones.add("+7" + d[-10:])
    return emails, sorted(phones)


# --------------------------------------------- соц/мессенджер (best-effort) ----
_SOCIAL_HOSTS = ("vk.com", "vk.ru", "hh.ru", "t.me", "ok.ru")
_DDG = "https://html.duckduckgo.com/html/?q="


def find_social(fio, company_name, inn, timeout=12, log=print):
    """Best-effort: поиск проф-профилей (VK/hh/Telegram) по ФИО, ПОДТВЕРЖДЁННЫХ
    ИНН-контекстом (на странице встречается ИНН или название компании). Иначе — не берём
    (тёзки). При блоке SERP просто вернём пусто — не роняем пайплайн."""
    q = f'"{fio}" {company_name}'.strip()
    try:
        html = HIS._fetch(_DDG + urllib.parse.quote(q), timeout=timeout)
    except Exception:
        return []
    urls = []
    for m in re.finditer(r'uddg=([^&"]+)', html):          # DDG заворачивает ссылки в uddg=
        try:
            u = urllib.parse.unquote(m.group(1))
        except Exception:
            continue
        host = urllib.parse.urlparse(u).netloc.lower()
        if any(h in host for h in _SOCIAL_HOSTS) and u not in urls:
            urls.append(u)
    out = []
    for u in urls[:6]:
        try:
            page = HIS._fetch(u, timeout=timeout)
        except Exception:
            continue
        txt = re.sub(r"<[^>]+>", " ", page)
        # подтверждение принадлежности: ИНН или название компании на странице
        if _text_belongs(txt, company_name, inn):
            host = urllib.parse.urlparse(u).netloc.lower()
            plat = ("Telegram" if "t.me" in host else "VK" if "vk." in host
                    else "hh.ru" if "hh.ru" in host else host)
            out.append({"platform": plat, "url": u, "source": u,
                        "confidence": "подтверждён ИНН-контекстом"})
        if len(out) >= 4:
            break
    return out


# --------------------------------------------------------- главный сбор ----
def enrich_person(fio, inn, *, domain=None, dadata_token=None, checko_token=None,
                  verify_email=True, social=True, timeout=12, log=print):
    """ФИО + ИНН -> структурированный прямой рабочий контакт. Никогда не роняет —
    любой недоступный источник просто даёт меньше полей."""
    inn = _digits(inn)
    if domain:                                    # принять и полный URL, и голый домен
        domain = urllib.parse.urlparse(domain if "//" in domain else "http://" + domain).netloc or domain
    res = {
        "input": {"fio": fio, "inn": inn},
        "identity": {}, "fio_confirmed": None, "domain": domain or "",
        "contacts": {"work_emails": [], "work_phones": [], "social": []},
        "sources": [], "excluded_sources": list(DENY_SOURCES), "notes": [],
    }

    # 1) Личность по ИНН (Dadata: ЕГРЮЛ/ЕГРИП — авторитетно)
    try:
        import dadata_enrich as DA
        card = DA.DadataClient(token=dadata_token).find_by_inn(inn)
        lpr = DA.extract_lpr(card) if card else {}
        if lpr:
            res["identity"] = {k: lpr.get(k, "") for k in (
                "legal_name", "type", "opf", "ogrn", "okved", "okved_name",
                "state_status", "address", "lpr_fio", "lpr_post")}
            res["sources"].append("Dadata findById (ЕГРЮЛ/ЕГРИП)")
            res["fio_confirmed"] = _fio_match(fio, lpr.get("lpr_fio", ""))
            if res["fio_confirmed"] is False and lpr.get("lpr_fio"):
                res["notes"].append(
                    f"ВНИМАНИЕ: входное ФИО «{fio}» не совпало с ЛПР по ЕГРЮЛ "
                    f"«{lpr.get('lpr_fio')}» — проверьте, тот ли это человек.")
    except Exception as e:
        res["notes"].append(f"Dadata недоступен ({str(e)[:80]}) — личность не подтверждена")

    # 2) Контакты компании по ИНН (Checko: сайт/тел/почта/CEO)
    company_name = (res["identity"].get("legal_name") or "").strip()
    try:
        import checko_enrich as CH
        data = CH.CheckoClient(token=checko_token).company(inn)
        cc = CH.extract_company_contacts(data) if data else {}
        if cc.get("website") and not res["domain"]:
            res["domain"] = urllib.parse.urlparse(HIS._norm_url(cc["website"])).netloc
        if cc.get("phone"):
            for p in re.split(r"[,;]", cc["phone"]):
                d = _digits(p)
                if len(d) >= 11:
                    res["contacts"]["work_phones"].append(
                        {"phone": "+7" + d[-10:], "source": "Checko (/company)"})
        if cc.get("email"):
            res["contacts"]["work_emails"].append(
                {"email": cc["email"], "kind": cc.get("email_kind", ""),
                 "source": "Checko (/company)", "confidence": "опубликован"})
        if cc.get("ceo") and not company_name:
            company_name = ""  # директор из Checko как запас (личность уже из Dadata)
        if data:
            res["sources"].append("Checko /company (контакты ЕГРЮЛ)")
    except Exception as e:
        res["notes"].append(f"Checko недоступен ({str(e)[:80]})")

    dom = res["domain"] or ""
    # валидация домена: он реально этой компании? (тот же приём, что в движке)
    if dom:
        try:
            page = HIS._fetch(HIS._norm_url(dom), timeout=timeout)
            if page and not _text_belongs(re.sub(r"<[^>]+>", " ", page), company_name or fio, inn):
                res["notes"].append(f"домен {dom} не подтвердил принадлежность — email по шаблону НЕ строю")
                dom = ""
        except Exception:
            pass

    # 3) Рабочий email: (а) найденный на сайте + совпавший с ФИО; (б) шаблон+MX/SMTP
    if dom:
        try:
            site_emails, site_phones = site_contacts(dom, fio, timeout=timeout)
        except Exception:
            site_emails, site_phones = [], []
        for e in site_emails:
            if e["fio_match"]:                          # почта на сайте совпала с ФИО = сильнейший сигнал
                res["contacts"]["work_emails"].append(
                    {"email": e["email"], "kind": "личная рабочая (совпала с ФИО)",
                     "source": f"{dom}{e['page']}", "confidence": "высокая (опубликована + ФИО)"})
        for ph in site_phones:
            if ph not in [p["phone"] for p in res["contacts"]["work_phones"]]:
                res["contacts"]["work_phones"].append({"phone": ph, "source": f"сайт {dom}"})

        # шаблонные кандидаты + проверка MX (и best-effort SMTP)
        pats = email_patterns(fio, dom)
        if pats and verify_email:
            mx = mx_hosts(dom)
            if not mx:
                res["notes"].append(f"у домена {dom} нет MX — шаблонные email не проверить")
            else:
                have = {e["email"] for e in res["contacts"]["work_emails"]}
                for addr in pats[:6]:              # кап: не долбить чужой SMTP десятком RCPT
                    if addr in have:
                        continue
                    verdict = smtp_probe(mx[0], addr) if verify_email else "unknown"
                    if verdict == "ok":
                        res["contacts"]["work_emails"].append(
                            {"email": addr, "kind": "рабочая (шаблон)", "source": f"шаблон+SMTP@{dom}",
                             "confidence": "средняя-высокая (SMTP подтвердил)"})
                    elif verdict == "unknown":
                        res["contacts"]["work_emails"].append(
                            {"email": addr, "kind": "рабочая (шаблон, не подтв.)",
                             "source": f"шаблон, MX ok @{dom}",
                             "confidence": "средняя (домен принимает почту; отправкой не подтверждён)"})
                res["sources"].append(f"email по шаблону домена {dom} (MX/SMTP)")
        elif pats:
            for addr in pats[:3]:
                res["contacts"]["work_emails"].append(
                    {"email": addr, "kind": "рабочая (шаблон, без проверки)",
                     "source": f"шаблон @{dom}", "confidence": "низкая (не проверено)"})

    # 4) Соц/мессенджер — best-effort, только подтверждённые ИНН-контекстом
    if social and company_name:
        try:
            res["contacts"]["social"] = find_social(fio, company_name, inn, timeout=timeout, log=log)
            if res["contacts"]["social"]:
                res["sources"].append("проф-соцсети (подтверждены ИНН-контекстом)")
        except Exception as e:
            res["notes"].append(f"соц-поиск недоступен ({str(e)[:60]})")

    # дедуп email
    seen, uniq = set(), []
    for e in res["contacts"]["work_emails"]:
        if e["email"].lower() not in seen:
            seen.add(e["email"].lower())
            uniq.append(e)
    res["contacts"]["work_emails"] = uniq
    return res


def format_findings_block(res):
    """Компактный текстовый блок прямых контактов ЛПР — для передачи писателю .docx
    (прицепляется к находкам движка в _research_one)."""
    idn = res.get("identity", {})
    L = ["=== ПРЯМЫЕ КОНТАКТЫ ЛПР (person_enrich — легитимные источники) ==="]
    conf = res.get("fio_confirmed")
    confs = ("подтверждено по ЕГРЮЛ" if conf else
             "НЕ подтверждено по ЕГРЮЛ" if conf is False else "не проверено")
    L.append(f"ЛПР: {idn.get('lpr_fio') or res['input']['fio']} — "
             f"{idn.get('lpr_post', '')} (ФИО {confs})")
    if res.get("domain"):
        L.append(f"Домен: {res['domain']}")
    if res["contacts"]["work_emails"]:
        L.append("Рабочие email:")
        for e in res["contacts"]["work_emails"]:
            L.append(f"  - {e['email']} — {e.get('kind', '')} "
                     f"[{e.get('confidence', '')}] (источник: {e.get('source', '')})")
    if res["contacts"]["work_phones"]:
        L.append("Рабочие телефоны:")
        for p in res["contacts"]["work_phones"]:
            L.append(f"  - {p['phone']} (источник: {p.get('source', '')})")
    if res["contacts"].get("social"):
        L.append("Проф-профили (подтв. ИНН-контекстом):")
        for s in res["contacts"]["social"]:
            L.append(f"  - {s.get('platform', '')}: {s.get('url', '')}")
    for n in res.get("notes", []):
        L.append(f"Замечание: {n}")
    L.append("(Приватные источники — пробив/утечки — НЕ использованы.)")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="ФИО+ИНН -> прямой рабочий контакт ЛПР (легитимные источники)")
    ap.add_argument("fio", nargs="?", help="ФИО (Фамилия Имя [Отчество])")
    ap.add_argument("inn", nargs="?", help="ИНН (10 — компания / 12 — ИП/физлицо)")
    ap.add_argument("--leads", default=None,
                    help="JSON лидов: по каждому взять contact_person + _inn + website")
    ap.add_argument("--domain", default=None, help="домен компании, если известен (пропустить резолв)")
    ap.add_argument("--no-verify", action="store_true", help="не проверять шаблонные email по SMTP")
    ap.add_argument("--no-social", action="store_true", help="не искать соцсети/мессенджеры")
    a = ap.parse_args()

    def _dom(url):
        return urllib.parse.urlparse(HIS._norm_url(url)).netloc if url else None

    if a.leads:                                   # пакетный прогон по JSON лидов (формат пайплайна)
        leads = json.load(open(a.leads, encoding="utf-8"))
        for l in (leads if isinstance(leads, list) else [leads]):
            fio, inn = (l.get("contact_person") or ""), (l.get("_inn") or "")
            if not (fio and inn):
                print(f"[skip] {l.get('name')}: нет contact_person/_inn")
                continue
            print(f"\n=== {l.get('name')} | {fio} | ИНН {inn} ===", flush=True)
            out = enrich_person(fio, inn, domain=a.domain or _dom(l.get("website")),
                                verify_email=not a.no_verify, social=not a.no_social)
            print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    if not (a.fio and a.inn):
        ap.error("нужно указать ФИО и ИНН, либо --leads <json>")
    out = enrich_person(a.fio, a.inn, domain=a.domain,
                        verify_email=not a.no_verify, social=not a.no_social)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
