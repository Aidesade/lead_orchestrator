# -*- coding: utf-8 -*-
"""Офлайн-контракт свободного агентного Kimi research loop.

Обычный script-test без pytest: `py test_kimi_agent_freedom.py`.
"""
from __future__ import annotations

import pathlib


HERE = pathlib.Path(__file__).resolve().parent
APP = HERE.parent / "app"            # код пайплайна
BOOT = HERE.parent / "bootstrap"     # лаунчеры
KIMI = HERE.parents[1] / "lead_orchestrator_kimi"


def _text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def test_launcher_uses_agent() -> None:
    launcher = _text(BOOT / "run_cit_kimi.cmd")
    assert 'set "KIMI_WRITER_AGENT=1"' in launcher
    assert 'set "KIMI_WRITER_AGENT=0"' not in launcher


def test_writer_can_research_directly() -> None:
    spec = _text(KIMI / "kimi_agent" / "writer.yaml")
    for tool in ("LeadSearch", "LeadFetch", "LeadCrawl"):
        assert f'leadgen_tools:{tool}' in spec, tool


def test_writer_chooses_scout_fanout() -> None:
    runner = _text(KIMI / "writer_kimi_agent.py")
    assert 'counts["scout"] < 2' not in runner
    assert "минимум ДВА scout" not in runner


def test_writer_has_no_hidden_step_limit() -> None:
    runner = _text(KIMI / "writer_kimi_agent.py")
    assert 'KIMI_WRITER_MAX_STEPS", "100"' not in runner
    assert "max_steps_per_turn=max_steps" in runner


def test_writer_logs_direct_research() -> None:
    runner = _text(KIMI / "writer_kimi_agent.py")
    assert "[kimi-agent] web ->" in runner
    assert '"web_tools": web_counts' in runner


def test_parent_kills_process_tree() -> None:
    parent = _text(APP / "writer_kimi.py")
    assert "async def _kill_tree" in parent
    assert '"taskkill", "/PID", str(proc.pid), "/T", "/F"' in parent
    assert "except asyncio.CancelledError" in parent
    assert 'env["KIMI_SHARE_DIR"] = share_dir' in parent


def test_five_research_subagents_have_dependency_graph_and_tools() -> None:
    runner = _text(KIMI / "research_enrichment_agent.py")
    expected = (
        '"official_sources"', '"corporate_contour"', '"secondary_sources"',
        '"role_candidates"', '"candidate_contacts"',
    )
    positions = [runner.index(name) for name in expected]
    assert positions == sorted(positions)
    assert 'SCHEMA_VERSION = 3' in runner
    assert '("official_sources",),' in runner
    assert '("corporate_contour", "secondary_sources")' in runner
    assert '"candidate_contacts": ("official_sources", "corporate_contour", "role_candidates")' in runner
    assert "_validate_role_result" in runner and "_validate_evidence_trace" in runner
    for stem in ("official_sources", "corporate_contour", "secondary_sources",
                 "role_candidates", "candidate_contacts"):
        spec = _text(KIMI / "kimi_agent" / f"{stem}.yaml")
        for tool in ("LeadSearch", "LeadFetch", "LeadCrawl"):
            assert f'leadgen_tools:{tool}' in spec, (stem, tool)
        assert (KIMI / "kimi_agent" / f"{stem}.md").is_file()
    contacts = _text(KIMI / "kimi_agent" / "candidate_contacts.md")
    assert "allowed_candidates" in contacts
    assert "contact_kind" in contacts and "source_context" in contacts and "best_use" in contacts
    assert "outreach_policy" in contacts
    cli = _text(APP / "kimi_research_cli.py")
    assert 'os.environ["DR_USE_CRAWL4AI"] = "0"' in cli


def test_enrichment_is_cached_and_given_to_both_documents() -> None:
    orchestrator = _text(APP / "orchestrator.py")
    assert "enrichment_{key}.json" in orchestrator
    assert "checkpoint=enrichment_path, checkpoint_ttl_h=ttl_h" in orchestrator
    assert 'findings["process"] = (findings.get("process") or "") + (' in orchestrator
    assert 'findings["roles"] = (findings.get("roles") or "") + (' in orchestrator
    assert "ДОПОЛНИТЕЛЬНОЕ ДОСЬЕ: OFFICIAL + CORPORATE CONTOUR" in orchestrator
    assert "JSON-ДОСЬЕ RESEARCH-СУБАГЕНТОВ" in orchestrator
    assert "enrichment=enrichment" in orchestrator


def main() -> None:
    tests = (
        test_launcher_uses_agent,
        test_writer_can_research_directly,
        test_writer_chooses_scout_fanout,
        test_writer_has_no_hidden_step_limit,
        test_writer_logs_direct_research,
        test_parent_kills_process_tree,
        test_five_research_subagents_have_dependency_graph_and_tools,
        test_enrichment_is_cached_and_given_to_both_documents,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print("test_kimi_agent_freedom: all checks passed")


if __name__ == "__main__":
    main()
