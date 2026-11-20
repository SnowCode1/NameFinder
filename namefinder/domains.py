"""Stage 3 - free domain availability over RDAP (JSON over HTTPS, no key).

404 = unregistered, 200 = registered. Paced to <= 2 req/s per registry, with
exponential backoff on 429 and every answer cached in SQLite.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from tqdm import tqdm

from . import config, db, net


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def domains_for(word: str) -> list[tuple[str, str]]:
    """Return [(domain, tld_role)] for one word, in priority order."""
    out: list[tuple[str, str]] = []
    for tld in config.TLDS:
        out.append((f"{word}.{tld}", tld))
    if config.USE_PREFIX:
        out.append((f"{config.USE_PREFIX}{word}.com", "use-com"))
    if config.CHECK_PLAIN_COM:
        out.append((f"{word}.com", "com"))
    return out


def _registry_for(role: str) -> str:
    tld = "com" if role in ("com", "use-com") else role
    return config.REGISTRY_BY_TLD[tld]


def _endpoint(domain: str, role: str) -> str:
    tld = "com" if role in ("com", "use-com") else role
    return config.RDAP_ENDPOINTS[tld].format(domain=domain)


def _detail(status: int, payload: Any) -> str:
    if status == 404:
        desc = ""
        if isinstance(payload, dict):
            desc = " ".join(payload.get("description") or [])
        return desc[:180] or "not found"
    if isinstance(payload, dict):
        name = payload.get("ldhName") or payload.get("handle") or ""
        states = payload.get("status") or []
        if isinstance(states, str):
            states = [states]
        return (f"{name} {','.join(states)}").strip()[:180]
    return ""


async def _check(http, word: str, domain: str, role: str, pacer: net.Pacer,
                 sem: asyncio.Semaphore) -> dict[str, Any]:
    async with sem:
        url = _endpoint(domain, role)
        status, payload = await net.aget_json(http, url, pacer=pacer)
    if status == 200:
        verdict = "registered"
    elif status == 404:
        verdict = "available"
    else:
        verdict = "unknown"
    return {
        "domain": domain,
        "word": word,
        "tld": role,
        "status": verdict,
        "http_status": status,
        "detail": _detail(status, payload),
        "checked_at": _now(),
    }


def _stale(cutoff: str | None) -> bool:
    return cutoff is None


async def _run_async(jobs: list[tuple[str, str, str]], db_path=None, quiet: bool = False) -> dict[str, int]:
    by_registry: dict[str, list[tuple[str, str, str]]] = {}
    for word, domain, role in jobs:
        by_registry.setdefault(_registry_for(role), []).append((word, domain, role))

    counts = {"available": 0, "registered": 0, "unknown": 0}
    results: list[dict[str, Any]] = []
    lock = asyncio.Lock()

    async with net.async_client(timeout=25.0) as http:
        pacers = {r: net.Pacer(config.RDAP_RATE_BY_REGISTRY.get(r, config.RDAP_RATE_PER_SEC)) for r in by_registry}
        # A little concurrency keeps the pipeline busy while the pacer enforces
        # the per-registry request rate.
        sems = {r: asyncio.Semaphore(8) for r in by_registry}

        tasks = [
            asyncio.create_task(_check(http, w, d, role, pacers[reg], sems[reg]))
            for reg, items in by_registry.items()
            for (w, d, role) in items
        ]

        done = 0
        with tqdm(total=len(tasks), desc="Checking domains", unit="domain", ncols=80, disable=quiet) as pbar:
            for coro in asyncio.as_completed(tasks):
                row = await coro
                async with lock:
                    results.append(row)
                    counts[row["status"]] += 1
                    done += 1
                    pbar.set_postfix({"free": counts["available"]})
                    pbar.update(1)
                if len(results) >= 100:
                    with db.session(db_path) as conn:
                        for r in results:
                            db.upsert_domain(conn, r)
                    results.clear()

    if results:
        with db.session(db_path) as conn:
            for r in results:
                db.upsert_domain(conn, r)
    return counts


def run(limit: int | None = None, refresh: bool = False, db_path=None, top_n: int | None = None) -> dict[str, int]:
    qversion = config.QUESTION_VERSION
    with db.session(db_path) as conn:
        has_scores = conn.execute(
            "SELECT 1 FROM scores WHERE qversion = ? LIMIT 1", (qversion,)
        ).fetchone() is not None
        rows = db.ranked_words(conn, qversion, limit=top_n or config.SCORE_TOP_N)
        words = [r["word"] for r in rows][: limit or None]
        cutoff = None if refresh else (
            datetime.now(timezone.utc) - timedelta(days=config.DOMAIN_STALE_DAYS)
        ).isoformat(timespec="seconds")
        cached: dict[str, str] = {}
        if cutoff:
            for r in conn.execute("SELECT domain, checked_at FROM domains WHERE checked_at >= ?", (cutoff,)):
                cached[r["domain"]] = r["checked_at"]

    if not words:
        raise SystemExit("[check-domains] no words found in database - run `prepare` first")

    if not has_scores:
        print(f"[check-domains] words unscored; screening top {len(words)} directly from wordlist")

    jobs: list[tuple[str, str, str]] = []
    for w in words:
        for domain, role in domains_for(w):
            if domain not in cached:
                jobs.append((w, domain, role))

    print(f"[check-domains] words={len(words)} | queued={len(jobs)} | cached={len(words) * len(config.active_domain_roles()) - len(jobs)}")
    try:
        counts = asyncio.run(_run_async(jobs, db_path)) if jobs else {"available": 0, "registered": 0, "unknown": 0}
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[check-domains] Domain checks paused by user (Ctrl+C). Completed checks saved to database.")
        counts = {"available": 0, "registered": 0, "unknown": 0}

    with db.session(db_path) as conn:
        passing = db.words_passing_domains(conn, qversion, limit=limit)
        print(f"\n[check-domains] query summary: {counts['available']} free, {counts['registered']} taken")
        print(f"[check-domains] words with at least one free name: {len(passing)}")
        for r in passing[:20]:
            print(f"  {r['word']:<10} score={r['composite']:.3f}  {r['domain_state']}")
    return counts
