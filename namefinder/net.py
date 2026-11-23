"""Polite HTTP helpers: per-host pacing, retry with backoff, no key logging."""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx

USER_AGENT = "namefinder/0.1 (brand research; contact: local user)"


class Pacer:
    """Enforces a minimum interval between requests to one registry."""

    def __init__(self, rate_per_sec: float) -> None:
        self.interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self.interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            if now < self._next:
                await asyncio.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self.interval


class SyncPacer:
    """Blocking version of :class:`Pacer`."""

    def __init__(self, rate_per_sec: float) -> None:
        self.interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._next = 0.0

    def wait(self) -> None:
        if self.interval <= 0:
            return
        now = time.monotonic()
        if now < self._next:
            time.sleep(self._next - now)
            now = time.monotonic()
        self._next = now + self.interval


def backoff_delay(attempt: int, retry_after: str | None = None) -> float:
    """Honour Retry-After when present, else exponential with jitter."""
    if retry_after:
        try:
            return min(30.0, max(0.0, float(retry_after)))  # a registry must not stall us for minutes
        except ValueError:
            pass
    return min(60.0, (2 ** attempt) * 0.75) + random.uniform(0, 0.4)


def llm_backoff_delay(attempt: int, retry_after: str | None = None) -> float:
    """429 wait for LLM calls: providers with per-minute token limits need tens of seconds, not seconds."""
    if retry_after:
        try:
            return min(90.0, max(1.0, float(retry_after)))
        except ValueError:
            pass
    return min(90.0, 8.0 * (2 ** attempt)) + random.uniform(0, 3.0)


def client(timeout: float = 20.0, **kwargs: Any) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=10.0),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
        **kwargs,
    )


def async_client(timeout: float = 20.0, **kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=10.0),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
        **kwargs,
    )


def get_json(
    http: httpx.Client,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    attempts: int = 4,
    pacer: SyncPacer | None = None,
) -> tuple[int, Any]:
    """GET returning (status, parsed-json-or-None). Retries 429/5xx/network."""
    last_status = 0
    for attempt in range(attempts):
        if pacer:
            pacer.wait()
        try:
            r = http.get(url, params=params, headers=headers)
        except httpx.HTTPError:
            if attempt == attempts - 1:
                return 0, None
            time.sleep(backoff_delay(attempt))
            continue

        last_status = r.status_code
        if r.status_code in (200, 201):
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, None
        if r.status_code in (404, 400, 401, 403, 422):
            # Definitive answers: do not burn retries on them.
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, None
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(backoff_delay(attempt, r.headers.get("retry-after")))
            continue
        return r.status_code, None
    return last_status, None


async def aget_json(
    http: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    attempts: int = 4,
    pacer: Pacer | None = None,
) -> tuple[int, Any]:
    """Async twin of :func:`get_json`."""
    last_status = 0
    for attempt in range(attempts):
        if pacer:
            await pacer.wait()
        try:
            r = await http.get(url, params=params, headers=headers)
        except httpx.HTTPError:
            if attempt == attempts - 1:
                return 0, None
            await asyncio.sleep(backoff_delay(attempt))
            continue

        last_status = r.status_code
        if r.status_code in (200, 201):
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, None
        if r.status_code in (404, 400, 401, 403, 422):
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, None
        if r.status_code == 429 or r.status_code >= 500:
            await asyncio.sleep(backoff_delay(attempt, r.headers.get("retry-after")))
            continue
        return r.status_code, None
    return last_status, None
