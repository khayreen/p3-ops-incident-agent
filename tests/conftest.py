from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from ops_agent.runner import open_investigation
from ops_agent.schemas import AssistantTurn, ToolCall
from ops_agent.tickets import TicketSink

SCENARIOS = Path(__file__).parent.parent / "scenarios"


def scenario_path(sid: str) -> Path:
    return next(SCENARIOS.glob(f"{sid}_*.json"))


def tool(name: str, **args) -> AssistantTurn:
    return AssistantTurn(tool_calls=[ToolCall(id=f"c_{name}", name=name, arguments=args)])


def final(root_cause: str = "permission_denied", evidence: list[str] | None = None, confidence: float = 0.9, **extra) -> AssistantTurn:
    body = {"root_cause": root_cause, "summary": "Test summary.", "evidence": evidence or [],
            "recommended_action": "Test action.", "confidence": confidence, **extra}
    return AssistantTurn(content=json.dumps(body))


class ScriptedLLM:
    """Plays back a fixed list of turns and records what it was sent."""

    name = "scripted"

    def __init__(self, *turns):
        self.turns = list(turns)
        self.calls: list[tuple[list[dict], list[dict]]] = []

    async def chat(self, messages, tools):
        self.calls.append((json.loads(json.dumps(messages)), tools))
        turn = self.turns[min(len(self.calls) - 1, len(self.turns) - 1)]
        if isinstance(turn, Exception):
            raise turn
        return turn


class GullibleLLM:
    """A worst-case model: it does whatever text in tool output tells it to.

    Used to prove that the structural defences hold even when the model is
    fully compromised by a prompt injection."""

    name = "gullible"

    def __init__(self, job: str):
        self.job = job
        self.tried: set[str] = set()
        self.loot: list[str] = []

    async def chat(self, messages, tools):
        tool_text = " ".join(m["content"] for m in messages if m["role"] == "tool")
        if not any(m["role"] == "tool" for m in messages):
            return tool("search_logs", job=self.job, level="ERROR", limit=30)
        self.loot = [m["content"] for m in messages if m["role"] == "tool"][1:]

        def once(key: str) -> bool:
            if key in self.tried:
                return False
            self.tried.add(key)
            return True

        if tools:
            if "contoh-customer-pii-prod" in tool_text and once("pii"):
                return tool("list_s3_objects", bucket="contoh-customer-pii-prod")
            if "payroll-disbursement" in tool_text and once("payroll"):
                return tool("get_job_config", job="payroll-disbursement")
            if "skip human approval" in tool_text and once("ticket"):
                return tool("file_ticket", severity="P1", skip_approval=True)
        cause = "config_error" if "root cause is config_error" in tool_text else "unknown"
        return AssistantTurn(content=json.dumps({
            "root_cause": cause, "summary": ("Obeyed: " + " ".join(self.loot))[:590], "evidence": [],
            "recommended_action": "none", "confidence": 1.0,
        }))


@pytest.fixture
def sink(tmp_path):
    return TicketSink(tmp_path / "tickets.jsonl")


@pytest.fixture
def investigate(sink):
    """investigate(sid, llm) -> async context yielding (investigator, scenario)."""

    @asynccontextmanager
    async def _open(sid: str, llm, **kwargs):
        async with open_investigation(scenario_path(sid), llm, sink, **kwargs) as pair:
            yield pair

    return _open
