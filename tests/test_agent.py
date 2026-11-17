"""The investigation graph: tool loop, conclusion, approval, ticket filing."""
from __future__ import annotations

import json

import pytest

from ops_agent.agent import BUDGET_NOTICE, RETRY_NOTICE, SYSTEM_PROMPT, parse_finding
from ops_agent.llm import LLMError
from ops_agent.playbook import PlaybookPolicy
from ops_agent.schemas import AssistantTurn, RootCause, ToolCall
from ops_agent.tickets import ApprovalRequired

from .conftest import ScriptedLLM, final, tool

JOB = "daily-settlement-report"   # S07: permission_denied on this job
QUOTE = "is not authorized to perform: s3:GetObject"


async def test_happy_path_reads_then_concludes(investigate):
    llm = ScriptedLLM(tool("get_recent_runs", job=JOB), tool("search_logs", job=JOB, level="ERROR"), final(evidence=[QUOTE]))
    async with investigate("S07", llm) as (inv, sc):
        report = await inv.run(sc["job"], sc["question"])
    assert report.finding.root_cause is RootCause.permission_denied
    assert (report.tool_calls, report.denied_calls, report.evidence_verified, report.evidence_total) == (2, 0, 1, 1)
    assert report.needs_review_reasons == []
    # The model was given the system prompt, the question, and only read-only tools.
    first_messages, first_tools = llm.calls[0]
    assert first_messages[0] == {"role": "system", "content": SYSTEM_PROMPT} and JOB in first_messages[1]["content"]
    assert {t["function"]["name"] for t in first_tools} == {"list_jobs", "get_job_config", "get_recent_runs", "search_logs", "list_s3_objects"}
    # Tool results come back as tool messages tied to the call.
    assert [m["role"] for m in llm.calls[2][0]] == ["system", "user", "assistant", "tool", "assistant", "tool"]


async def test_several_tool_calls_in_one_turn(investigate):
    both = AssistantTurn(tool_calls=[ToolCall(id="a", name="get_recent_runs", arguments={"job": JOB}),
                                     ToolCall(id="b", name="get_job_config", arguments={"job": JOB})])
    async with investigate("S07", ScriptedLLM(both, final(evidence=["daily-settlement-report-role"]))) as (inv, sc):
        report = await inv.run(sc["job"], sc["question"])
        tool_messages = [m for m in await inv.messages() if m["role"] == "tool"]
    assert report.tool_calls == 2 and [m["tool_call_id"] for m in tool_messages] == ["a", "b"]


async def test_invalid_answer_gets_one_retry(investigate):
    llm = ScriptedLLM(AssistantTurn(content="I think it is permissions."), final(evidence=[]))
    async with investigate("S07", llm) as (inv, sc):
        report = await inv.run(sc["job"], sc["question"])
    assert report.finding.root_cause is RootCause.permission_denied and len(llm.calls) == 2
    assert llm.calls[1][0][-1] == {"role": "user", "content": RETRY_NOTICE} and llm.calls[1][1] == []


@pytest.mark.parametrize(
    "bad",
    [
        AssistantTurn(content="no json here"),
        final(root_cause="aliens"),                       # outside the closed set
        final(action="restart_job", target="prod"),       # extra fields are refused
        final(confidence=3),
        LLMError("HTTP 503"),
    ],
)
async def test_unusable_answers_become_unknown_and_need_review(investigate, sink, bad):
    async with investigate("S07", ScriptedLLM(bad)) as (inv, sc):
        report = await inv.run(sc["job"], sc["question"])
    assert report.finding.root_cause is RootCause.unknown and report.finding.confidence == 0
    assert {"invalid_output", "no_conclusion", "low_confidence"} <= set(report.needs_review_reasons)


async def test_budget_exhaustion_forces_a_conclusion(investigate):
    llm = ScriptedLLM(*[tool("get_recent_runs", job=JOB)] * 3, final(evidence=[]))
    async with investigate("S07", llm, max_calls=3) as (inv, sc):
        report = await inv.run(sc["job"], sc["question"])
    assert report.tool_calls == 3 and len(llm.calls) == 4
    last_messages, last_tools = llm.calls[-1]
    assert last_tools == [] and last_messages[-1] == {"role": "user", "content": BUDGET_NOTICE}


async def test_a_model_that_never_stops_calling_tools_is_cut_off(investigate):
    llm = ScriptedLLM(tool("file_ticket"))  # refused every time, so the call budget is never spent
    async with investigate("S07", llm) as (inv, sc):
        report = await inv.run(sc["job"], sc["question"])
    assert len(llm.calls) <= 15 and report.finding.root_cause is RootCause.unknown
    assert "denied_tool_calls" in report.needs_review_reasons


async def test_fabricated_evidence_is_caught(investigate):
    llm = ScriptedLLM(tool("search_logs", job=JOB, level="ERROR"), final(evidence=[QUOTE, "ERROR: disk full on /dev/sda1"]))
    async with investigate("S07", llm) as (inv, sc):
        report = await inv.run(sc["job"], sc["question"])
    assert (report.evidence_verified, report.evidence_total) == (1, 2)
    assert "unverified_evidence" in report.needs_review_reasons


async def test_evidence_must_come_from_this_investigation(investigate):
    # The quote is real, but the model never ran the search that would have shown it.
    async with investigate("S07", ScriptedLLM(final(evidence=[QUOTE]))) as (inv, sc):
        report = await inv.run(sc["job"], sc["question"])
    assert report.evidence_verified == 0 and "unverified_evidence" in report.needs_review_reasons


# --- approval gate ----------------------------------------------------------------

async def test_no_ticket_exists_before_a_human_decides(investigate, sink):
    async with investigate("S07", ScriptedLLM(final())) as (inv, sc):
        await inv.run(sc["job"], sc["question"])
        assert sink.count() == 0


async def test_approval_files_a_ticket_naming_the_approver(investigate, sink):
    async with investigate("S07", ScriptedLLM(final())) as (inv, sc):
        await inv.run(sc["job"], sc["question"])
        ticket = await inv.decide(True, "Khayreen")
    record = json.loads(sink.path.read_text())
    assert ticket == "OPS-0001" and record["approved_by"] == "Khayreen" and record["root_cause"] == "permission_denied"


@pytest.mark.parametrize("approved,name", [(False, "Khayreen"), (True, ""), (True, "   ")])
async def test_rejection_or_anonymous_approval_files_nothing(investigate, sink, approved, name):
    async with investigate("S07", ScriptedLLM(final())) as (inv, sc):
        await inv.run(sc["job"], sc["question"])
        assert await inv.decide(approved, name) is None
    assert sink.count() == 0


def test_the_ticket_sink_itself_refuses_without_an_approver(sink):
    report = {"job": JOB, "finding": {"root_cause": "timeout", "summary": "s", "recommended_action": "a", "evidence": []}}
    with pytest.raises(ApprovalRequired):
        sink.create(report, "")
    assert sink.count() == 0


# --- parsing and the rule-based policy ---------------------------------------------

def test_parse_finding_accepts_json_wrapped_in_prose():
    wrapped = "Here is my answer:\n```json\n" + final().content + "\n```"
    assert parse_finding(wrapped).root_cause is RootCause.permission_denied
    assert parse_finding("") is None and parse_finding("{not json}") is None


@pytest.mark.parametrize(
    "sid,expected",
    [("S01", "missing_input_file"), ("S04", "config_error"), ("S05", "config_error"), ("S08", "permission_denied"),
     ("S10", "expired_credentials"), ("S14", "db_connection_failure"), ("S16", "timeout"), ("S21", "out_of_memory"),
     ("S23", "schema_mismatch")],
)
async def test_playbook_policy_on_representative_scenarios(investigate, sid, expected):
    async with investigate(sid, PlaybookPolicy()) as (inv, sc):
        report = await inv.run(sc["job"], sc["question"])
    assert report.finding.root_cause.value == expected == sc["truth"]["root_cause"]
    assert report.evidence_verified == report.evidence_total >= 1 and report.denied_calls == 0


async def test_playbook_known_miss_is_reported_honestly_as_unknown(investigate):
    # S06: a config change points DB_HOST at a name that does not resolve. The fixed
    # rules have no pattern for it; the right behaviour is "unknown", not a wrong guess.
    async with investigate("S06", PlaybookPolicy()) as (inv, sc):
        report = await inv.run(sc["job"], sc["question"])
    assert sc["truth"]["root_cause"] == "config_error" and report.finding.root_cause is RootCause.unknown
    assert "no_conclusion" in report.needs_review_reasons
