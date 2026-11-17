"""Chat client with tool calling for any OpenAI-compatible API (Groq, Gemini, OpenRouter, Ollama)."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Protocol

import httpx

from .schemas import AssistantTurn, ToolCall

PRESETS: dict[str, dict[str, str]] = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY", "model": "llama-3.3-70b-versatile"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "key_env": "GEMINI_API_KEY", "model": "gemini-2.5-flash"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY", "model": "meta-llama/llama-3.3-70b-instruct:free"},
    "ollama": {"base_url": "http://localhost:11434/v1", "key_env": "", "model": "llama3.2"},
}


class LLMError(RuntimeError):
    pass


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
    def __init__(self, base_url: str, api_key: str, model: str, timeout_s: float = 60, max_retries: int = 4):
        self.base_url, self.api_key, self.name = base_url.rstrip("/"), api_key, model
        self.timeout_s, self.max_retries = timeout_s, max_retries

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
        async with httpx.AsyncClient(timeout=self.timeout_s) as http:
            for attempt in range(self.max_retries):
                try:
                    r = await http.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
                except httpx.HTTPError as exc:
                    last = f"network error: {type(exc).__name__}"
                else:
                    if r.status_code == 200:
                        return parse_turn(r.json())
                    last = f"HTTP {r.status_code}"
                    if r.status_code != 429 and r.status_code < 500:
                        break
                    retry_after = r.headers.get("retry-after", "")
                    if retry_after.replace(".", "", 1).isdigit():
                        await asyncio.sleep(min(float(retry_after), 30))
                        continue
                await asyncio.sleep(2**attempt)
        raise LLMError(f"LLM call failed: {last}")


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
