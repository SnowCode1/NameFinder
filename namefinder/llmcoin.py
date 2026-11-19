"""LLM coining engine: ask a model to invent short, verb-like names from the best base words.

The model only proposes. Every name still has to pass the same quality gate as the
rule engines (`coin.acceptable`), is de-duplicated against the database, and is
then scored, domain-checked and learned from like any other engine. Names are
stored with engine='llm' and the parent word, so engine learning can adapt how
much of each cycle comes from here.

Feedback loop: the prompt shows the model which earlier LLM names scored best and
worst, plus the names our filters recently rejected, so its style drifts toward
what the scorer rewards.
"""

from __future__ import annotations

import asyncio
import math
import random
import re
import sys
from collections import deque
from typing import Any, Sequence

from tqdm import tqdm

from . import budget, coin, config, db, net, score

DEFAULT_COIN_PROMPT = (
    "You are a brand-naming linguist inventing names for a software product.\n"
    "Product context: " + config.PRODUCT_BLURB + "\n\n"
    "A great name is a coined word that feels like it already exists: elegant, easy to say, and visibly built "
    "from a real idea. Think Spotify, Twilio, Kubernetes, Figma, Vercel, Sentry, Zapier.\n"
    "Bonus: it works as a verb in \"I just ___ it and found what I needed\" (googled, zoomed, pinged).\n\n"
    "Rules:\n"
    "- 5 to 8 letters, two syllables preferred.\n"
    "- Build each name from the parent's meaning: use a Latin/Greek/Romance root, a meaningful fragment, or a clean "
    "blend with a second concept word. The reader should half-recognise the idea.\n"
    "- It must read like a natural word of English or another European language. No k-for-c respellings, no stacked "
    "consonants, no stray suffix endings like -et, -ek, -le, -ly, -ix. Prefer ending on a consonant or a silent e "
    "(-r, -n, -t, -k, -x, -p, -m, -e) and avoid the generic -a / -ara / -ora / -ia pattern.\n"
    "- Not an ordinary dictionary word, a known brand, or a crude sound-alike.\n"
    "- Every name for a parent must use a different technique or fragment; no near-copies.\n"
    "- Vary the endings: no more than two names per parent may share the same last two letters."
)

STYLE_ANCHORS = "ping, grep, zoom, slack, figma, notion, vercel, sentry"

_RECENT_REJECTS: deque[str] = deque(maxlen=24)


def feedback_examples(conn, qversion: str, n: int = 6) -> tuple[list[str], list[str]]:
    """(best, worst) recent LLM-coined names by composite; falls back to the best coined names overall."""
    rows = conn.execute(
        """
        SELECT w.word, w.engine, s.composite FROM words w
        JOIN scores s ON s.word = w.word AND s.qversion = ?
        WHERE w.source = 'coined'
        ORDER BY s.composite DESC
        """,
        (qversion,),
    ).fetchall()
    llm = [r["word"] for r in rows if r["engine"] == "llm"]
    if len(llm) >= 2 * n:
        return llm[:n], llm[-n:]
    best = [r["word"] for r in rows][:n]
    return (best if len(best) >= n else []), []


def build_user_prompt(
    parents: Sequence[str], per_parent: int, min_len: int, max_len: int,
    best: Sequence[str], worst: Sequence[str], rejected: Sequence[str],
) -> str:
    lines = [
        f"Parents: {', '.join(parents)}",
        "",
        f"For each parent invent {per_parent} different candidate names, {min_len} to {max_len} letters, "
        "lowercase letters only.",
        f"Real-world style anchors (do not output these): {STYLE_ANCHORS}.",
    ]
    if best:
        lines.append(f"Names our scorer rated highest so far (match their feel, do not copy): {', '.join(best)}.")
    if worst:
        lines.append(f"Names it rated lowest (avoid this style): {', '.join(worst)}.")
    if rejected:
        lines.append(
            "Names our filters rejected recently, usually for being real words, hard to pronounce or crude: "
            + ", ".join(rejected) + "."
        )
    lines += [
        "",
        'Return a JSON array with one object per parent, in order: {"word": "<parent>", "names": ["...", "..."]}',
        "Return ONLY the raw JSON array. No markdown fences, no prose.",
    ]
    return "\n".join(lines)


async def _ask(http, url: str, headers: dict, model: str, system: str, user: str, temperature: float):
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": 3000,
        **config.openrouter_extras(url),
    }
    for attempt in range(5):
        if budget.exhausted():
            return None
        try:
            r = await http.post(f"{url}/chat/completions", json=body, headers=headers)
        except Exception as exc:
            wait_s = min(60.0, 3.0 * (2 ** attempt))
            tqdm.write(f"[llm-coin] network glitch ({exc.__class__.__name__}); retrying in {wait_s:.0f}s", file=sys.stderr)
            await asyncio.sleep(wait_s)
            continue
        if r.status_code == 429:
            await asyncio.sleep(net.backoff_delay(attempt, r.headers.get("retry-after")))
            continue
        if r.status_code >= 500:
            await asyncio.sleep(min(60.0, 3.0 * (2 ** attempt)))
            continue
        if r.status_code != 200:
            tqdm.write(f"[llm-coin] HTTP {r.status_code}: {r.text[:160]}", file=sys.stderr)
            return None
        try:
            payload = r.json()
            budget.record(payload)
            content = (payload.get("choices") or [{}])[0].get("message", {}).get("content")
            parsed = score._parse_scores(content)
            if parsed:
                return parsed
        except (ValueError, AttributeError, KeyError, IndexError):
            pass
        await asyncio.sleep(1.5)
    return None


def _clean(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    w = name.strip().lower()
    return w if re.fullmatch(r"[a-z]{3,16}", w) else None


async def _propose(
    chunks: list[list[str]], url: str, key: str, model: str, system: str,
    per_parent: int, min_len: int, max_len: int,
    best: Sequence[str], worst: Sequence[str], rejected: Sequence[str],
    temperature: float, concurrency: int,
) -> list[tuple[str, str]]:
    """Run the proposal requests concurrently. Returns raw (name, parent) pairs."""
    headers = {"Authorization": f"Bearer {key or 'local'}", "Content-Type": "application/json"}
    sem = asyncio.Semaphore(max(1, concurrency))
    out: list[tuple[str, str]] = []

    async with net.async_client(timeout=120.0) as http:
        async def one(chunk: list[str]) -> None:
            user = build_user_prompt(chunk, per_parent, min_len, max_len, best, worst, rejected)
            async with sem:
                parsed = await _ask(http, url, headers, model, system, user, temperature)
            for item in parsed or []:
                parent = str(item.get("word", "")).strip().lower()
                names = item.get("names")
                if parent not in chunk:
                    parent = chunk[0]
                if isinstance(names, str):
                    names = re.split(r"[,\s]+", names)
                for n in names or []:
                    w = _clean(n)
                    if w:
                        out.append((w, parent))

        await asyncio.gather(*(one(c) for c in chunks))
    return out


def llm_batch(
    ranked_words: Sequence[Any],
    count: int,
    half_life: float = 25.0,
    min_len: int = 5,
    max_len: int = 10,
    db_path=None,
    api_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    concurrency: int = 3,
) -> list[tuple[str, str]]:
    """Ask the LLM for `count` new, gate-approved names and register them. Returns (name, parent) pairs."""
    if count <= 0 or not ranked_words or budget.exhausted():
        return []
    url = api_url or config.OPENROUTER_URL
    key = api_key or config.secret(config.OPENROUTER_KEY_ENV)
    model = model or config.COIN_MODEL
    per_parent = config.COIN_LLM_PER_NAME_COUNT
    system = config.load_prompt("coin_prompt.md", DEFAULT_COIN_PROMPT)

    weights = coin.parent_weights(ranked_words, half_life)
    words = [r["word"] if hasattr(r, "keys") else str(r) for r in ranked_words]
    # ~40% of raw proposals survive the gate and dedupe; over-ask a little
    n_parents = max(2, math.ceil(count * 1.3 / (per_parent * 0.4)))
    picked: list[str] = []
    for w in random.choices(words, weights=weights, k=n_parents * 4):
        if w not in picked:
            picked.append(w)
        if len(picked) >= n_parents:
            break
    size = max(1, config.COIN_LLM_PARENTS_PER_REQUEST)
    chunks = [picked[i : i + size] for i in range(0, len(picked), size)]

    with db.session(db_path) as conn:
        known = {r[0] for r in conn.execute("SELECT word FROM words")}
        best, worst = feedback_examples(conn, config.QUESTION_VERSION, config.COIN_LLM_FEEDBACK)

    raw = asyncio.run(_propose(
        chunks, url, key, model, system, per_parent, min_len, max_len,
        best, worst, list(_RECENT_REJECTS), config.COIN_LLM_TEMPERATURE, concurrency,
    ))

    accepted: dict[str, str] = {}
    for w, parent in raw:
        if w in known or w in accepted or w == parent:
            continue
        if not coin.acceptable(w, min_len=min_len, max_len=max_len):
            if w not in _RECENT_REJECTS:
                _RECENT_REJECTS.append(w)
            continue
        accepted[w] = parent

    pairs = list(accepted.items())
    random.shuffle(pairs)
    pairs = pairs[:count]
    if not pairs:
        return []

    ts = coin._now()
    with db.session(db_path) as conn:
        for w, parent in pairs:
            conn.execute(
                "INSERT OR IGNORE INTO words (word, pos, zipf, source, created_at, engine, parents) "
                "VALUES (?, 'n', 3.0, 'coined', ?, 'llm', ?)",
                (w, ts, parent),
            )
        conn.commit()
    return pairs
