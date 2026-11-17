"""Chat client with tool calling for any OpenAI-compatible API (Groq, Gemini, OpenRouter, Ollama)."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Protocol

import httpx

from .schemas import AssistantTurn, ToolCall

PRESETS: dict[str, dict[str, str]] = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY", "model": "openai/gpt-oss-120b"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "key_env": "GEMINI_API_KEY", "model": "gemini-2.5-flash"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY", "model": "meta-llama/llama-3.3-70b-instruct:free"},
    "ollama": {"base_url": "http://localhost:11434/v1", "key_env": "", "model": "llama3.2"},
}


class LLMError(RuntimeError):
    """A call that returned no model answer.

    `model_fault` is True only when the model itself produced something the API
    rejected (a malformed tool call). Rate limits, network and server errors are
    not the model's answer, so an evaluation must not score them as one.
    `daily_limit` is True when the provider's daily quota is used up."""

    def __init__(self, message: str, *, model_fault: bool = False, daily_limit: bool = False):
        super().__init__(message)
        self.model_fault, self.daily_limit = model_fault, daily_limit


HINTS = {
    "per day": "the free daily token limit is used up; it resets within 24 hours, then run again with --resume",
    "HTTP 401": "the API key was rejected; check it in .env",
    "HTTP 403": "the API key is not allowed to use this model",
    "HTTP 404": "the model name was not found; providers retire models, so set LLM_MODEL in .env to a current one",
    "HTTP 429": "rate limit reached; wait a minute and use --resume",
}


def explain(error: Exception) -> str:
    """A plain-words reason for a failed call, for the person running a script."""
    text = str(error)
    return next((f"{text} ({hint})" for code, hint in HINTS.items() if code in text), text)


async def check_reachable(llm) -> None:
    """One test call. Without it, a wrong key or model name would turn every
    investigation into "unknown" and the report would not be an LLM result."""
    try:
        await llm.chat([{"role": "user", "content": "Reply with OK."}], [])
    except LLMError as exc:
        raise SystemExit(f"The LLM is not reachable, so nothing was run.\n  {explain(exc)}")


class ChatModel(Protocol):
    name: str

    async def chat(self, messages: list[dict], tools: list[dict]) -> AssistantTurn: ...


def read_dotenv(path: str = ".env") -> None:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class OpenAICompatLLM:
    def __init__(self, base_url: str, api_key: str, model: str, timeout_s: float = 60, max_retries: int = 8):
        self.base_url, self.api_key, self.name = base_url.rstrip("/"), api_key, model
        self.timeout_s, self.max_retries = timeout_s, max_retries
        self.tokens_used = 0

    @classmethod
    def from_env(cls) -> "OpenAICompatLLM":
        read_dotenv()
        provider = os.getenv("LLM_PROVIDER", "groq").lower()
        if provider not in PRESETS:
            raise LLMError(f"LLM_PROVIDER must be one of {sorted(PRESETS)}")
        preset = PRESETS[provider]
        key = os.getenv(preset["key_env"], "") if preset["key_env"] else ""
        if preset["key_env"] and not key:
            raise LLMError(f"Set {preset['key_env']} in your environment or .env file")
        return cls(os.getenv("LLM_BASE_URL", preset["base_url"]), key, os.getenv("LLM_MODEL", preset["model"]))

    async def chat(self, messages: list[dict], tools: list[dict]) -> AssistantTurn:
        payload: dict = {"model": self.name, "temperature": 0, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        last = "no attempt made"
        bad_tool_calls = 0
        async with httpx.AsyncClient(timeout=self.timeout_s) as http:
            for attempt in range(self.max_retries):
                try:
                    r = await http.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
                except httpx.HTTPError as exc:
                    last = f"network error: {type(exc).__name__}"
                    await asyncio.sleep(min(2**attempt, 30))
                    continue
                if r.status_code == 200:
                    body = r.json()
                    usage = body.get("usage") if isinstance(body, dict) else None
                    if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
                        self.tokens_used += usage["total_tokens"]
                    return parse_turn(body)
                code, detail = error_detail(r)
                last = f"HTTP {r.status_code}" + (f" {code}" if code else "") + (f": {detail}" if detail else "")
                if r.status_code == 429:
                    if "per day" in detail.lower():
                        raise LLMError(f"LLM call failed: {last}", daily_limit=True)
                    await asyncio.sleep(retry_wait(r, attempt))
                    continue
                if r.status_code == 400 and code == "tool_use_failed":
                    # The model wrote a tool call the API could not parse. Ask again;
                    # if it keeps happening, that is the model's own failure.
                    bad_tool_calls += 1
                    if bad_tool_calls >= 3:
                        raise LLMError(f"LLM call failed: {last}", model_fault=True)
                    continue
                if r.status_code >= 500:
                    await asyncio.sleep(min(2**attempt, 30))
                    continue
                break
        raise LLMError(f"LLM call failed: {last}")


def error_detail(r: httpx.Response) -> tuple[str, str]:
    """The provider's error code and a short message, without echoing the request."""
    try:
        err = r.json().get("error")
    except ValueError:
        return "", ""
    if not isinstance(err, dict):
        return "", ""
    code = err.get("code") if isinstance(err.get("code"), str) else ""
    message = err.get("message") if isinstance(err.get("message"), str) else ""
    return code, message[:200]


def retry_wait(r: httpx.Response, attempt: int) -> float:
    """Seconds to wait after a rate limit: the provider's retry-after, else back off."""
    retry_after = r.headers.get("retry-after", "")
    try:
        return min(max(float(retry_after), 1.0), 90.0)
    except ValueError:
        return min(5.0 * 2**attempt, 60.0)


def parse_turn(body: dict) -> AssistantTurn:
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("unexpected response shape") from exc
    calls = []
    for i, call in enumerate(message.get("tool_calls") or []):
        fn = call.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = None  # the gateway refuses non-object arguments
        calls.append(ToolCall(id=call.get("id") or f"call_{i}", name=fn.get("name", ""), arguments=args if isinstance(args, dict) else {"_invalid": True}))
    return AssistantTurn(content=message.get("content") or "", tool_calls=calls)
