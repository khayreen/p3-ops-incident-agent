"""A rule-based investigator with no language model.

It speaks the same interface as the LLM (it returns tool calls, then a final
JSON answer), so it runs through the same graph, gateway and approval step.
Two uses:

  1. A baseline: how far do fixed if-then rules get on the scenarios?
  2. A way to run and test the whole system with no API key.

It follows a fixed runbook and decides by matching error text, which is exactly
what a language model should improve on.
"""
from __future__ import annotations

import json
import re

from .schemas import AssistantTurn, ToolCall


class PlaybookPolicy:
    name = "rule-based playbook"

    async def chat(self, messages: list[dict], tools: list[dict]) -> AssistantTurn:
        job = re.search(r"'([a-z0-9-]+)'", next(m["content"] for m in messages if m["role"] == "user")).group(1)
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        step = len(results)

        def call(name: str, **args) -> AssistantTurn:
            return AssistantTurn(tool_calls=[ToolCall(id=f"pb_{step}", name=name, arguments=args)])

        if not tools:  # budget exhausted: answer with what we have
            return self._answer(job, results)
        if step == 0:
            return call("get_recent_runs", job=job, limit=5)
        if step == 1:
            return call("get_job_config", job=job)
        if step == 2:
            return call("search_logs", job=job, level="ERROR", limit=30)
        if step == 3:
            return call("search_logs", job=job, level="WARN", limit=30)

        runs, config = results[0].get("runs", []), results[1].get("config", {})
        date = runs[0]["started_at"][:10] if runs else ""
        source = config.get("input", {})
        upstream = (config.get("upstream") or [None])[0]
        if step == 4 and source:
            return call("list_s3_objects", bucket=source["bucket"], prefix=source["prefix"].replace("{date}", date))
        if step == 5 and upstream:
            return call("get_job_config", job=upstream)
        if step == 6 and upstream:
            out = results[5].get("config", {}).get("output", {})
            # Only worth a call when upstream writes somewhere other than where this job reads.
            if out and (out["bucket"], out["prefix"]) != (source.get("bucket"), source.get("prefix")):
                return call("list_s3_objects", bucket=out["bucket"], prefix=out["prefix"].replace("{date}", date))
        return self._answer(job, results)

    @staticmethod
    def _answer(job: str, results: list[dict]) -> AssistantTurn:
        def get(i: int) -> dict:
            return results[i] if len(results) > i else {}

        runs, config = get(0).get("runs", []), get(1).get("config", {})
        run = runs[0] if runs else {}
        date = (run.get("started_at") or "")[:10]
        errors = [l["msg"] for l in get(2).get("lines", []) if l["ts"].startswith(date) and "END run" not in l["msg"]]
        text = " | ".join(errors).lower()
        run_error = (run.get("error") or "").lower()
        input_listing = get(4).get("objects", [])
        upstream_listing = get(6).get("objects", [])

        def has(*needles: str) -> bool:
            return any(n in text for n in needles)

        evidence = errors[:2]
        if "timed out after" in run_error or run.get("duration_ms", 0) >= config.get("timeout_s", 1e9) * 1000:
            cause, why, evidence = "timeout", "The run reached its configured time limit.", [run.get("error") or ""]
        elif "killed" in run_error or "memoryerror" in run_error or has("memoryerror") or run.get("max_memory_mb", 0) >= config.get("memory_mb", 1e9):
            cause, why, evidence = "out_of_memory", "The run reached its configured memory limit.", [run.get("error") or ""] + errors[:1]
        elif has("accessdenied", "not authorized"):
            cause, why = "permission_denied", "Access was refused for the job's role."
        elif has("expiredtoken", "password authentication failed", "token expired", "401 unauthorized"):
            cause, why = "expired_credentials", "A credential used by the job was no longer valid."
        elif has("nosuchbucket"):
            cause, why = "config_error", "The job is configured with a bucket that does not exist."
        elif has("nosuchkey"):
            if not input_listing and upstream_listing:
                cause, why = "config_error", "The input file exists where upstream writes it, but not at the path this job is configured to read."
            elif not input_listing:
                cause, why = "missing_input_file", "The input file for the run date was never delivered."
            else:
                cause, why = "unknown", "The job reported a missing key, but the file is listed."
        elif has("connection refused", "too many clients", "remaining connection slots", "connection timed out", "could not connect"):
            cause, why = "db_connection_failure", "The job could not get a database connection."
        elif has("keyerror", "valueerror", "parsererror", "expected ") and errors:
            cause, why = "schema_mismatch", "The input data did not match the expected format."
        else:
            cause, why = "unknown", "No rule matched the evidence."

        answer = {
            "root_cause": cause, "summary": f"{job}: {why}", "evidence": list(dict.fromkeys(e for e in evidence if e))[:5],
            "recommended_action": "Review the quoted evidence and confirm before acting.",
            "confidence": 0.3 if cause == "unknown" else 0.8,
        }
        return AssistantTurn(content=json.dumps(answer))
