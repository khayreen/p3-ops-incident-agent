"""Ticket filing. This is the only component that writes anything, and it is
called by the graph's approval step, never by the model."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class ApprovalRequired(PermissionError):
    pass


class TicketSink:
    def __init__(self, path: str | Path = "tickets.jsonl"):
        self.path = Path(path)

    def count(self) -> int:
        if not self.path.exists():
            return 0
        return sum(1 for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip())

    def create(self, report: dict, approved_by: str) -> str:
        if not approved_by or not approved_by.strip():
            raise ApprovalRequired("a ticket cannot be filed without a named human approver")
        ticket_id = f"OPS-{self.count() + 1:04d}"
        record = {
            "ticket_id": ticket_id,
            "filed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "approved_by": approved_by.strip(),
            "job": report["job"],
            "root_cause": report["finding"]["root_cause"],
            "summary": report["finding"]["summary"],
            "recommended_action": report["finding"]["recommended_action"],
            "evidence": report["finding"]["evidence"],
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return ticket_id
