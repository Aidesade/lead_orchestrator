# -*- coding: utf-8 -*-
r"""
Проверка контактных данных лида на ОФИЦИАЛЬНОМ САЙТЕ (сайт берётся из RusProfile/
Checko). Заходим на сайт (контакты/о компании), снимаем email и телефоны и сверяем
с тем, что у лида:
  _email_verified — почта лида найдена на сайте (или совпал домен почты);
  _phone_verified — телефон лида найден на сайте;
  _site_reachable — сайт ответил.
Пустые/нецелевые контакты добираются с сайта (email — через email_finder, телефон —
первый найденный). Так контакт из RusProfile/Checko подтверждается первоисточником.

CLI:  py site_verify.py "D:\лиды\<база>.json"   (пересоберёт *_verified.json)
"""
import argparse
import json
import re
import sys

from harvest_inn_site import is_own_site
from webutil import _harvest_one, apply_best

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

_PHONE = re.compile(r"(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-()]*\d{2}[\s\-()]*\d{2}")


def _digits(s):
    return re.sub(r"\D", "", s or "")


def _tail(s):
    d = _digits(s)
    return d[-10:] if len(d) >= 10 else ""


def verify_contacts(leads, log=print):
    todo = [l for l in leads if is_own_site(l.get("website"))]
    log(f"=== Проверка контактов на сайте: {len(todo)} лидов с сайтом ===")
    ev = pv = add_m = add_p = unreachable = 0
    for i, l in enumerate(todo, 1):
        try:
            cands, pages = _harvest_one(l, budget=5)
        except Exception:
            l["_site_reachable"] = False
            unreachable += 1
            continue
        reachable = bool((pages or "").strip())
        l["_site_reachable"] = reachable
        if not reachable:
            unreachable += 1
            continue
        # email-кандидаты С САЙТА (исключаем затравку из 2ГИС/прежнюю почту)
        site_emails = {c["email"].lower() for c in cands
                       if c.get("page") != "2gis" and "@" in c.get("email", "")}
        site_phone_tails = {_tail(p) for p in _PHONE.findall(pages) if _tail(p)}

        le = (l.get("email") or "").lower()
        l["_email_verified"] = bool(le and (
            le in site_emails
            or any(le.split("@")[-1] == se.split("@")[-1] for se in site_emails if "@" in se)))
        if l["_email_verified"]:
            ev += 1

        lead_tails = {_tail(p) for p in re.split(r"[,;/]", l.get("phone") or "") if _tail(p)}
        l["_phone_verified"] = bool(lead_tails & site_phone_tails)
        if l["_phone_verified"]:
            pv += 1

        # ДОБОР email ТОЛЬКО если у лида почты НЕТ — существующую НЕ трогаем и НЕ понижаем
        if not le and site_emails:
            b = apply_best(l, cands, pages)
            if b:
                l["_email_src"] = "офиц. сайт"
                add_m += 1
        # телефон ТОЛЬКО если пуст
        if not l.get("phone") and site_phone_tails:
            l["phone"] = "+7" + sorted(site_phone_tails)[0]
            add_p += 1

        if i % 20 == 0:
            log(f"  проверено {i}/{len(todo)} | email✓{ev} тел✓{pv}")
    log(f"=== На сайте: email подтв. {ev} | тел подтв. {pv} | добрано email {add_m} "
        f"тел {add_p} | сайт недоступен {unreachable} ===")
    return {"email_verified": ev, "phone_verified": pv,
            "email_added": add_m, "phone_added": add_p, "unreachable": unreachable}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="JSON базы лидов")
    a = ap.parse_args()
    leads = json.load(open(a.path, encoding="utf-8"))
    verify_contacts(leads)
    out = a.path.replace(".json", "_verified.json")
    json.dump(leads, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("written:", out)


if __name__ == "__main__":
    main()
