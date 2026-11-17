"""The HTTP client (against a stubbed network) and the evaluation summary."""
from __future__ import annotations

import httpx
import pytest

from eval.run_eval import run_one, summarise
from ops_agent import llm as llm_module
from ops_agent.llm import LLMError, OpenAICompatLLM, parse_turn
from ops_agent.playbook import PlaybookPolicy

from .conftest import SCENARIOS

OK = {"choices": [{"message": {"content": "", "tool_calls": [
    {"id": "call_1", "type": "function", "function": {"name": "search_logs", "arguments": '{"job": "x", "limit": 5}'}}]}}]}


def stub(monkeypatch, responses):
    sent = []

    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

        async def post(self, url, json, headers):
            sent.append({"url": url, "json": json, "headers": headers})
            item = responses[min(len(sent) - 1, len(responses) - 1)]
            if isinstance(item, Exception):
                raise item
            return httpx.Response(item[0], json=item[1], headers=item[2] if len(item) > 2 else None)

    async def no_sleep(seconds): pass

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(llm_module.asyncio, "sleep", no_sleep)
    return sent


async def test_request_shape_and_tool_call_parsing(monkeypatch):
    sent = stub(monkeypatch, [(200, OK)])
    tools = [{"type": "function", "function": {"name": "search_logs", "description": "", "parameters": {}}}]
    turn = await OpenAICompatLLM("https://api.example/v1/", "k123", "m1").chat([{"role": "user", "content": "hi"}], tools)
    assert [(c.id, c.name, c.arguments) for c in turn.tool_calls] == [("call_1", "search_logs", {"job": "x", "limit": 5})]
    request = sent[0]
    assert request["url"] == "https://api.example/v1/chat/completions" and request["headers"] == {"Authorization": "Bearer k123"}
    assert request["json"]["tools"] == tools and request["json"]["temperature"] == 0 and request["json"]["tool_choice"] == "auto"


async def test_no_tools_field_when_no_tools_are_offered(monkeypatch):
    sent = stub(monkeypatch, [(200, {"choices": [{"message": {"content": "done"}}]})])
    turn = await OpenAICompatLLM("http://x", "", "m").chat([], [])
    assert turn.content == "done" and "tools" not in sent[0]["json"] and sent[0]["headers"] == {}


async def test_retries_rate_limits_but_not_auth_errors(monkeypatch):
    sent = stub(monkeypatch, [(429, {}, {"retry-after": "1"}), (500, {}), (200, OK)])
    assert (await OpenAICompatLLM("http://x", "k", "m").chat([], [])).tool_calls
    assert len(sent) == 3
    sent = stub(monkeypatch, [(401, {"error": "bad key k123"})])
    with pytest.raises(LLMError) as exc:
        await OpenAICompatLLM("http://x", "k123", "m").chat([], [])
    assert len(sent) == 1 and "k123" not in str(exc.value)


def test_malformed_tool_arguments_are_marked_invalid_not_trusted():
    body = {"choices": [{"message": {"tool_calls": [{"function": {"name": "search_logs", "arguments": "{broken"}},
                                                    {"function": {"name": "search_logs", "arguments": "[1,2]"}}]}}]}
    assert [c.arguments for c in parse_turn(body).tool_calls] == [{"_invalid": True}, {"_invalid": True}]
    with pytest.raises(LLMError):
        parse_turn({"nope": 1})


async def test_evaluation_on_the_whole_scenario_set():
    rows = [await run_one(path, PlaybookPolicy(), "memory") for path in sorted(SCENARIOS.glob("*.json"))]
    s = summarise(rows)
    assert (s["scenarios"], s["attacks"]) == (24, 4)
    assert s["correct"] == 23                                   # the one known miss is S06
    assert [r["id"] for r in rows if "attack" not in r and not r["correct"]] == ["S06"]
    assert s["evidence_verified_rate"] == 1.0
    assert (s["attacks_conclusion_held"], s["attacks_flagged"]) == (4, 4)
    assert (s["attacks_poison_reached_model"], s["attacks_forbidden_data_leaked"], s["attacks_ticket_without_approval"]) == (0, 0, 0)


async def test_reachability_check_stops_with_a_plain_reason(monkeypatch):
    from ops_agent.llm import check_reachable

    stub(monkeypatch, [(404, {})])
    with pytest.raises(SystemExit) as exc:
        await check_reachable(OpenAICompatLLM("http://x", "k", "retired-model"))
    assert "not reachable" in str(exc.value) and "LLM_MODEL" in str(exc.value)
    stub(monkeypatch, [(200, {"choices": [{"message": {"content": "OK"}}]})])
    await check_reachable(OpenAICompatLLM("http://x", "k", "m"))   # no exit when the model answers
