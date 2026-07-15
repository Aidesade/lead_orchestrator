# -*- coding: utf-8 -*-
"""Чтение лидов из ORQ_LEADS_DIR + карточка компании.

Никакой БД: источник правды — те же leads_*.json, что пишет фаза 1 (pipeline._save).
Дедуп по ИНН, побеждает запись из файла, изменённого позже.

Имена папок/файлов на Диске НЕ дублируем — импортируем disk_organize из пакета
оркестратора (там _safe/category_for/industry_folder/_doc_names — чистые функции,
на верхнем уровне только stdlib). Иначе правила именования разъедутся с пайплайном,
и карточка начнёт показывать ссылки на несуществующие папки.
"""
from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config

if str(config.ORCH_DIR) not in sys.path:
    sys.path.insert(0, str(config.ORCH_DIR))

try:
    import disk_organize as DO
except Exception as e:                    # noqa: BLE001 — деградируем, но не падаем
    DO = None
    _DO_ERR = str(e)
else:
    _DO_ERR = ""


def _files_mtime() -> List[Path]:
    if not config.LEADS_DIR.exists():
        return []
    return sorted(config.LEADS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)


def load_all() -> List[Dict[str, Any]]:
    """Все лиды из всех JSON, дедуп по ИНН (позже изменённый файл побеждает)."""
    by_key: Dict[str, Dict[str, Any]] = {}
    for f in _files_mtime():                        # от старых к новым
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, list):
            continue
        for lead in data:
            if not lead or not lead.get("name"):
                continue
            key = str(lead.get("_inn") or lead["name"]).strip()
            by_key[key] = {**lead, "_file": f.name}
    return list(by_key.values())


def _has(lead: Dict[str, Any], k: str) -> bool:
    return bool(str(lead.get(k) or "").strip())


def contacts_score(lead: Dict[str, Any]) -> int:
    return sum(1 for k in ("email", "phone", "website", "contact_person") if _has(lead, k))


def enrich(lead: Dict[str, Any]) -> Dict[str, Any]:
    """Добавить производные поля, которые нужны таблице/карточке."""
    out = dict(lead)
    out["_contacts"] = contacts_score(lead)
    out["_category"] = DO.category_for(lead) if DO else None
    return out


def query(q: str = "", industry: str = "", region: str = "",
          min_revenue: float = 0.0, contacts: str = "",
          sort: str = "revenue", limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    rows = [enrich(l) for l in load_all()]

    if q:
        ql = q.lower().strip()
        rows = [r for r in rows
                if ql in str(r.get("name", "")).lower()
                or ql in str(r.get("_inn", ""))
                or ql in str(r.get("website", "")).lower()]
    if industry:
        keys = {s.strip() for s in industry.split(",") if s.strip()}
        rows = [r for r in rows if r.get("_industry") in keys]
    if region:
        rl = region.lower().strip()
        rows = [r for r in rows if rl in str(r.get("_region", "")).lower()]
    if min_revenue:
        rows = [r for r in rows if (r.get("_revenue") or 0) >= min_revenue]
    if contacts == "full":
        rows = [r for r in rows if r["_contacts"] == 4]
    elif contacts == "partial":
        rows = [r for r in rows if 0 < r["_contacts"] < 4]
    elif contacts == "none":
        rows = [r for r in rows if r["_contacts"] == 0]

    reverse = sort in ("revenue", "contacts")
    keyf = {
        "revenue": lambda r: r.get("_revenue") or 0,
        "contacts": lambda r: r["_contacts"],
        "name": lambda r: str(r.get("name", "")).lower(),
    }.get(sort, lambda r: r.get("_revenue") or 0)
    rows.sort(key=keyf, reverse=reverse)

    total = len(rows)
    return {"total": total, "offset": offset, "limit": limit,
            "items": rows[offset:offset + limit]}


def facets() -> Dict[str, Any]:
    rows = load_all()
    regions = sorted({str(r.get("_region") or "").strip()
                      for r in rows if r.get("_region")})
    inds = sorted({str(r.get("_industry") or "").strip()
                   for r in rows if r.get("_industry")})
    return {"total": len(rows), "regions": regions, "industries": inds,
            "files": [f.name for f in _files_mtime()],
            "leads_dir": str(config.LEADS_DIR),
            "disk_naming": bool(DO), "disk_naming_error": _DO_ERR}


def find(inn: str) -> Optional[Dict[str, Any]]:
    for l in load_all():
        if str(l.get("_inn") or "").strip() == inn.strip() or l.get("name") == inn:
            return l
    return None


def findings(lead: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Кэш дипресёрча по компании: orq_cache/findings_<ИНН>.md (TTL 72 ч в оркестраторе,
    но здесь показываем как есть, отдавая возраст — пусть решает человек)."""
    inn = str(lead.get("_inn") or "").strip()
    if not inn:
        return None
    p = config.CACHE_DIR / f"findings_{inn}.md"
    if not p.exists():
        return None
    st = p.stat()
    return {"path": str(p), "age_h": round((__import__("time").time() - st.st_mtime) / 3600, 1),
            "size": st.st_size, "text": p.read_text(encoding="utf-8", errors="replace")}


def deliverables(lead: Dict[str, Any]) -> Dict[str, Any]:
    """Где на Яндекс Диске лежат три файла компании. Правила имён — из disk_organize."""
    if not DO:
        return {"available": False, "error": _DO_ERR}
    dn = DO._safe(lead["name"])
    comp_dir = "/".join([config.DISK_BASE.rstrip("/"),
                         DO.industry_folder(lead), DO.category_for(lead), dn])
    names = DO._doc_names(dn)
    kinds = ["Карта бизнес-процессов", "Карта ролей и контактов", "One-pager Telepatt"]
    return {"available": True, "dir": comp_dir,
            "files": [{"kind": k, "name": n, "path": f"{comp_dir}/{n}"}
                      for k, n in zip(kinds, names)]}


def card(inn: str) -> Optional[Dict[str, Any]]:
    lead = find(inn)
    if not lead:
        return None
    return {"lead": enrich(lead), "deliverables": deliverables(lead),
            "findings": findings(lead)}


CSV_COLS = ["name", "_inn", "_industry", "_region", "_revenue", "website", "phone",
            "email", "contact_person", "_okved_descr", "_rusprofile_url",
            "_revenue_source_url", "pain", "offer"]


def to_csv(rows: List[Dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLS, extrasaction="ignore", delimiter=";")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()
