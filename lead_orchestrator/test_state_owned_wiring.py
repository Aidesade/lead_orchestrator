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


def check_collect_only_stops_before_phase_two():
    source = (HERE / "orchestrator.py").read_text(encoding="utf-8")
    assert "--collect-only" in source
    assert "работает только со сбором" in source
    # Выход collect-only стоит ДО отбора компаний в Фазу 2 («ресёрчим ВСЕХ»).
    assert source.index("[итог] collect-only:") < source.index("ресёрчим ВСЕХ")
    print("  ✓ --collect-only останавливает прогон после ФАЗЫ 1")


def check_one_deadline_wraps_all_preconditions():
    source = (HERE / "orchestrator.py").read_text(encoding="utf-8")
    collector = source[source.index("def _collect_state_owned"):source.index("def _collect(")]
    assert collector.index("deadline = time.monotonic()") < collector.index("fetch_existing_leads(")
    assert "fetch_existing_leads(deadline=deadline)" in collector
    assert "RosimRegistry.from_environment(deadline=deadline)" in collector
    assert "offscreen=offscreen, deadline=deadline" in collector
    assert "ownership_verifier=verifier, deadline=deadline" in collector
    print("  ✓ один абсолютный deadline охватывает CRM, Росимущество, Chrome и collector")


def check_clients_reach_collector():
    """Боевой вход обязан ДОСТАВИТЬ индекс клиентов в добор.

    `harvest_state_owned` за клиентами сам не ходит, когда индекс лидов передан
    снаружи, — иначе офлайн-вызовы полезли бы в сеть. Цена этого решения в том,
    что потерянный `client_index=` не сломает ничего явно: сбор просто перестанет
    отсеивать клиентов и молча пойдёт к своим. Этот тест и есть та поломка."""
    source = (HERE / "orchestrator.py").read_text(encoding="utf-8")
    collector = source[source.index("def _collect_state_owned"):source.index("def _collect(")]
    assert "fetch_existing_clients(deadline=deadline)" in collector, \
        "боевой вход перестал читать индекс клиентов"
    assert collector.index("fetch_existing_clients(") < collector.index("harvest_state_owned("), \
        "клиентов надо получить ДО Chrome и первой карточки"
    assert "client_index=client_index" in collector, \
        "индекс клиентов не доезжает до harvest_state_owned — отсев выключится молча"
    assert "print(client_facts(client_index))" in collector, \
        "состояние отсева клиентов обязано печататься: выключенный отсев должен быть виден"
    print("  ✓ индекс клиентов читается до Chrome и доезжает до добора")


def check_push_crm_only_after_collect():
    """`--push-crm` живёт ровно там, где прогон кончается сбором.

    Два пути записи в один раздел CRM за один прогон — это спор о статусе лида:
    после ФАЗЫ 2 у госрежима свой атомарный batch исследованных. Поэтому флаг
    привязан к `--collect-only`, запрещён в dry-run (заглушки в CRM не льём) и
    выгружает ПОСЛЕ сохранения JSON — иначе упавшая выгрузка унесла бы с собой
    и результат сбора.

    Сам запрос обёртка не собирает: и статус лида, и выбор ручки CRM живут в
    `crm_push.push_collected` — одно место, где решается, чем именно сырой сбор
    отличается от отправленного письма."""
    source = (HERE / "orchestrator.py").read_text(encoding="utf-8")
    assert '"--push-crm работает только с --collect-only"' in source
    assert "--push-crm несовместим с --dry-run" in source
    pusher = source[source.index("def _push_collected_to_crm"):source.index("def _collect_state_owned")]
    assert "push_collected(" in pusher
    assert "create_researched_batch" not in pusher and "push_lead(" not in pusher,         "обёртка не выбирает ручку CRM сама — это решает push_collected"
    call = source.index("            _push_collected_to_crm(")   # вызов, а не определение
    assert source.index("[итог] collect-only:") < call,         "выгрузка обязана идти после сохранения JSON: по нему её можно повторить"
    assert call < source.index("ресёрчим ВСЕХ"),         "выгрузка сырого сбора не должна доживать до ФАЗЫ 2"
    print("  ✓ --push-crm: только с --collect-only, после JSON и мимо batch исследованных")


def main():
    print("wiring режима госкомпаний:")
    check_state_plan()
    check_conflicts()
    check_fixed_profile_not_overridden()
    check_crm_batch_after_phase_two()
    check_collect_only_stops_before_phase_two()
    check_one_deadline_wraps_all_preconditions()
    check_clients_reach_collector()
    check_push_crm_only_after_collect()
    print("test_state_owned_wiring: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
