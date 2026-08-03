# -*- coding: utf-8 -*-
"""Офлайн-контракт пяти stage-2 enrichment-субагентов (без сети и LLM).

Запуск: python test_research_enrichment.py
"""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import pathlib
import tempfile
import types
import zipfile
from types import SimpleNamespace


ROOT = pathlib.Path(__file__).resolve().parent
KIMI = ROOT.parent / "lead_orchestrator_kimi"
SPEC = importlib.util.spec_from_file_location(
    "research_enrichment_agent_under_test", KIMI / "research_enrichment_agent.py")
R = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(R)


def _message(role: str, text: str = "", *, calls=None, call_id: str = ""):
    return SimpleNamespace(
        role=role,
        tool_calls=list(calls or []),
        tool_call_id=call_id or None,
        extract_text=lambda: text,
    )


def _call(call_id: str, name: str, arguments: dict):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments, ensure_ascii=False)),
    )


def _fixtures():
    official_url = "https://company.test/leadership"
    media_url = "https://media.test/speaker"
    contact_url = "https://company.test/director-email"
    return {
        "official_sources": {
            "confirmed_facts": [{
                "claim": "На официальной странице указан генеральный директор Иванов Иван Иванович",
                "source_type": "official_company_site", "source_url": official_url,
                "publication_date": "2026-01-10", "observed_at": "2020-01-01T00:00:00+00:00",
                "evidence_text": "Страница руководства называет Иванова генеральным директором",
                "confidence": 0.98,
            }],
            "official_domains": ["company.test"],
            "checked_sources": [{
                "source_type": "official_company_site", "source_url": official_url,
                "outcome": "evidence_found", "observed_at": "2020-01-01T00:00:00+00:00",
            }],
            "gaps": [{
                "gap_id": "person_full_name",
                "description": "не удалось независимо подтвердить полное ФИО во втором официальном источнике",
            }],
        },
        "corporate_contour": {
            "target_company": {"name": "АО Тест", "inn": "1234567890"},
            "decision_centers": [{
                "function": "общее управление", "organization": "АО Тест", "type": "target",
                "rationale": "Официальная страница указывает руководителя целевого юрлица",
                "confidence": 0.95, "status": "confirmed", "source_urls": [official_url],
            }],
            "organizations": [{
                "name": "ООО Тест Управление", "inn": "1234567891",
                "relation": "management_company", "functions": ["управление"],
                "source_urls": [official_url], "status": "probable",
            }],
            "edges": [{
                "from": "ООО Тест Управление", "to": "АО Тест", "relation": "management_company",
                "source_url": official_url, "confidence": 0.72, "status": "probable",
            }],
            "gaps": [],
        },
        "secondary_sources": {
            "findings": [{
                "claim": "В программе конференции приведено полное отчество Иванова",
                "status": "probable", "source_type": "conference", "source_url": media_url,
                "publication_date": "2026-02-02", "observed_at": "2020-01-01T00:00:00+00:00",
                "evidence_text": "В программе Иванов указан спикером от АО Тест",
                "official_gap_id": "person_full_name", "confidence": 0.75,
            }],
            "hypotheses_for_verification": [],
            "checked_sources": [{
                "source_type": "conference", "source_url": media_url,
                "outcome": "evidence_found", "observed_at": "2020-01-01T00:00:00+00:00",
            }],
        },
        "role_candidates": {
            "candidates": [{
                "full_name": "Иванов Иван Иванович", "target_function": "ceo",
                "reported_title": "генеральный директор", "organization": "АО Тест",
                "inn": "1234567890", "status": "confirmed",
                "is_current_role_confirmed": False, "source_urls": [official_url, media_url],
                "publication_dates": ["2026-01-10", "2026-02-02"],
                "evidence": "Два источника называют ФИО и роль", "confidence": 0.93,
                "observed_at": "2020-01-01T00:00:00+00:00",
            }],
            "unfilled_functions": sorted(R.TARGET_FUNCTIONS - {"ceo"}), "conflicts": [],
        },
        "candidate_contacts": {
            "contacts": [{
                "candidate_full_name": "Иванов Иван Иванович", "candidate_function": "ceo",
                "value": "director@company.test", "contact_kind": "personal_work",
                "source_context": "official_company_site", "best_use": "прямой адресат",
                "outreach_policy": "direct_allowed",
                "organization": "АО Тест", "source_url": contact_url,
                "publication_date": "2026-03-03", "observed_at": "2020-01-01T00:00:00+00:00",
                "status": "confirmed", "confidence": 0.96,
            }],
            "routing_paths": [], "candidates_without_contacts": [],
        },
    }


class FakePrompt:
    def __init__(self, fixtures):
        self.fixtures = fixtures
        self.started = []
        self.finished = []
        self.events = []
        self.contexts = {}
        self.wave_two_started = set()

    async def __call__(self, user_input, **kwargs):
        role = pathlib.Path(kwargs["agent_file"]).stem
        self.started.append(role)
        self.events.append(("start", role))
        self.contexts[role] = user_input
        if role in ("corporate_contour", "secondary_sources"):
            self.wave_two_started.add(role)
            while self.wave_two_started != {"corporate_contour", "secondary_sources"}:
                await asyncio.sleep(0)
        source = {
            "official_sources": "https://company.test/leadership",
            "secondary_sources": "https://media.test/speaker",
            "candidate_contacts": "https://company.test/director-email",
        }.get(role)
        if source:
            call_id = f"{role}-fetch"
            yield _message("assistant", calls=[_call(call_id, "LeadFetch", {"url": source})])
            yield _message("tool", f"URL: {source}\nИсточник: http\n\nстраница открыта", call_id=call_id)
        yield _message("assistant", json.dumps(self.fixtures[role], ensure_ascii=False))
        self.finished.append(role)
        self.events.append(("finish", role))


class FailingPrompt(FakePrompt):
    """Как FakePrompt, но заданная роль всегда падает — для проверки мягкой деградации графа."""

    def __init__(self, fixtures, fail_role):
        super().__init__(fixtures)
        self.fail_role = fail_role

    async def __call__(self, user_input, **kwargs):
        role = pathlib.Path(kwargs["agent_file"]).stem
        if role == self.fail_role:
            self.started.append(role)
            raise RuntimeError(f"смоделированный сбой роли {role}")
            yield  # pragma: no cover — оператор yield делает функцию async-генератором
        async for message in super().__call__(user_input, **kwargs):
            yield message


def test_dependency_graph_and_cache():
    fixtures = _fixtures()
    with tempfile.TemporaryDirectory(prefix="enrichment_test_") as temp:
        checkpoint = pathlib.Path(temp) / "checkpoint.json"
        request = {
            "lead": {"name": "АО Тест", "_inn": "1234567890", "_ogrn": "123",
                     "website": "https://company.test", "contact_person": "Иванов Иван Иванович"},
            "company": "АО Тест", "inn": "1234567890", "model": "test-model",
            "seed": {"process": "PROCESS_SEED", "roles": "SEED_ONLY_PERSON Петров Пётр Петрович"},
            "checkpoint": str(checkpoint), "checkpoint_ttl_h": 72,
        }
        # Реальные старые checkpoints не имели версии/hashes и не должны пережить миграцию.
        checkpoint.write_text(json.dumps({
            "execution": "parallel", "complete": True,
            "roles": {role: {} for role in R.ROLE_ORDER},
        }), encoding="utf-8")
        fake = FakePrompt(fixtures)
        result = asyncio.run(R.run(request, prompt_fn=fake))

        assert result["schema_version"] == R.SCHEMA_VERSION
        assert result["execution"] == "dependency_graph" and result["complete"] is True
        assert set(result["evidence_traces"]) == set(R.ROLE_ORDER)
        assert result["evidence_traces"]["official_sources"]["approved_official_domains"] == [
            "company.test"]
        assert fake.started[0] == "official_sources"
        assert fake.started[-2:] == ["role_candidates", "candidate_contacts"]
        assert set(fake.started[1:3]) == {"corporate_contour", "secondary_sources"}
        position = {event: index for index, event in enumerate(fake.events)}
        assert position[("finish", "official_sources")] < position[("start", "secondary_sources")]
        assert position[("finish", "corporate_contour")] < position[("start", "role_candidates")]
        assert position[("finish", "secondary_sources")] < position[("start", "role_candidates")]
        assert position[("finish", "role_candidates")] < position[("start", "candidate_contacts")]
        assert "person_full_name" in fake.contexts["secondary_sources"]
        assert "Иванов Иван Иванович" in fake.contexts["candidate_contacts"]
        assert "SEED_ONLY_PERSON" not in fake.contexts["candidate_contacts"]
        assert result["roles"]["official_sources"]["confirmed_facts"][0]["observed_at"] != "2020-01-01T00:00:00+00:00"

        async def should_not_run(*args, **kwargs):
            raise AssertionError("валидный checkpoint должен исключить повторный LLM-вызов")
            yield  # pragma: no cover

        checkpoint_mtime = checkpoint.stat().st_mtime_ns
        cached = asyncio.run(R.run(request, prompt_fn=should_not_run))
        assert cached["complete"] is True and cached["roles"] == result["roles"]
        assert checkpoint.stat().st_mtime_ns == checkpoint_mtime, "cache hit не должен продлевать TTL"

        changed_request = copy.deepcopy(request)
        changed_request["lead"]["phone"] = "+7 843 000-00-00"
        changed_prompt = FakePrompt(fixtures)
        asyncio.run(R.run(changed_request, prompt_fn=changed_prompt))
        assert "official_sources" not in changed_prompt.started
        assert set(changed_prompt.started) == {
            "corporate_contour", "secondary_sources", "role_candidates", "candidate_contacts"}

        tampered = json.loads(checkpoint.read_text(encoding="utf-8"))
        tampered["roles"]["candidate_contacts"]["contacts"][0]["value"] = "tampered@invalid.test"
        checkpoint.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
        resumed_prompt = FakePrompt(fixtures)
        resumed = asyncio.run(R.run(changed_request, prompt_fn=resumed_prompt))
        assert resumed_prompt.started == ["candidate_contacts"]
        assert resumed["roles"]["candidate_contacts"]["contacts"][0]["value"] == "director@company.test"


def test_contract_guards():
    fixtures = _fixtures()
    request = {"lead": {"name": "АО Тест", "_inn": "1234567890",
                         "website": "https://company.test"},
               "company": "АО Тест", "inn": "1234567890"}
    completed = {
        "official_sources": fixtures["official_sources"],
        "corporate_contour": fixtures["corporate_contour"],
        "secondary_sources": fixtures["secondary_sources"],
    }

    bad_role = copy.deepcopy(fixtures["role_candidates"])
    bad_role["candidates"][0]["is_current_role_confirmed"] = True
    try:
        R._validate_role_result("role_candidates", bad_role, request, completed, stamp=R._now())
    except R.ContractError:
        pass
    else:
        raise AssertionError("role agent самостоятельно подтвердил текущую должность")

    completed["role_candidates"] = fixtures["role_candidates"]
    bad_contact = copy.deepcopy(fixtures["candidate_contacts"])
    bad_contact["contacts"][0]["candidate_full_name"] = "Неизвестный Новый Человек"
    try:
        R._validate_role_result("candidate_contacts", bad_contact, request, completed, stamp=R._now())
    except R.ContractError:
        pass
    else:
        raise AssertionError("contact agent добавил человека вне role_candidates")

    bad_official = copy.deepcopy(fixtures["official_sources"])
    bad_official["official_domains"] = ["checko.com"]
    bad_official["confirmed_facts"][0]["source_url"] = "https://checko.com/company/123"
    try:
        R._validate_role_result("official_sources", bad_official, request, {}, stamp=R._now())
    except R.ContractError:
        pass
    else:
        raise AssertionError("business aggregator принят как официальный источник")

    no_official_attempt = copy.deepcopy(fixtures["official_sources"])
    no_official_attempt["confirmed_facts"] = []
    no_official_attempt["checked_sources"] = []
    try:
        R._validate_role_result(
            "official_sources", no_official_attempt, request, {}, stamp=R._now())
    except R.ContractError:
        pass
    else:
        raise AssertionError("official agent завершился одними gaps без единого URL-вызова")

    empty_trace = {
        "opened_urls": set(), "attempted_urls": set(), "attempted_hosts": set(),
        "attempted_crawls": set(),
        "opened_pages": [],
    }
    evil_official = copy.deepcopy(fixtures["official_sources"])
    evil_url = "https://evil.example/registry/123"
    evil_official["official_domains"] = ["evil.example"]
    evil_official["confirmed_facts"][0].update({
        "source_url": evil_url, "source_type": "official_company_site"})
    evil_official["checked_sources"][0].update({
        "source_url": evil_url, "source_type": "official_company_site"})
    R._validate_role_result("official_sources", evil_official, request, {}, stamp=R._now())
    evil_trace = copy.deepcopy(empty_trace)
    evil_trace["opened_urls"].add(evil_url)
    evil_trace["attempted_urls"].add(evil_url)
    evil_trace["opened_pages"].append({"url": evil_url, "text": "self-declared"})
    try:
        R._validate_evidence_trace("official_sources", evil_official, evil_trace, {}, request)
    except R.ContractError:
        pass
    else:
        raise AssertionError("self-declared arbitrary domain принят как официальный")

    unavailable = copy.deepcopy(fixtures["official_sources"])
    unavailable_url = "https://company.test/unavailable"
    unavailable["checked_sources"].append({
        "source_type": "official_company_site", "source_url": unavailable_url,
        "outcome": "unavailable", "observed_at": R._now(),
    })
    bad_contour = copy.deepcopy(fixtures["corporate_contour"])
    bad_contour["decision_centers"][0]["source_urls"] = [unavailable_url]
    R._validate_role_result("corporate_contour", bad_contour, request, {}, stamp=R._now())
    try:
        R._validate_evidence_trace(
            "corporate_contour", bad_contour, copy.deepcopy(empty_trace),
            {"official_sources": unavailable}, request)
    except R.ContractError:
        pass
    else:
        raise AssertionError("unavailable upstream URL принят как доказательство")

    crawl_trace = copy.deepcopy(empty_trace)
    crawl_trace["opened_urls"].add("https://company.test/returned")
    try:
        R._validate_evidence_trace(
            "corporate_contour", fixtures["corporate_contour"], crawl_trace, {}, request)
    except R.ContractError:
        pass
    else:
        raise AssertionError("произвольный path однажды crawled host принят как доказательство")

    redirect_trace = copy.deepcopy(empty_trace)
    requested = "https://company.test/redirect"
    returned = "https://evil.example/payload"
    redirect_trace["attempted_urls"].update({requested, returned})
    redirect_trace["opened_urls"].add(returned)
    redirect_trace["opened_pages"].append({"url": returned, "text": "redirected content"})
    redirected_official = copy.deepcopy(fixtures["official_sources"])
    redirected_official["confirmed_facts"][0]["source_url"] = requested
    redirected_official["checked_sources"][0]["source_url"] = requested
    try:
        R._validate_evidence_trace(
            "official_sources", redirected_official, redirect_trace, {}, request)
    except R.ContractError:
        pass
    else:
        raise AssertionError("cross-origin redirect alias принят как evidence URL")

    bad_gap = copy.deepcopy(fixtures["secondary_sources"])
    bad_gap["findings"][0]["official_gap_id"] = "nonexistent_gap"
    try:
        R._validate_role_result(
            "secondary_sources", bad_gap, request,
            {"official_sources": fixtures["official_sources"]}, stamp=R._now())
    except R.ContractError:
        pass
    else:
        raise AssertionError("secondary finding сослался на несуществующий official gap")

    incomplete_roles = copy.deepcopy(fixtures["role_candidates"])
    incomplete_roles["unfilled_functions"] = []
    try:
        R._validate_role_result("role_candidates", incomplete_roles, request, completed, stamp=R._now())
    except R.ContractError:
        pass
    else:
        raise AssertionError("неполное покрытие target functions принято")

    bad_dates = copy.deepcopy(fixtures["role_candidates"])
    bad_dates["candidates"][0]["publication_dates"] = ["2026-01-10"]
    try:
        R._validate_role_result("role_candidates", bad_dates, request, completed, stamp=R._now())
    except R.ContractError:
        pass
    else:
        raise AssertionError("publication_dates не сопоставлены один-к-одному с source_urls")

    disconnected = copy.deepcopy(fixtures["corporate_contour"])
    for suffix in ("А", "Б"):
        disconnected["organizations"].append({
            "name": f"ООО Остров {suffix}", "inn": f"123456789{2 if suffix == 'А' else 3}",
            "relation": "holding", "functions": ["управление"],
            "source_urls": ["https://company.test/leadership"], "status": "probable",
        })
    disconnected["edges"].append({
        "from": "ООО Остров А", "to": "ООО Остров Б", "relation": "holding",
        "source_url": "https://company.test/leadership", "confidence": 0.6,
        "status": "probable",
    })
    try:
        R._validate_role_result("corporate_contour", disconnected, request, {}, stamp=R._now())
    except R.ContractError:
        pass
    else:
        raise AssertionError("оторванный от target компонент корпоративного графа принят")

    bad_org_contact = copy.deepcopy(fixtures["candidate_contacts"])
    bad_org_contact["contacts"][0]["organization"] = "ООО Чужая компания"
    try:
        R._validate_role_result(
            "candidate_contacts", bad_org_contact, request, completed, stamp=R._now())
    except R.ContractError:
        pass
    else:
        raise AssertionError("контакт чужой организации привязан кандидату")

    completed_with_two = copy.deepcopy(completed)
    second_candidate = copy.deepcopy(fixtures["role_candidates"]["candidates"][0])
    second_candidate.update({"full_name": "Петров Пётр Петрович", "target_function": "owner"})
    completed_with_two["role_candidates"]["candidates"].append(second_candidate)
    completed_with_two["role_candidates"]["unfilled_functions"].remove("owner")
    try:
        R._validate_role_result(
            "candidate_contacts", fixtures["candidate_contacts"], request,
            completed_with_two, stamp=R._now())
    except R.ContractError:
        pass
    else:
        raise AssertionError("второй кандидат без contact/gap остался непокрытым")

    aggregator_contact = copy.deepcopy(fixtures["candidate_contacts"])
    aggregator_contact["contacts"][0].update({
        "source_url": "https://checko.com/company/123", "source_context": "other_public_source",
        "status": "confirmed", "confidence": 0.99,
    })
    try:
        R._validate_role_result(
            "candidate_contacts", aggregator_contact, request, completed, stamp=R._now())
    except R.ContractError:
        pass
    else:
        raise AssertionError("агрегаторный контакт замаскирован как other_public_source")


def test_docx_adapter():
    import sys
    sys.path.insert(0, str(ROOT))
    import writer_kimi as writer

    enrichment = {
        "complete": True,
        "roles": _fixtures(),
    }
    payload = {
        "org_name": "АО Тест", "disclaimers": [],
        "leadership_table": [{"fio": "Иванов Иван Иванович", "position": "CEO"}],
        "contacts_table": [{"fio": "Иванов Иван Иванович", "contact": "legacy"}],
        "lpr_profile_title": "Иванов Иван Иванович — CEO", "lpr_profile": "текущий ЛПР",
    }
    writer.apply_research_enrichment(payload, enrichment)
    assert payload["decision_centers_table"]
    assert len(payload["corporate_graph_table"]) == len(
        enrichment["roles"]["corporate_contour"]["edges"]), "граф не должен дублировать рёбра"
    assert payload["role_candidates_table"][0]["current_role"].startswith("не присвоена")
    contact = payload["research_contacts_table"][0]
    assert contact["type"] == "персональный рабочий"
    assert contact["source_context"] == "официальный сайт компании"
    assert contact["best_use"] == "прямой адресат"
    assert contact["outreach_policy"] == "прямой outreach допустим"
    assert payload["research_evidence_table"]
    assert any(row.get("evidence_text") for row in payload["research_evidence_table"])
    assert payload["leadership_table"] == [] and payload["contacts_table"] == []
    assert payload["lpr_profile"] == ""

    try:
        import docx  # noqa: F401
    except ModuleNotFoundError:
        print("SKIP DOCX render: python-docx не установлен в текущем интерпретаторе")
        return
    try:
        import claude_agent_sdk  # noqa: F401
    except ModuleNotFoundError:
        # Рендерер DOCX не использует SDK; локальная тестовая среда может не иметь Claude.
        sdk = types.ModuleType("claude_agent_sdk")
        sdk.query = lambda *args, **kwargs: None
        sdk.tool = lambda *args, **kwargs: (lambda function: function)
        sdk.create_sdk_mcp_server = lambda *args, **kwargs: None
        for name in ("ClaudeAgentOptions", "ClaudeSDKClient", "AssistantMessage", "TextBlock",
                     "ToolUseBlock", "ResultMessage"):
            setattr(sdk, name, type(name, (), {}))
        sys.modules["claude_agent_sdk"] = sdk
    import company_research_agent as cra
    payload.update({
        "tldr": ["Тестовый результат"],
        "departments_table": [],
        "official_contacts": ["info@company.test"],
    })
    with tempfile.TemporaryDirectory(prefix="enrichment_docx_") as temp:
        path = pathlib.Path(temp) / "roles.docx"
        cra._write_roles_contacts_docx(payload, str(path))
        assert path.stat().st_size > 5000
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml").decode("utf-8")
        assert "Корпоративный контур" in xml
        assert "Контактные каналы" in xml
        assert "director@company.test" in xml


def test_partial_degradation():
    """Мягкая деградация: падение роли даёт частичное досье (complete=False), а не исключение."""
    fixtures = _fixtures()

    def _request(checkpoint):
        return {
            "lead": {"name": "АО Тест", "_inn": "1234567890", "_ogrn": "123",
                     "website": "https://company.test", "contact_person": "Иванов Иван Иванович"},
            "company": "АО Тест", "inn": "1234567890", "model": "test-model",
            "seed": {"process": "PROCESS_SEED", "roles": "Петров Пётр Петрович"},
            "checkpoint": str(checkpoint), "checkpoint_ttl_h": 72,
        }

    # 1) Падает лист графа (candidate_contacts): остальные 4 роли сохраняются, run() НЕ бросает.
    with tempfile.TemporaryDirectory(prefix="enrichment_degrade1_") as temp:
        checkpoint = pathlib.Path(temp) / "checkpoint.json"
        result = asyncio.run(R.run(_request(checkpoint),
                                   prompt_fn=FailingPrompt(fixtures, "candidate_contacts")))
        assert result["complete"] is False
        assert set(result["roles_failed"]) == {"candidate_contacts"}
        assert set(result["roles"]) == set(R.ROLE_ORDER) - {"candidate_contacts"}
        assert checkpoint.is_file(), "частичный чекпойнт должен быть записан"

    # 2) Падает корень (official_sources): всё downstream помечается пропущенным, но run() НЕ бросает.
    with tempfile.TemporaryDirectory(prefix="enrichment_degrade2_") as temp:
        checkpoint = pathlib.Path(temp) / "checkpoint.json"
        result = asyncio.run(R.run(_request(checkpoint),
                                   prompt_fn=FailingPrompt(fixtures, "official_sources")))
        assert result["complete"] is False
        assert result["roles"] == {}
        assert set(result["roles_failed"]) == set(R.ROLE_ORDER)


def main():
    tests = (test_dependency_graph_and_cache, test_contract_guards, test_docx_adapter,
             test_partial_degradation)
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print("test_research_enrichment: all checks passed")


if __name__ == "__main__":
    main()
