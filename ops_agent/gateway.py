"""The only path between the model and the tools.

Every tool call the model asks for passes through `ToolGateway.call`, which
enforces, in order:

  1. Allowlist   - only known read-only tools; anything else is refused.
  2. Arguments   - only declared parameters, of the declared type.
  3. Scope       - only the job under investigation, its upstream jobs, and
                   the buckets those jobs are configured to use.
  4. Budget      - a hard cap on the number of calls per investigation.
  5. Quarantine  - instruction-like text in results is withheld from the model.
  6. Audit       - every call, allowed or refused, is recorded.

A refusal is returned to the model as an error result, so it can carry on with
the investigation, and is counted against the case for the human reviewer.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import guard

READ_ONLY_TOOLS = frozenset({"list_jobs", "get_job_config", "get_recent_runs", "search_logs", "list_s3_objects"})
MAX_RESULT_CHARS = 6000
_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool}


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict

    def as_openai(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


@dataclass
class CallRecord:
    step: int
    tool: str
    arguments: dict
    status: str               # ok | denied | error
    reason: str = ""
    result_sha256: str = ""
    result_chars: int = 0
    quarantined: int = 0
    duration_ms: int = 0


@dataclass
class ToolGateway:
    call_tool: Callable[[str, dict], Awaitable[dict]]
    specs: list[ToolSpec]
    allowed_jobs: set[str]
    allowed_buckets: set[str]
    max_calls: int = 12
    records: list[CallRecord] = field(default_factory=list)
    shown: list[str] = field(default_factory=list)   # every string the model was shown

    def model_tools(self) -> list[dict]:
        return [s.as_openai() for s in self.specs if s.name in READ_ONLY_TOOLS]

    @property
    def calls_made(self) -> int:
        return sum(1 for r in self.records if r.status != "denied")

    @property
    def denied(self) -> int:
        return sum(1 for r in self.records if r.status == "denied")

    @property
    def quarantined(self) -> int:
        return sum(r.quarantined for r in self.records)

    def _check(self, name: str, args: Any) -> str:
        """Return a refusal reason, or '' if the call is allowed."""
        spec = next((s for s in self.specs if s.name == name), None)
        if name not in READ_ONLY_TOOLS or spec is None:
            return f"tool '{name}' is not available to this investigation"
        if not isinstance(args, dict):
            return "arguments must be an object"
        properties = spec.parameters.get("properties", {})
        for key, value in args.items():
            if key not in properties:
                return f"unknown argument '{key}'"
            expected = _TYPES.get(properties[key].get("type", ""))
            if expected and (not isinstance(value, expected) or (isinstance(value, bool) and expected is not bool)):
                return f"argument '{key}' has the wrong type"
        for key in spec.parameters.get("required", []):
            if key not in args:
                return f"missing required argument '{key}'"
        if "job" in args and args["job"] not in self.allowed_jobs:
            return f"job is outside this investigation's scope; allowed: {sorted(self.allowed_jobs)}"
        if "bucket" in args and args["bucket"] not in self.allowed_buckets:
            return f"bucket is outside this investigation's scope; allowed: {sorted(self.allowed_buckets)}"
        if self.calls_made >= self.max_calls:
            return "tool budget for this investigation is used up; give your conclusion"
        return ""

    async def call(self, name: str, args: Any) -> dict:
        step = len(self.records) + 1
        safe_args = args if isinstance(args, dict) else {"_invalid": str(args)[:200]}
        reason = self._check(name, args)
        if reason:
            self.records.append(CallRecord(step, name, safe_args, "denied", reason))
            return {"error": reason}

        started = time.perf_counter()
        try:
            raw = await self.call_tool(name, args)
        except Exception as exc:  # a broken tool must not crash the investigation
            self.records.append(CallRecord(step, name, safe_args, "error", type(exc).__name__))
            return {"error": "the tool failed; try a different query"}
        duration = int((time.perf_counter() - started) * 1000)

        clean, withheld = guard.quarantine(raw)
        text = json.dumps(clean, ensure_ascii=False)
        if len(text) > MAX_RESULT_CHARS:
            clean = {"error": "result too large; narrow the query with a filter or a smaller limit"}
        else:
            self.shown.extend(guard.strings_in(clean))
        self.records.append(CallRecord(
            step, name, safe_args, "error" if "error" in raw else "ok",
            result_sha256=hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest(),
            result_chars=len(text), quarantined=withheld, duration_ms=duration,
        ))
        return clean


# --- MCP adapter ------------------------------------------------------------------

async def specs_from_mcp(client) -> list[ToolSpec]:
    listed = await client.list_tools()
    return [ToolSpec(t.name, t.description or "", t.input_schema) for t in listed.tools]


def mcp_caller(client) -> Callable[[str, dict], Awaitable[dict]]:
    async def call(name: str, args: dict) -> dict:
        result = await client.call_tool(name, args)
        text = "".join(getattr(block, "text", "") for block in result.content)
        if result.is_error:
            return {"error": text[:300]}
        try:
            parsed = json.loads(text)
        except ValueError:
            return {"text": text}
        return parsed if isinstance(parsed, dict) else {"result": parsed}

    return call
