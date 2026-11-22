"""Pass 2: stabilise the rankings of the top coined candidates.

Pass 1 (`score --mode brand`, and the coin loop) already makes the model spell
out how every name is said (syllables, respelling, heard spelling, sound-alikes)
and folds that into the composite. That is one sample at one temperature, so the
top of the table is noisy. This pass re-asks the same spoken-name questions
several times for only the best words, averages those samples together with the
pass-1 answer, blends the result with the deterministic checks through
`score.spoken_review`, and writes it back onto the same score row.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from tqdm import tqdm

from . import budget, config, console, db, net, score

DEFAULT_REFINE_PROMPT = (
    "You are a phonetics-aware brand naming reviewer for a software product.\n"
    "Product context: " + config.PRODUCT_BLURB + "\n\n"
    "For every candidate, WORK THROUGH THE SPOKEN WORD IN THIS ORDER before scoring:\n"
    "1. syllables: split the word into syllables joined by hyphens (e.g. 'met-a-prism').\n"
    "2. say_aloud: a plain-English respelling with the stressed syllable in CAPITALS (e.g. 'MET-uh-prizm').\n"
    "3. heard_spelling: imagine it said once over a phone call; write how a listener would most likely spell it.\n"
    "4. misheard_as: real words or phrases it could be mistaken for, or crude/awkward sound-alikes. Empty string if none.\n"
    "Then score honestly. Most invented words are mediocre; use the full range.\n"
    "- ambiguity (0.0-1.0): chance a listener spells it wrong or a reader says it wrong. heard_spelling != word implies >= 0.4.\n"
    "- sayable (0.0-1.0): how easy and pleasant it is to say aloud; 1.0 = flows, 0.3 = tongue-twister or clumsy.\n"
    "- mishear_risk (0.0-1.0): how damaging the sound-alikes in misheard_as are (0 if none).\n"
    "- spellable (0.0-1.0): chance of spelling it correctly after hearing it once.\n"
    "- natural (0.0-1.0): reads like a real, serious word rather than a chimera.\n"
    "- neg (0.0-1.0): negative connotation, in English or common languages.\n"
    "- verbable (0.0-1.0): how naturally it works as a verb in 'I just ___ it and found what I needed' "
    "(googled, pinged, grepped). 1.0 = instantly natural; 0.0 = awkward or noun-only.\n"
    "- why_not: 3-8 words on the biggest spoken-name weakness, or empty."
)

DEFAULT_REFINE_USER = """Review each candidate name for how it is spoken, heard and spelled.

Candidates: {words}

Return a JSON array, one object per candidate in the same order, with the keys in EXACTLY this order:
"word", "syllables", "say_aloud", "heard_spelling", "misheard_as", "ambiguity", "sayable", "mishear_risk", "spellable", "natural", "neg", "verbable", "why_not"

Return ONLY the raw JSON array. No markdown fences, no prose."""

REFINE_SYSTEM_PROMPT = config.load_prompt("refine_prompt.md", DEFAULT_REFINE_PROMPT)
REFINE_USER_PROMPT = config.load_prompt("refine_user_prompt.md", DEFAULT_REFINE_USER)

NUMERIC_KEYS = ("ambiguity", "sayable", "mishear_risk", "spellable", "natural", "neg", "verbable")


def _clamp(v: Any, lo: float = 0.0, hi: float = 1.0, default: float = 0.5) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def pick_candidates(
    conn, qversion: str, top_n: int, refresh: bool, free_domains_only: bool,
    all_free: bool = False, clean_only: bool = False,
) -> list[str]:
    """Best coined words that have not been refined yet, using the same filters as `top`."""
    coined = {r["word"] for r in conn.execute("SELECT word FROM words WHERE source = 'coined'")}
    items = db.get_candidates_summary(
        conn, qversion, limit=10**9,
        only_free_domains=free_domains_only,
        only_clean_trademarks=clean_only,
        all_domains_free=all_free,
    )
    picked = [
        it["word"] for it in items
        if it["word"] in coined and (refresh or not it.get("refined"))
    ]
    return picked[:top_n]


async def _ask(http, url, headers, model, chunk: list[str], temperature: float) -> list[dict[str, Any]] | None:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": REFINE_SYSTEM_PROMPT},
            {"role": "user", "content": REFINE_USER_PROMPT.format(words=", ".join(chunk))},
        ],
        "temperature": temperature,
        "max_tokens": 4000,
        **config.openrouter_extras(url),
    }
    for attempt in range(5):
        if budget.exhausted():
            return None
        try:
            r = await http.post(f"{url}/chat/completions", json=body, headers=headers)
        except Exception as exc:
            wait_s = min(60.0, 3.0 * (2 ** attempt))
            tqdm.write(f"[refine] network glitch ({exc.__class__.__name__}); retrying in {wait_s:.0f}s", file=sys.stderr)
            await asyncio.sleep(wait_s)
            continue
        if r.status_code == 429:
            await asyncio.sleep(net.backoff_delay(attempt, r.headers.get("retry-after")))
            continue
        if r.status_code >= 500:
            await asyncio.sleep(min(60.0, 3.0 * (2 ** attempt)))
            continue
        if r.status_code != 200:
            tqdm.write(f"[refine] HTTP {r.status_code}: {r.text[:160]}", file=sys.stderr)
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


def merge_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Average numeric answers across samples; take the most common text answer."""
    merged: dict[str, Any] = {}
    for key in NUMERIC_KEYS:
        vals = [_clamp(s[key]) for s in samples if key in s]
        if vals:
            merged[key] = statistics.fmean(vals)
    for key in ("syllables", "say_aloud", "heard_spelling", "misheard_as", "why_not"):
        vals = [str(s.get(key) or "").strip() for s in samples]
        counted = Counter(v for v in vals if v)
        merged[key] = counted.most_common(1)[0][0] if counted else ""
    merged["n_samples"] = len(samples)
    return merged


def pass1_sample(old: dict[str, Any]) -> dict[str, Any] | None:
    """The spoken review stored by brand scoring, usable as one more sample (None for older rows)."""
    try:
        item = (json.loads(old.get("raw") or "{}") or {}).get("openrouter") or {}
    except (TypeError, ValueError):
        return None
    return item if isinstance(item, dict) and "sayable" in item else None


def build_row(conn, old: dict[str, Any], m: dict[str, Any], model: str = "") -> dict[str, Any]:
    """Combine pass-1 fields, the merged LLM review and the deterministic checks."""
    word = old["word"]
    sp = score.spoken_review(word, m)
    phonetic, ambiguity = sp["phonetic"], sp["ambiguity"]

    spellable = m.get("spellable", old["spellable"])
    natural = m.get("natural", old["natural"])
    neg = max(m.get("neg", old["neg"]), sp["mishear_risk"])

    verbable = m.get("verbable", old.get("verbable"))
    comp = score.composite_brand(
        old["fit"], natural, spellable, old["memorable"], neg, old["taken"], old["common"], verbable
    )
    comp = score.adjust_brand_composite(conn, word, comp, phonetic)

    why_not = m.get("why_not") or old.get("why_not")
    raw = {}
    try:
        raw = json.loads(old.get("raw") or "{}")
    except (TypeError, ValueError):
        pass
    raw["refine"] = m

    return {
        **old,
        "model": f"{old.get('model') or '?'} | refine:{model}" if model else old.get("model"),
        "natural": round(natural, 4),
        "spellable": round(spellable, 4),
        "neg": round(neg, 4),
        "composite": comp,
        "verbable": None if verbable is None else round(verbable, 4),
        "why_not": (why_not or None) and why_not[:450],
        "raw": json.dumps(raw, default=str)[:20000],
        "syllables": sp["syllables"],
        "say_aloud": sp["say_aloud"],
        "misheard_as": sp["misheard_as"],
        "ambiguity": ambiguity,
        "phonetic": phonetic,
        "refined": 1,
        "refined_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


async def refine_words(
    words: list[str],
    qversion: str,
    model: str,
    db_path=None,
    api_url: str | None = None,
    api_key: str | None = None,
    batch_size: int = 8,
    samples: int = 3,
    concurrency: int = 1,
    pbar: Any = None,
) -> int:
    url = api_url or config.OPENROUTER_URL
    key = api_key or config.secret(config.OPENROUTER_KEY_ENV)
    if not key and "localhost" not in url and "127.0.0.1" not in url:
        raise SystemExit(f"[refine] OPENROUTER_API_KEY is not set in .env. {config.WRITE_KEY_HINT}")
    headers = {"Authorization": f"Bearer {key or 'local'}", "Content-Type": "application/json"}

    sem = asyncio.Semaphore(max(1, concurrency))
    results: dict[str, list[dict[str, Any]]] = {w: [] for w in words}
    chunks = [words[i : i + batch_size] for i in range(0, len(words), batch_size)]

    async with net.async_client(timeout=180.0) as http:

        async def one(chunk: list[str], temperature: float) -> None:
            async with sem:
                parsed = await _ask(http, url, headers, model, chunk, temperature)
            if parsed:
                by_word = {str(p.get("word", "")).strip().lower(): p for p in parsed}
                for w in chunk:
                    if w in by_word:
                        results[w].append(by_word[w])
            if pbar:
                pbar.update(len(chunk))
                pbar.set_postfix_str(budget.metrics_text(), refresh=False)

        jobs = []
        for s in range(samples):
            temperature = 0.2 if samples == 1 else 0.2 + 0.2 * s   # vary samples slightly
            for c in chunks:
                jobs.append(one(c, temperature))
        await asyncio.gather(*jobs)

    done = 0
    with db.session(db_path) as conn:
        for w, samp in results.items():
            if not samp:
                continue
            row = conn.execute(
                "SELECT * FROM scores WHERE word = ? AND qversion = ?", (w, qversion)
            ).fetchone()
            if not row:
                continue
            old = dict(row)
            first = pass1_sample(old)
            db.upsert_score(conn, build_row(conn, old, merge_samples(samp + ([first] if first else [])), model))
            done += 1
    return done


def run(
    top_n: int | None = None,
    samples: int | None = None,
    model: str | None = None,
    backend: str | None = None,
    refresh: bool = False,
    free_domains_only: bool = False,
    all_free: bool = False,
    clean_only: bool = False,
    db_path=None,
    sample: int = 15,
    concurrency: int | None = None,
) -> None:
    qversion = config.QUESTION_VERSION
    top_n = top_n or config.REFINE_TOP_N
    samples = samples or config.REFINE_SAMPLES

    if not backend:
        backend = "openrouter" if config.secret(config.OPENROUTER_KEY_ENV) else "local"
    if backend in ("local", "lmstudio"):
        api_url, api_key = "http://localhost:1234/v1", "lm-studio"
        model = model or "qwen3-4b-instruct-2507"
        batch_size, concurrency = 6, 1
    else:
        api_url = api_key = None
        model = model or config.REFINE_MODEL
        batch_size = 8
        concurrency = concurrency or config.SCORE_CONCURRENCY

    with db.session(db_path) as conn:
        todo = pick_candidates(conn, qversion, top_n, refresh, free_domains_only, all_free, clean_only)

    if not todo:
        print("[refine] no unrefined coined words match. Run `namefinder coin` first, relax the filters, or use --refresh.")
        return

    print(f"\n[refine] backend={backend}  model={model}  words={len(todo)}  samples={samples}")
    try:
        with tqdm(total=len(todo) * samples, desc="Refining", unit="word", ncols=80) as pbar:
            done = asyncio.run(refine_words(
                todo, qversion, model, db_path, api_url=api_url, api_key=api_key,
                batch_size=batch_size, samples=samples, concurrency=concurrency, pbar=pbar,
            ))
        print(f"[refine] refined {done}/{len(todo)} words")
        print(budget.summary())
        if budget.exhausted():
            print(budget.stop_notice())
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[refine] interrupted; nothing was written for the unfinished run.")
        return

    with db.session(db_path) as conn:
        console.show_live_top_10(
            conn, qversion, title=f"Top {sample} After Spoken-Name Review", limit=sample,
            free_domains_only=free_domains_only or all_free,
        )
