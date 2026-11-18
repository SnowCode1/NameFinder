"""Stage 2 - brand vibe scoring (OpenRouter with free models, or TypeSafe).

Evaluates candidate words on brand fit, naturalness, spellability,
memorability, cleanliness, and novelty.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx
from tqdm import tqdm

from . import config, console, db, net


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


DEFAULT_SYSTEM_PROMPT = (
    "You are an expert brand naming strategist for a high-craft modern technology product.\n"
    "Product: A fast, reliable software tool designed for clarity, deep insight, and quiet intellectual utility.\n\n"
    "THE 'WHY' NARRATIVE BRIDGE RULE:\n"
    "A great brand name must have an intuitive conceptual bridge to what the product does.\n"
    "- Structure & Foundation (Loom, Lattice, Mesh, Nexus, Core, Grid) -> Evokes interconnected reliability.\n"
    "- Optics & Focus (Prism, Lens, Beacon, Clarity, Ray) -> Evokes bringing signal out of noise.\n"
    "- Depth & Exploration (Fathom, Trace, Probe, Vector, Compass) -> Evokes thorough investigation.\n"
    "- Logic & Precision (Axiom, Tenet, Calculus, Syntax) -> Evokes mathematical elegance.\n\n"
    "CRITICAL 'WHY NOT' & PHONETIC COLLISION RULES:\n"
    "For EVERY candidate, identify brand risks:\n"
    "1. Homophone & Typo Trap: Avoid words that look like misspelled common words or sound awkward.\n"
    "2. Unflattering mishearings: Avoid words with crude or embarrassing phonetic overlaps.\n"
    "3. Clumsy neologisms: Avoid chimera words that feel unnatural to speak aloud.\n\n"
    "SCORING CALIBRATION:\n"
    "- 0.80 to 0.95 (EXCELLENT): Natural, dignified metaphor with clean pronunciation and zero cringe.\n"
    "- 0.50 to 0.75 (PLAUSIBLE): Good functional metaphor, easily spellable.\n"
    "- 0.20 to 0.45 (WEAK): Weak metaphor or slight awkwardness.\n"
    "- 0.00 to 0.20 (REJECT): Mundane everyday nouns, prepositions, or flat bodily verbs.\n\n"
    "EARLY-OUT RULE (CRITICAL FOR SPEED):\n"
    "- If a word is an obvious non-starter (flat noun, preposition, body verb), immediately EARLY-OUT:\n"
    "  set 'fit' <= 0.2, 'why': '', and 'why_not': 3-5 words explaining why.\n"
    "- ONLY for viable candidates (fit >= 0.5), write a single crisp sentence (< 10 words) for 'why' and 'why_not'."
)

DEFAULT_USER_PROMPT = """Evaluate each candidate word for this software product brand.

Candidates: {words}

For each candidate, provide exact numerical scores using the narrative bridge, collision, and early-out rubric:
- "word": the candidate word exactly as given
- "fit": float between 0.0 and 1.0 (<= 0.2 for non-starters; >= 0.8 only for strong conceptual alignment)
- "natural": float between 0.0 and 1.0
- "spellable": float between 0.0 and 1.0
- "memorable": float between 0.0 and 4.0
- "neg": float between 0.0 and 1.0
- "taken": float between 0.0 and 1.0
- "common": float between 0.0 and 1.0
- "why": short string (leave empty '' for rejected non-starters; <= 10 words for viable brands)
- "why_not": short string (3-5 words for non-starters; <= 10 words for viable brands)

Return a JSON array of objects, one per candidate, in the same order.
Return ONLY the raw JSON array. No markdown fences, no prose."""

OPENROUTER_SYSTEM_PROMPT = config.load_prompt("system_prompt.md", DEFAULT_SYSTEM_PROMPT)
OPENROUTER_USER_PROMPT = config.load_prompt("user_prompt.md", DEFAULT_USER_PROMPT)


def composite(
    fit: float,
    natural: float,
    spellable: float,
    memorable: float,
    neg: float,
    taken: float,
    common: float,
) -> float:
    """Weighted sum of judgments, gated multiplicatively by cleanliness."""
    w = config.WEIGHTS
    novelty = (1.0 - taken * config.TAKEN_DAMP) * (1.0 - common * config.COMMON_NAME_DAMP)
    base = (
        w["fit"] * fit
        + w["natural"] * natural
        + w["spellable"] * spellable
        + w["memorable"] * (memorable / 4.0)
        + w["novelty"] * novelty
    )
    return round(base * (1.0 - neg), 4)


def _parse_scores(content: str | None) -> list[dict[str, Any]] | None:
    if not content or not isinstance(content, str):
        return None
    text = content.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start = text.find("[")
    if start < 0:
        return None

    # 1. Try clean JSON array parsing first
    end = text.rfind("]")
    if end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict) and d.get("word")]
        except json.JSONDecodeError:
            pass

    # 2. Resilient truncation recovery: find last completed object '}' and close array
    last_brace = text.rfind("}")
    if last_brace > start:
        try:
            repaired = text[start : last_brace + 1] + "\n]"
            data = json.loads(repaired)
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict) and d.get("word")]
        except json.JSONDecodeError:
            pass

    return None


async def score_words_openrouter(
    words: list[str],
    qversion: str,
    model: str,
    db_path=None,
    batch_size: int = 20,
    pbar: tqdm | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
) -> int:
    url = api_url or config.OPENROUTER_URL
    key = api_key or config.secret(config.OPENROUTER_KEY_ENV)
    if not key and "localhost" not in url and "127.0.0.1" not in url:
        raise SystemExit(
            f"[score] OPENROUTER_API_KEY is not set in .env. {config.WRITE_KEY_HINT}"
        )

    headers = {"Authorization": f"Bearer {key or 'local'}", "Content-Type": "application/json"}
    done = 0
    failed = 0

    with db.session(db_path) as conn:
        async with net.async_client(timeout=120.0) as http:
            for idx in range(0, len(words), batch_size):
                chunk = words[idx : idx + batch_size]
                body = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": OPENROUTER_SYSTEM_PROMPT},
                        {"role": "user", "content": OPENROUTER_USER_PROMPT.format(words=", ".join(chunk))},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 3500,
                }

                parsed: list[dict[str, Any]] | None = None
                for attempt in range(6):
                    try:
                        r = await http.post(
                            f"{url}/chat/completions",
                            json=body,
                            headers=headers,
                        )
                    except Exception as exc:
                        wait_s = min(60.0, 3.0 * (2 ** attempt))
                        tqdm.write(f"[score] network connection glitch ({exc.__class__.__name__}): retrying in {wait_s:.1f}s...", file=sys.stderr)
                        await asyncio.sleep(wait_s)
                        continue

                    if r.status_code == 429:
                        wait_s = net.backoff_delay(attempt, r.headers.get("retry-after"))
                        tqdm.write(f"[score] rate limited (429), pausing {wait_s:.1f}s...", file=sys.stderr)
                        await asyncio.sleep(wait_s)
                        continue

                    if r.status_code >= 500:
                        wait_s = min(60.0, 3.0 * (2 ** attempt))
                        tqdm.write(f"[score] OpenRouter HTTP {r.status_code}; retrying in {wait_s:.1f}s...", file=sys.stderr)
                        await asyncio.sleep(wait_s)
                        continue

                    if r.status_code != 200:
                        tqdm.write(f"[score] OpenRouter HTTP {r.status_code}: {r.text[:180]}", file=sys.stderr)
                        break

                    try:
                        choices = r.json().get("choices") or [{}]
                        content = choices[0].get("message", {}).get("content")
                        parsed = _parse_scores(content)
                        if parsed:
                            break
                    except (KeyError, IndexError, ValueError, AttributeError):
                        pass

                    await asyncio.sleep(2.0)

                if not parsed:
                    failed += len(chunk)
                    tqdm.write(f"[score] warning: batch failed after retries; skipping {len(chunk)} words (they remain queued for next pass)", file=sys.stderr)
                    if pbar:
                        pbar.update(len(chunk))
                    continue

                by_word = {str(item.get("word", "")).strip().lower(): item for item in parsed}
                now_ts = _now()
                for w in chunk:
                    item = by_word.get(w.lower())
                    if not item:
                        continue
                    try:
                        fit = float(item.get("fit", 0.5))
                        natural = float(item.get("natural", 0.8))
                        spellable = float(item.get("spellable", 0.8))
                        memorable = float(item.get("memorable", 2.0))
                        neg = float(item.get("neg", 0.0))
                        taken = float(item.get("taken", 0.0))
                        common = float(item.get("common", 0.0))
                    except (TypeError, ValueError):
                        continue

                    why_text = str(item.get("why", "")).strip()[:450] or None
                    why_not_raw = item.get("why_not")
                    if isinstance(why_not_raw, list):
                        why_not_text = " ".join(str(x) for x in why_not_raw).strip()[:450] or None
                    else:
                        why_not_text = str(why_not_raw or "").strip()[:450] or None

                    row = {
                        "word": w,
                        "qversion": qversion,
                        "model": model,
                        "fit": round(max(0.0, min(1.0, fit)), 4),
                        "natural": round(max(0.0, min(1.0, natural)), 4),
                        "spellable": round(max(0.0, min(1.0, spellable)), 4),
                        "memorable": round(max(0.0, min(4.0, memorable)), 3),
                        "neg": round(max(0.0, min(1.0, neg)), 4),
                        "taken": round(max(0.0, min(1.0, taken)), 4),
                        "common": round(max(0.0, min(1.0, common)), 4),
                        "composite": composite(fit, natural, spellable, memorable, neg, taken, common),
                        "why": why_text,
                        "why_not": why_not_text,
                        "raw": json.dumps({"openrouter": item}, default=str)[:20000],
                        "scored_at": now_ts,
                    }
                    db.upsert_score(conn, row)
                    done += 1

                conn.commit()
                if pbar:
                    pbar.update(len(chunk))
                # Render live top 10 leaderboard directly during slow scoring runs
                console.show_live_top_10(conn, qversion, title=f"Live Top 10 Scored Candidates ({done}/{len(words)} processed)")
                await asyncio.sleep(1.0)

    return done


def run(
    limit: int | None = None,
    top_n: int | None = None,
    refresh: bool = False,
    concurrency: int | None = None,
    model: str | None = None,
    db_path=None,
    sample: int = 25,
    backend: str | None = None,
    free_domains_only: bool = False,
    clear_only: bool = False,
) -> None:
    if clear_only:
        with db.session(db_path) as conn:
            c = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
            conn.execute("DELETE FROM scores")
            conn.commit()
        print(f"[score] cleared all {c} cached scores from database (database is clean).")
        return

    qversion = config.QUESTION_VERSION

    # Auto-detect backend
    if not backend:
        has_openrouter = bool(config.secret(config.OPENROUTER_KEY_ENV))
        if has_openrouter:
            backend = "openrouter"
        else:
            backend = "local"

    api_url = None
    api_key = None
    if backend in ("local", "lmstudio"):
        api_url = "http://localhost:1234/v1"
        api_key = "lm-studio"
        model = model or "qwen3-4b-instruct-2507"
        batch_size = 12
    elif backend == "openrouter":
        model = model or config.SCORE_MODEL
        batch_size = 20
    else:
        model = model or config.SCORE_MODEL
        batch_size = 15

    with db.session(db_path) as conn:
        total = db.count_words(conn)
        if total == 0:
            raise SystemExit("[score] no words cached - run `prepare` first")
        if refresh:
            conn.execute("DELETE FROM scores")
            conn.commit()
            if free_domains_only:
                todo = [r["word"] for r in conn.execute(
                    "SELECT w.word FROM words w JOIN domains d ON d.word = w.word WHERE d.status = 'available' GROUP BY w.word ORDER BY w.zipf DESC"
                ).fetchall()]
            else:
                todo = [r["word"] for r in conn.execute("SELECT word FROM words ORDER BY zipf DESC")]
            if limit:
                todo = todo[:limit]
        else:
            if free_domains_only:
                todo = [r["word"] for r in conn.execute(
                    """
                    SELECT w.word FROM words w
                    JOIN domains d ON d.word = w.word AND d.status = 'available'
                    LEFT JOIN scores s ON s.word = w.word AND s.qversion = ?
                    WHERE s.word IS NULL
                    GROUP BY w.word
                    ORDER BY w.zipf DESC
                    """,
                    (qversion,),
                ).fetchall()]
            else:
                todo = db.words_without_scores(conn, qversion)
            if limit:
                todo = todo[:limit]
        cached = conn.execute(
            "SELECT COUNT(*) c FROM scores WHERE qversion = ?", (qversion,)
        ).fetchone()["c"]

    filter_note = " (filtering: free domains only)" if free_domains_only else ""
    print(f"\n[score] backend={backend}  model={model}{filter_note}")
    print(f"[score] cached={cached}  queued={len(todo)}\n")

    if not todo:
        print("[score] all words are already scored for this version.")
    else:
        try:
            with tqdm(total=len(todo), desc="Scoring candidates", unit="word", ncols=80) as pbar:
                if backend in ("openrouter", "local", "lmstudio"):
                    done = asyncio.run(score_words_openrouter(
                        todo, qversion, model, db_path, pbar=pbar, api_url=api_url, api_key=api_key,
                        batch_size=batch_size
                    ))
                else:
                    raise SystemExit(f"[score] unknown backend: {backend}. Use 'openrouter' or 'local'.")
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n[score] Scoring paused by user (Ctrl+C). Progress safely saved to database.")

    with db.session(db_path) as conn:
        console.show_live_top_10(conn, qversion, title=f"Top {sample} Scored Candidates", limit=sample)
