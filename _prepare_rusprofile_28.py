import json
import re
from pathlib import Path

root = Path(__file__).resolve().parent
source = json.loads((root / "rusprofile_28_companies.json").read_text(encoding="utf-8"))
rows = []
for item in source:
    revenue_text = str(item.get("revenue") or "")
    match = re.search(r"([\d,.]+)\s*(млрд|млн)?", revenue_text, re.I)
    revenue = None
    if match:
        revenue = float(match.group(1).replace(" ", "").replace(",", "."))
        revenue *= 1_000_000_000 if (match.group(2) or "").lower() == "млрд" else 1_000_000
    phones = item.get("phones") or []
    emails = item.get("emails") or []
    rows.append({
        "name": item.get("name") or "",
        "niche": "Нефтегазовая отрасль",
        "website": item.get("website") or "",
        "phone": ", ".join(phones),
        "email": emails[0] if emails else "",
        "emails": emails,
        "contact_person": item.get("manager_name") or "",
        "contact_role": item.get("manager_role") or "",
        "source": "RusProfile",
        "pain": "Повышение эффективности нефтегазовых процессов и управление производственными знаниями",
        "offer": "Корпоративная on-premises LLM-платформа Telepatt",
        "status": "",
        "next_step": "",
        "_inn": str(item.get("inn") or ""),
        "_revenue": int(revenue) if revenue is not None else None,
        "_revenue_src": "RusProfile",
        "_industry": "oil_gas",
    })
(root / "rusprofile_28_orchestrator.json").write_text(
    json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
cache = Path(r"D:\лиды_нефтяная_отрасль\orq_cache")
cached_inns = set()
for path in cache.glob("findings_*__*.md"):
    cached = re.fullmatch(r"findings_(\d+)__(?:process|roles)\.md", path.name)
    if cached and path.stat().st_size:
        cached_inns.add(cached.group(1))
# Эти компании оставляем для повторной попытки писателя: их deep-research сохранён,
# но предыдущая Kimi-сессия завершилась без документов.
retry_writer_inns = {"1642002123", "1644014815"}  # Алойл, Татех
excluded_inns = cached_inns - retry_writer_inns
pending = [row for row in rows if row["_inn"] not in excluded_inns]
(root / "rusprofile_28_pending.json").write_text(
    json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Подготовлено: всего {len(rows)}, исключено по кэшу {len(rows) - len(pending)}, "
      f"в прогоне {len(pending)}")
