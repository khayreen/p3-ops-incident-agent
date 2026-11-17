"""Investigate one scenario from the terminal, with a human approval prompt.

  python -m ops_agent.cli scenarios/S04_config_error.json
  python -m ops_agent.cli scenarios/A02_attack_out_of_scope_bucket.json --policy llm --transport stdio
"""
from __future__ import annotations

import argparse
import asyncio
import json

from .llm import OpenAICompatLLM
from .playbook import PlaybookPolicy
from .runner import open_investigation, write_trace
from .tickets import TicketSink


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario")
    ap.add_argument("--policy", choices=["playbook", "llm"], default="playbook")
    ap.add_argument("--transport", choices=["memory", "stdio"], default="stdio")
    ap.add_argument("--approve-as", default=None, help="approve without prompting, as this name")
    ap.add_argument("--reject", action="store_true", help="reject without prompting")
    ap.add_argument("--trace", default="trace.jsonl")
    ap.add_argument("--tickets", default="tickets.jsonl")
    args = ap.parse_args()

    llm = OpenAICompatLLM.from_env() if args.policy == "llm" else PlaybookPolicy()
    async with open_investigation(args.scenario, llm, TicketSink(args.tickets), args.transport) as (investigator, scenario):
        print(f"\n{scenario['question']}\nInvestigator: {llm.name}   MCP transport: {args.transport}\n")
        report = await investigator.run(scenario["job"], scenario["question"])

        print("Tool calls")
        for r in investigator.gateway.records:
            note = f"  <- {r.reason}" if r.status == "denied" else (f"  ({r.quarantined} line(s) withheld)" if r.quarantined else "")
            print(f"  {r.step:>2}. [{r.status:<6}] {r.tool}({json.dumps(r.arguments)}){note}")

        f = report.finding
        print(f"\nRoot cause : {f.root_cause.value}   (confidence {f.confidence:.0%})")
        print(f"Summary    : {f.summary}")
        for quote in f.evidence:
            print(f"Evidence   : {quote}")
        print(f"Verified   : {report.evidence_verified} of {report.evidence_total} quotes found word-for-word in tool output")
        print(f"Next step  : {f.recommended_action}")
        if report.needs_review_reasons:
            print(f"REVIEW     : {', '.join(report.needs_review_reasons)}")

        if args.reject:
            approver = ""
        elif args.approve_as is not None:
            approver = args.approve_as
        else:
            answer = input("\nFile a ticket with this finding? Type your name to approve, or press Enter to reject: ")
            approver = answer.strip()
        ticket = await investigator.decide(bool(approver), approver)
        print(f"\nTicket filed: {ticket} (approved by {approver})" if ticket else "\nNo ticket filed.")
        write_trace(args.trace, scenario, investigator, report, ticket)
        print(f"Trace appended to {args.trace}")


if __name__ == "__main__":
    asyncio.run(main())
