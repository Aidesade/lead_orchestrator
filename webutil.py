# -*- coding: utf-8 -*-
"""
Добор «нужной» почты лида с его сайта (для B2B-аутрича, не поддержки).

Движок — обычный HTTP (urllib, как в harvest_inn_site), НЕ браузер: на десятках
произвольных сайтов Playwright нестабилен (падает «socket.send() raised
exception»), а почта лежит в HTML/`mailto:` и отлично снимается без рендера.
Минус — сайты, где почта рисуется только JS, пропускаются (меньшинство).

Логика выбора (email_finder):
  ИП/микро -> личная/именная почта владельца или почта-бренд;
  сеть/франшиза -> почта отдела сотрудничества (franchise@/partner@/pr@/opt@)
                   со страниц /franchise, /partners, /sotrudnichestvo.
Поддержку (cc@/support@/info@/брони) берём только если целевой нет — и помечаем.

Поля результата в лиде:
  email · _email_kind · _email_is_target · _email_src · _email_candidates
Кандидаты сохраняются -> после Dadata (ОПФ+ФИО) бесплатно переоцениваются
(rerank_existing) без повторного захода на сайт.
"""
import argparse
import json
import sys

import email_finder as ef
from harvest_inn_site import _fetch, _norm_url, is_own_site

# построчный вывод (иначе прогресс долгой стадии буферизуется и кажется «зависшим»)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

# порядок обхода: футер/контакты (где обычно личная почта) -> сотрудничество
# (где почта отдела у сетей) -> о компании. Бюджет ограничивает число заходов.
CRAWL_PATHS = (("", "root"),
               *((p, "contacts") for p in ef.CONTACT_PATHS[:3]),
               *((p, "coop") for p in (
                   "/franchise", "/franshiza", "/partners", "/partnery",
                   "/sotrudnichestvo", "/opt", "/optom", "/reklama")),
               ("/about", "about"), ("/o-nas", "about"))


def _need_crawl(lead):
    """Качать сайт, если почты нет ИЛИ она нецелевая (общая/мусор)."""
    if not is_own_site(lead.get("website")):
        return False
    email = (lead.get("email") or "").strip()
    if not email:
        return True
    if lead.get("_email_is_target"):
        return False
    cls, _ = ef.classify(email, ef.brand_tokens(lead.get("name", ""),
                                                lead.get("website", "")),
                         *ef.fio_tokens(lead.get("contact_person", "")))
    return cls in ("generic", "junk")


def _harvest_one(lead, budget=6, timeout=9):
    """Обойти сайт лида по HTTP, собрать кандидатов в почту. (cands, pages_text)."""
    base = _norm_url(lead["website"]).rstrip("/")
    cands, pages_text, visited = [], [], 0
    # затравка: почта из 2ГИС/Яндекса тоже кандидат (не теряем её)
    if lead.get("email"):
        cands.append({"email": lead["email"].lower(), "page": "2gis",
                      "page_kind": "other", "context": ""})
    brand = ef.brand_tokens(lead.get("name", ""), lead.get("website", ""))
    fio_s, fio_w = ef.fio_tokens(lead.get("contact_person", ""))

    for path, _kind in CRAWL_PATHS:
        if visited >= budget:
            break
        try:
            html = _fetch(base + path, timeout=timeout)
        except Exception:
            continue
        visited += 1
        pages_text.append(ef._strip_tags(html)[:4000])
        cands.extend(ef.extract_candidates(html, path or "/"))
        # ранний выход: нашли сильную цель — личную почту ЛПР (совпадение ФИО)
        for c in cands:
            cls, sig = ef.classify(c["email"], brand, fio_s, fio_w)
            if cls == "personal_lpr" and sig.get("fio") == "strong":
                return cands, " ".join(pages_text)
    return cands, " ".join(pages_text)


def apply_best(lead, cands, pages_text=""):
    """Записать лучший email и метки в лид (re-rank-safe, без сети)."""
    cands = ef.dedup_candidates(cands)
    lead["_email_candidates"] = [
        {k: c.get(k, "") for k in ("email", "page", "page_kind", "context")}
        for c in cands]
    best = ef.pick_best(cands, lead, fallback_generic=True, pages_text=pages_text)
    if best:
        lead["email"] = best["email"]
        lead["_email_kind"] = best["kind"]
        lead["_email_is_target"] = bool(best["is_target"])
        lead["_email_src"] = best["page"]
    return best


def enrich_emails(leads, headless=False, log=print, save_path=None):
    """Best-effort: по HTTP зайти на сайт компании и подобрать целевую почту.
    save_path — куда инкрементально сохранять JSON (краш-безопасность).
    Параметр headless оставлен для совместимости вызова, не используется (без браузера)."""
    targets = [l for l in leads if _need_crawl(l)]
    if not targets:
        log("=== Добор email: нечего добирать (у всех целевая почта) ===")
        return
    log(f"\n=== Добор целевой почты с сайтов (HTTP): {len(targets)} компаний ===")
    hit = upgraded = 0
    for i, l in enumerate(targets, 1):
        before = l.get("email", "")
        try:
            cands, text = _harvest_one(l)
            best = apply_best(l, cands, text)
            if best and best["is_target"]:
                hit += 1
                if best["email"] != before:
                    upgraded += 1
        except Exception as e:
            log(f"    [warn] {l.get('name','')[:30]}: {e}")
        if i % 5 == 0:
            log(f"    email: {i}/{len(targets)} | целевых {hit}")
        if save_path and i % 10 == 0:           # инкрементально -> краш не теряет
            try:
                json.dump(leads, open(save_path, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=1)
            except Exception:
                pass
    log(f"=== Целевая почта найдена у {hit}/{len(targets)} "
        f"(улучшено адресов: {upgraded}) ===")


def rerank_existing(leads, log=print):
    """Без сети: переоценить уже собранных кандидатов (после Dadata, когда
    известны ОПФ и ФИО ЛПР). Возвращает число изменённых адресов."""
    changed = 0
    for l in leads:
        cands = l.get("_email_candidates")
        if not cands:
            # кандидатов с сайта нет, но почта из 2ГИС есть -> хотя бы пометить тип
            if l.get("email") and not l.get("_email_kind"):
                pseudo = [{"email": l["email"].lower(), "page": "2gis",
                           "page_kind": "other", "context": ""}]
                b = ef.pick_best(pseudo, l, fallback_generic=True)
                if b:
                    l["_email_kind"] = b["kind"]
                    l["_email_is_target"] = bool(b["is_target"])
                    l["_email_src"] = b["page"]
            continue
        cands = ef.dedup_candidates(cands)
        l["_email_candidates"] = cands           # почистить дубли при ре-ранке
        before = l.get("email", "")
        best = ef.pick_best(cands, l, fallback_generic=True)
        if best:
            l["email"] = best["email"]
            l["_email_kind"] = best["kind"]
            l["_email_is_target"] = bool(best["is_target"])
            l["_email_src"] = best["page"]
            if best["email"] != before:
                changed += 1
    if changed:
        log(f"=== Re-rank почты после Dadata: уточнено {changed} адресов ===")
    return changed


def main():
    ap = argparse.ArgumentParser(
        description="Добор целевой почты (личная ЛПР / отдел сотрудничества) с сайтов по HTTP")
    ap.add_argument("json_path", help="JSON базы лидов (можно *_ЛПР.json)")
    ap.add_argument("--headless", action="store_true", help="(совместимость, не используется)")
    ap.add_argument("--rerank-only", action="store_true",
                    help="не заходить на сайты, только переоценить кандидатов")
    ap.add_argument("--no-excel", action="store_true", help="не пересобирать xlsx")
    a = ap.parse_args()

    leads = json.load(open(a.json_path, encoding="utf-8"))
    if a.rerank_only:
        rerank_existing(leads)
    else:
        enrich_emails(leads, save_path=a.json_path)
    json.dump(leads, open(a.json_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"[saved] {a.json_path}")

    n_target = sum(1 for l in leads if l.get("_email_is_target"))
    n_mail = sum(1 for l in leads if l.get("email"))
    print(f"[stats] email всего: {n_mail} | из них целевых (не общих): {n_target}")
    by_kind = {}
    for l in leads:
        k = l.get("_email_kind", "—" if not l.get("email") else "?")
        by_kind[k] = by_kind.get(k, 0) + 1
    print(f"[stats] по типам: {by_kind}")

    if not a.no_excel and a.json_path.lower().endswith(".json"):
        import os
        import re
        from build_excel import build
        out = os.path.splitext(a.json_path)[0] + ".xlsx"
        industry = re.sub(r"^База_лидов_|_\d{4}-\d{2}-\d{2}.*$", "",
                          os.path.splitext(os.path.basename(a.json_path))[0]
                          ).replace("-", " ") or "лиды"
        build(leads, industry, out)
        print(f"[saved] {out}")


if __name__ == "__main__":
    main()
