"""Wires a scenario, an MCP connection, the gateway and the graph together."""
from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from mcp import Client, StdioServerParameters

from .agent import Investigator
from .gateway import ToolGateway, mcp_caller, specs_from_mcp
from .llm import ChatModel
from .mcp_server import build_server
from .tickets import TicketSink
from .world import World


def load_scenario(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


@asynccontextmanager
async def open_investigation(scenario_path: str | Path, llm: ChatModel, sink: TicketSink,
                             transport: str = "memory", max_calls: int = 12):
    """Yield (investigator, scenario). `transport` is 'memory' (in-process MCP)
    or 'stdio' (the MCP server as a separate process, as in a real deployment)."""
    scenario = load_scenario(scenario_path)
    world = World(scenario["world"])
    if transport == "stdio":
        target = StdioServerParameters(command=sys.executable, args=["-m", "ops_agent.mcp_server", "--scenario", str(scenario_path)])
    else:
        target = build_server(world)
    async with Client(target) as client:
        # Scope comes from configuration the investigator trusts (the job registry),
        # never from anything the model or the logs say.
        jobs, buckets = world.scope_for(scenario["job"])
        gateway = ToolGateway(mcp_caller(client), await specs_from_mcp(client), jobs, buckets, max_calls=max_calls)
        yield Investigator(llm, gateway, sink), scenario


def write_trace(path: str | Path, scenario: dict, investigator: Investigator, report, ticket_id: str | None) -> None:
    record = {
        "scenario": scenario["id"], "job": scenario["job"],
        "tool_calls": [asdict(r) for r in investigator.gateway.records],
        "report": report.model_dump(mode="json"), "ticket_id": ticket_id,
    }
    with Path(path).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
