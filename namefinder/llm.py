"""Shared helpers for the generation stages (ideas, names, variants): one JSON chat call with retries."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from . import budget, config, console, net


def parse_json(content: str | None) -> Any | None:
    """The first JSON array or object in a model answer (fences and prose around it are ignored)."""
    if not content or not isinstance(content, str):
        return None
    text = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
    starts = [i for i in (text.find("["), text.find("{")) if i >= 0]
    if not starts:
        return None
    start = min(starts)
    closer = "]" if text[start] == "[" else "}"
    end = text.rfind(closer)
    if end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    if closer == "]":   # truncated array: keep the objects that completed
        last = text.rfind("}")
        if last > start:
            try:
                return json.loads(text[start : last + 1] + "]")
            except json.JSONDecodeError:
                pass
    return None


def resolve(model: str | None, default_model: str, backend: str | None = None) -> tuple[str, str, str]:
    """(api_url, api_key, model) for a backend; OpenRouter when a key exists, else a local server."""
    if not backend:
        backend = "openrouter" if config.secret(config.OPENROUTER_KEY_ENV) else "local"
    if backend in ("local", "lmstudio"):
        return "http://localhost:1234/v1", "lm-studio", model or "qwen3-4b-instruct-2507"
    key = config.secret(config.OPENROUTER_KEY_ENV)
    if not key:
        raise SystemExit(f"OPENROUTER_API_KEY is not set in .env. {config.WRITE_KEY_HINT}")
    return config.OPENROUTER_URL, key, model or default_model


async def chat_json(
    http, url: str, key: str, model: str, system: str, user: str,
    temperature: float = 0.8, max_tokens: int = 4000, label: str = "llm",
) -> Any | None:
    """One chat completion parsed as JSON. Retries on glitches and bad JSON; None when it gives up."""
    headers = {"Authorization": f"Bearer {key or 'local'}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        **config.openrouter_extras(url),
    }
    for attempt in range(5):
        if budget.exhausted():
            return None
        try:
            r = await http.post(f"{url}/chat/completions", json=body, headers=headers)
        except Exception as exc:
            wait_s = min(60.0, 3.0 * (2 ** attempt))
            console.log(f"[{label}] network glitch ({exc.__class__.__name__}); retrying in {wait_s:.0f}s")
            await asyncio.sleep(wait_s)
            continue
        if r.status_code == 429:
            await asyncio.sleep(net.backoff_delay(attempt, r.headers.get("retry-after")))
            continue
        if r.status_code >= 500:
            await asyncio.sleep(min(60.0, 3.0 * (2 ** attempt)))
            continue
        if r.status_code != 200:
            console.log(f"[{label}] HTTP {r.status_code}: {r.text[:160]}")
            return None
        try:
            payload = r.json()
            budget.record(payload)
            parsed = parse_json((payload.get("choices") or [{}])[0].get("message", {}).get("content"))
            if parsed:
                return parsed
        except (ValueError, AttributeError, KeyError, IndexError):
            pass
        await asyncio.sleep(1.5)
    return None
