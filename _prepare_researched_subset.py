import json
import re
from pathlib import Path

root = Path(__file__).resolve().parent
source = json.loads((root / "rusprofile_28_orchestrator.json").read_text(encoding="utf-8"))
cache = Path(r"D:\лиды_нефтяная_отрасль\orq_cache")
passes = {}
for path in cache.glob("findings_*__*.md"):
    match = re.fullmatch(r"findings_(\d+)__(process|roles)\.md", path.name)
    if match and path.stat().st_size:
        passes.setdefault(match.group(1), set()).add(match.group(2))
ready = {inn for inn, kinds in passes.items() if {"process", "roles"} <= kinds}
rows = [lead for lead in source if str(lead.get("_inn") or "") in ready]
(root / "rusprofile_researched_subset.json").write_text(
    json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Компаний с двумя проходами deep research: {len(rows)}")
for lead in rows:
    print(f"- {lead.get('name')} ({lead.get('_inn')})")
