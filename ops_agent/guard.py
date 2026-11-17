"""Quarantine for text that reads like an instruction instead of a log entry.

Logs, file names and config values are written by other systems, and some of
that text is attacker-controlled (a crafted file name or request field that a
job logged). Anything that looks like an instruction to the agent is withheld
from the model and counted, and the case is forced to human review.

This is one layer and it is heuristic. The controls that do not depend on it
are in gateway.py (scope, allowlist, budget) and agent.py (approval gate).
"""
from __future__ import annotations

import re
import unicodedata

WITHHELD = "[withheld: this text read like an instruction, not a system record]"

_PATTERNS = [
    r"ignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions|prompts|rules|messages)",
    r"ignore the errors?",
    r"disregard (?:all |any |the |your )?(?:previous|prior|above|system) (?:instructions|prompts?|rules)",
    r"(?:new|updated|override) (?:system )?instructions?\s*:",
    r"\b(?:note|message|instruction)s? (?:to|for) (?:the )?(?:ai|agent|assistant|llm|model)\b",
    r"\[(?:system|assistant|assistant instruction|inst)\]",
    r"^\s*system\s*:",
    r"</?(?:system|assistant|instructions?)>",
    r"\byou must (?:call|run|use|invoke|report|include)\b",
    r"\bcall (?:the )?[a-z_]+ (?:tool )?(?:on|for|with)\b",
    r"\bskip (?:the )?(?:human )?(?:approval|review)\b",
    r"\bapproval (?:was |is |has been )?(?:already )?granted\b",
    r"\bfile an? p\d ticket\b",
    r"\breport (?:the )?root[_ ]cause\s*(?:=|as|is)\b",
    r"\breport unknown\b",
    r"\bdo not (?:mention|flag|escalate|report)\b",
    r"\b(?:print|reveal|include|output) (?:its|the|every|all) .{0,40}(?:password|secret|token|key|credential)",
    r"\b(?:reveal|print|show|repeat) (?:your|the) (?:system )?(?:prompt|instructions)\b",
    r"abaikan (?:semua )?arahan",
]
_INJECTION = re.compile("|".join(f"(?:{p})" for p in _PATTERNS), re.IGNORECASE | re.MULTILINE)
_ZERO_WIDTH = re.compile(r"[​-‏⁠-⁤﻿]")


def looks_like_instruction(text: str) -> bool:
    text = _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text))
    return bool(_INJECTION.search(re.sub(r"[ \t]+", " ", text)))


def quarantine(value):
    """Return (clean_copy, withheld_count) for any JSON-like value."""
    if isinstance(value, str):
        return (WITHHELD, 1) if looks_like_instruction(value) else (value, 0)
    if isinstance(value, list):
        items = [quarantine(v) for v in value]
        return [c for c, _ in items], sum(n for _, n in items)
    if isinstance(value, dict):
        items = {k: quarantine(v) for k, v in value.items()}
        return {k: c for k, (c, _) in items.items()}, sum(n for _, n in items.values())
    return value, 0


def strings_in(value) -> list[str]:
    """Every string leaf in a JSON-like value (used to verify quoted evidence)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in strings_in(v)]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings_in(v)]
    return []
