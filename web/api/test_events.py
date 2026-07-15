# -*- coding: utf-8 -*-
"""Прогон парсера по НАСТОЯЩИМ логам прошлых прогонов (D:\\orq_tmp\\run_*.log).

Синтетические строки проверяют только то, что я сам себе придумал. Логи реальных
прогонов — единственный источник правды о том, что оркестратор печатает на самом деле.

    py web/api/test_events.py [путь_к_логу ...]
"""
from __future__ import annotations

import collections
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from web.api import events  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def run(path: str) -> None:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    kinds = collections.Counter()
    companies: dict[int, dict] = {}
    summary = None
    for line in lines:
        ev = events.parse(line)
        kinds[ev["type"]] += 1
        idx = ev.get("idx")
        if idx is not None:
            c = companies.setdefault(idx, {"idx": idx, "stages": set()})
            if ev["type"] == "company_stage":
                c["stages"].add(ev["stage"])
            elif ev["type"] == "company_done":
                c.update(name=ev["name"], cost=ev["cost"],
                         pdf=ev["one_pager"], dir=ev["dir"])
            elif ev["type"] == "company_skipped":
                c["skipped"] = ev["mode"]
        if ev["type"] == "finished":
            summary = ev

    unparsed = kinds["log"]
    print(f"\n=== {Path(path).name}: {len(lines)} строк ===")
    print("события:", dict(sorted(kinds.items(), key=lambda kv: -kv[1])))
    print(f"распознано: {len(lines) - unparsed}/{len(lines)} "
          f"({100 * (len(lines) - unparsed) // max(1, len(lines))}%), "
          f"осталось сырым логом: {unparsed}")
    print(f"компаний найдено: {len(companies)}")
    for c in sorted(companies.values(), key=lambda c: c["idx"]):
        st = ",".join(sorted(c["stages"])) or "-"
        print(f"  [{c['idx']}] {str(c.get('name'))[:38]:38} "
              f"стадии={st:34} ${c.get('cost', 0):.2f} "
              f"pdf={c.get('pdf')} {'SKIP=' + c['skipped'] if c.get('skipped') else ''}")
    print("итог:", {k: summary[k] for k in ("ok", "total", "files", "cost")} if summary
          else "НЕ РАСПОЗНАН")


if __name__ == "__main__":
    args = sys.argv[1:] or sorted(glob.glob(r"D:\orq_tmp\run_*.log"))
    for p in args:
        if Path(p).stat().st_size > 0:
            run(p)
