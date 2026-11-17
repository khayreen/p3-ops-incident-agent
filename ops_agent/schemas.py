"""Data contracts. The agent's conclusion must fit `Finding` or it is discarded."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class RootCause(str, Enum):
    missing_input_file = "missing_input_file"          # upstream never delivered the file
    config_error = "config_error"                      # the job's own configuration is wrong
    permission_denied = "permission_denied"            # IAM / KMS / bucket policy refused access
    expired_credentials = "expired_credentials"        # token, secret or password no longer valid
    db_connection_failure = "db_connection_failure"    # database unreachable or out of connections
    timeout = "timeout"                                # ran out of time
    out_of_memory = "out_of_memory"                    # ran out of memory
    schema_mismatch = "schema_mismatch"                # input data did not match the expected shape
    unknown = "unknown"                                # not enough evidence to say


class Finding(BaseModel):
    """What the investigator must return. Unknown fields are an error."""

    model_config = ConfigDict(extra="forbid")

    root_cause: RootCause
    summary: str = Field(min_length=1, max_length=600)
    evidence: list[str] = Field(default_factory=list, max_length=8)
    recommended_action: str = Field(default="", max_length=600)
    confidence: float = Field(ge=0.0, le=1.0)


class Report(BaseModel):
    """A finding plus everything the reviewer needs to judge it."""

    job: str
    finding: Finding
    evidence_verified: int = 0          # evidence quotes found word-for-word in tool output
    evidence_total: int = 0
    tool_calls: int = 0
    denied_calls: int = 0
    quarantined_lines: int = 0
    needs_review_reasons: list[str] = Field(default_factory=list)


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict


class AssistantTurn(BaseModel):
    """One model response: either tool calls, or final text."""

    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
