# -*- coding: utf-8 -*-
"""Офлайн-контракт CLI/NL-подключения режима госкомпаний."""
from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import orchestrator_agent as OA


def check_state_plan():
    plan = OA._normalise_plan({
        "action": "run",
        "state_owned": True,
        "count_total": 7,
        "dry_run": True,
        "no_presentation": True,
    })
    assert plan["action"] == "run"
    assert plan["state_owned"] is True
    assert plan["count_total"] == 7
    cmd, error = OA._build_cmd(plan)
    assert error is None
    assert "--state-owned" in cmd
    assert cmd[cmd.index("--count") + 1] == "7"
    assert "--industries" not in cmd
    print("  ✓ NL-план строит --state-owned --count N")


def check_conflicts():
    plan = OA._normalise_plan({
        "action": "run", "state_owned": True, "industries": "mining",
    })
    assert plan["action"] == "clarify"
    plan = OA._normalise_plan({
        "action": "run", "state_owned": True, "leads_json": "leads.json",
    })
    assert plan["action"] == "clarify"
    plan = OA._normalise_plan({
        "action": "run", "state_owned": True, "count_total": 201,
    })
    assert plan["action"] == "clarify"
    source = (HERE / "orchestrator.py").read_text(encoding="utf-8")
    assert "not 1 <= a.count <= 200" in source
    print("  ✓ госрежим нельзя смешать с отраслью/JSON и ограничен 200 лидами")


def check_fixed_profile_not_overridden():
    plan = OA._normalise_plan({
        "action": "run", "state_owned": True, "count_total": 3,
        "min_revenue": 99, "region": "Москва",
    })
    cmd, error = OA._build_cmd(plan)
    assert error is None
    assert "--min-revenue" not in cmd and "--region" not in cmd
    print("  ✓ NL не может ослабить фиксированные бизнес-критерии")


def check_crm_batch_after_phase_two():
    source = (HERE / "orchestrator.py").read_text(encoding="utf-8")
    collector = source[source.index("def _collect_state_owned"):source.index("def _collect(")]
    assert "create_researched_batch" not in collector
    assert source.index("results.append(r)") < source.index("create_researched_batch")
    assert "elif fails:" in source and "CRM atomic batch не выполнен" in source
    print("  ✓ CRM create-only batch выполняется только после успешной Фазы 2")


def check_one_deadline_wraps_all_preconditions():
    source = (HERE / "orchestrator.py").read_text(encoding="utf-8")
    collector = source[source.index("def _collect_state_owned"):source.index("def _collect(")]
    assert collector.index("deadline = time.monotonic()") < collector.index("fetch_existing_leads(")
    assert "fetch_existing_leads(deadline=deadline)" in collector
    assert "RosimRegistry.from_environment(deadline=deadline)" in collector
    assert "offscreen=offscreen, deadline=deadline" in collector
    assert "ownership_verifier=verifier, deadline=deadline" in collector
    print("  ✓ один абсолютный deadline охватывает CRM, Росимущество, Chrome и collector")


def main():
    print("wiring режима госкомпаний:")
    check_state_plan()
    check_conflicts()
    check_fixed_profile_not_overridden()
    check_crm_batch_after_phase_two()
    check_one_deadline_wraps_all_preconditions()
    print("test_state_owned_wiring: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
