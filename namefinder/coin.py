"""Custom word generation module (algorithmic neologisms).

Generates novel brand name variations programmatically from top-scoring base words,
prunes for English phonotactics and pronounceability, screens RDAP domain
availability, and scores them using the calibrated brand rubric.
"""

from __future__ import annotations

import asyncio
import random
import re
import sys
from datetime import datetime, timezone
from typing import Iterable, Sequence

from tqdm import tqdm

from . import budget, config, console, db, domains, score

PREFIXES = (
    "in", "re", "con", "syn", "per", "pro", "meta", "omni",
    "sub", "pan", "inter", "ultra", "neo", "epi", "co",
)

SUFFIXES = (
    "on", "is", "ix", "or", "ar", "ex", "um", "el", "en",
    "os", "ic", "us", "an", "ax", "ox", "io", "ia",
)

DEFAULT_CONCEPT_ROOTS = (
    "inspect", "vector", "tracer", "matrix", "prism",
    "logic", "nexus", "vertex", "axiom", "focus", "graph",
    "signal", "cipher", "tensor", "beacon", "optic",
    "syntax", "lattice", "core", "flow", "fathom", "loom",
)
CONCEPT_ROOTS = config.load_concept_roots(DEFAULT_CONCEPT_ROOTS)

VOWELS = set("aeiouy")
CONSONANTS = set("bcdfghjklmnpqrstvwxz")

# Awkward consonant pairs in English onset/coda
AWKWARD_PAIRS = frozenset([
    "bk", "bq", "bx", "bz", "cj", "cp", "cx", "dx", "fq", "fx",
    "gq", "gx", "hx", "jb", "jc", "jd", "jf", "jg", "jh", "jk",
    "jl", "jm", "jn", "jp", "jq", "jr", "js", "jt", "jv", "jw",
    "jx", "jy", "jz", "kq", "kx", "lq", "lx", "mq", "mx", "pq",
    "px", "qb", "qc", "qd", "qf", "qg", "qh", "qj", "qk", "ql",
    "qm", "qn", "qp", "qr", "qs", "qt", "qv", "qw", "qx", "qy",
    "qz", "rx", "tq", "tx", "vb", "vc", "vd", "vf", "vg", "vh",
    "vj", "vk", "vm", "vp", "vq", "vt", "vx", "vz", "wq", "wx",
    "xd", "xf", "xg", "xj", "xk", "xm", "xp", "xq", "xr", "xs",
    "xv", "xw", "xz", "yq", "yx", "zq", "zx",
])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_pronounceable(word: str, min_len: int = 5, max_len: int = 10) -> bool:
    """Filter for English phonotactics and clean readability."""
    w = word.lower().strip()
    if not (min_len <= len(w) <= max_len):
        return False
    if not re.match(r"^[a-z]+$", w):
        return False

    # Must contain at least one vowel
    vowels_count = sum(1 for ch in w if ch in VOWELS)
    if vowels_count == 0:
        return False

    # Balanced syllable ratio: 20% to 65% vowels
    v_ratio = vowels_count / len(w)
    if v_ratio < 0.20 or v_ratio > 0.65:
        return False

    # No 4 consecutive consonants
    if re.search(r"[bcdfghjklmnpqrstvwxz]{4,}", w):
        return False

    # No 3 consecutive vowels (except rare diphthongs)
    if re.search(r"[aeiouy]{3,}", w):
        return False

    # No 3 identical letters in a row
    if re.search(r"(.)\1\1", w):
        return False

    # No double awkward letters (e.g. qq, jj, vv, xx, yy)
    if re.search(r"(q|j|v|x|y)\1", w):
        return False

    # Q must be followed by U
    if "q" in w and "qu" not in w:
        return False

    # Avoid awkward consonant junctions
    for i in range(len(w) - 1):
        if w[i : i + 2] in AWKWARD_PAIRS:
            return False

    return True


def generate_variations(
    base: str,
    min_len: int = 5,
    max_len: int = 10,
) -> set[str]:
    """Generate rule-based morphological variations of a single base word."""
    w = base.lower().strip()
    out: set[str] = set()

    # 1. Prefix variations (one modification at a time)
    for p in PREFIXES:
        # direct prefix
        cand = p + w
        out.add(cand)
        # Junction smoothing if duplicate vowel/consonant
        if p[-1] == w[0]:
            out.add(p[:-1] + w)
        # S-cluster blending (e.g. in + spectral -> inspectral)
        if w.startswith("s") and len(w) > 4 and p == "in":
            out.add("in" + w)

    # 2. Suffix variations (replace existing grammatical ending or append)
    for s in SUFFIXES:
        out.add(w + s)
        # Replace trailing vowel
        if w[-1] in "aeiouy":
            out.add(w[:-1] + s)
        # Replace common 2-letter endings (-al, -us, -ic, -is, -on, -um, -er, -en)
        if len(w) > 4 and w[-2:] in ("al", "us", "ic", "is", "on", "um", "er", "en", "ar"):
            out.add(w[:-2] + s)
        # Replace common 3-letter endings (-ing, -ion, -oid, -ian)
        if len(w) > 5 and w[-3:] in ("ing", "ion", "oid", "ian", "ite"):
            out.add(w[:-3] + s)

    # 3. Overlapping Portmanteaus with core concept roots
    for c in CONCEPT_ROOTS:
        # Check overlaps of 2, 3, or 4 letters
        for ol in range(4, 1, -1):
            if c.endswith(w[:ol]):
                out.add(c + w[ol:])
            if w.endswith(c[:ol]):
                out.add(w + c[ol:])
            # Substring intersection (e.g. inspect + spectral -> inspectral)
            if ol >= 3:
                for idx in range(len(c) - ol + 1):
                    sub = c[idx : idx + ol]
                    if sub in w:
                        w_idx = w.find(sub)
                        blend1 = c[:idx] + w[w_idx:]
                        blend2 = w[:w_idx] + c[idx:]
                        out.add(blend1)
                        out.add(blend2)

    # 4. Single Vowel Shifts (Ablaut)
    vowels_list = "aeiou"
    for i, ch in enumerate(w):
        if ch in vowels_list:
            for v in vowels_list:
                if v != ch:
                    out.add(w[:i] + v + w[i + 1 :])

    # 5. Syllabic Truncation (Apocope) + Coda
    if len(w) >= 6:
        stem = w[:4]
        for s in ("x", "on", "is", "ix", "or", "ar", "el"):
            out.add(stem + s)

    # Filter and prune
    valid: set[str] = set()
    for cand in out:
        if cand != w and is_pronounceable(cand, min_len=min_len, max_len=max_len):
            valid.add(cand)

    return valid


def coin_candidates(
    bases: Sequence[str],
    min_len: int = 5,
    max_len: int = 10,
    db_path=None,
) -> list[str]:
    """Generate, filter, and register novel coined words into SQLite."""
    all_generated: set[str] = set()
    for b in bases:
        all_generated.update(generate_variations(b, min_len=min_len, max_len=max_len))

    with db.session(db_path) as conn:
        existing = {
            r["word"]
            for r in conn.execute("SELECT word FROM words WHERE word IN ({})".format(
                ",".join("?" * len(all_generated))
            ), list(all_generated)).fetchall()
        } if all_generated else set()

        novel = sorted(list(all_generated - existing))

        # Insert novel coined words into words table
        ts = _now()
        for w in novel:
            conn.execute(
                "INSERT OR IGNORE INTO words (word, pos, zipf, source, created_at) VALUES (?, 'n', 3.0, 'coined', ?)",
                (w, ts),
            )
        conn.commit()

    return novel


def sample_base_words(ranked_words: Sequence[Any], count: int = 15, half_life: float = 25.0) -> list[str]:
    """Sample base words from the full scored ranking using exponential falloff."""
    if not ranked_words:
        return []
    words = [r["word"] if isinstance(r, (dict, db.sqlite3.Row if hasattr(db, "sqlite3") else object)) else str(r) for r in ranked_words]
    weights = [2.0 ** (-i / half_life) for i in range(len(words))]
    sampled: list[str] = []
    seen = set()
    picks = random.choices(words, weights=weights, k=max(count * 4, 60))
    for p in picks:
        if p not in seen:
            seen.add(p)
            sampled.append(p)
            if len(sampled) >= count:
                break
    return sampled


def sample_one_variation(
    ranked_words: Sequence[Any],
    weights: Sequence[float],
    min_len: int = 5,
    max_len: int = 10,
    max_attempts: int = 30,
) -> tuple[str, str] | None:
    """Sample one base word and generate a single valid, pronounceable variation."""
    words = [r["word"] if hasattr(r, "__getitem__") else str(r) for r in ranked_words]
    for _ in range(max_attempts):
        base = random.choices(words, weights=weights, k=1)[0]
        engine = random.choice(["prefix", "suffix", "trunc", "portmanteau", "vowel"])
        cand = None

        if engine == "prefix":
            p = random.choice(PREFIXES)
            cand = p + base
            if p.endswith("o") and base.startswith("o"):
                cand = p[:-1] + base

        elif engine == "suffix":
            s = random.choice(SUFFIXES)
            if base[-1] in "aeiouy":
                cand = base[:-1] + s
            elif len(base) > 4 and base[-2:] in ("al", "us", "ic", "is", "on", "um", "er", "en", "ar"):
                cand = base[:-2] + s
            elif len(base) > 5 and base[-3:] in ("ing", "ion", "oid", "ian", "ite"):
                cand = base[:-3] + s
            else:
                cand = base + s

        elif engine == "trunc" and len(base) >= 5:
            cand = base[:4] + random.choice(("x", "on", "is", "ix", "or", "ar", "el", "ic", "al"))

        elif engine == "portmanteau":
            other = random.choice(CONCEPT_ROOTS) if random.random() < 0.5 else random.choices(words, weights=weights, k=1)[0]
            if other != base:
                for ol in range(4, 1, -1):
                    if other.endswith(base[:ol]):
                        cand = other + base[ol:]
                        break
                    elif base.endswith(other[:ol]):
                        cand = base + other[ol:]
                        break
                if not cand and len(base) >= 4 and len(other) >= 4:
                    cand = base[:3] + other[len(other) // 2 :]

        elif engine == "vowel":
            vowels = [i for i, ch in enumerate(base) if ch in "aeiou"]
            if vowels:
                idx = random.choice(vowels)
                cand = base[:idx] + random.choice("aeiou") + base[idx + 1 :]

        if cand and cand != base and is_pronounceable(cand, min_len=min_len, max_len=max_len):
            return cand, base

    return None


def coin_stream_batch(
    ranked_words: Sequence[Any],
    batch_size: int = 15,
    half_life: float = 25.0,
    min_len: int = 5,
    max_len: int = 10,
    db_path=None,
) -> list[tuple[str, str]]:
    """Sample batch_size novel variations, where each variation is its own independent base sample."""
    weights = [2.0 ** (-i / half_life) for i in range(len(ranked_words))]
    pairs: list[tuple[str, str]] = []
    seen = set()

    attempts = 0
    while len(pairs) < batch_size and attempts < batch_size * 25:
        attempts += 1
        res = sample_one_variation(ranked_words, weights, min_len=min_len, max_len=max_len)
        if res:
            cand, base = res
            if cand not in seen:
                seen.add(cand)
                pairs.append((cand, base))

    if not pairs:
        return []

    cand_words = [p[0] for p in pairs]
    with db.session(db_path) as conn:
        existing = {
            r["word"]
            for r in conn.execute(
                "SELECT word FROM words WHERE word IN ({})".format(",".join("?" * len(cand_words))),
                cand_words,
            ).fetchall()
        }

        novel_pairs = [p for p in pairs if p[0] not in existing]

        ts = _now()
        for cand, base in novel_pairs:
            conn.execute(
                "INSERT OR IGNORE INTO words (word, pos, zipf, source, created_at) VALUES (?, 'n', 3.0, 'coined', ?)",
                (cand, ts),
            )
        conn.commit()

    return novel_pairs


def run_continuous_loop(
    half_life: float = 25.0,
    min_len: int = 5,
    max_len: int = 10,
    backend: str | None = None,
    db_path=None,
    model_override: str | None = None,
) -> None:
    """Continuously sample base words, generate variations, check domains, and score winners."""
    qversion = config.QUESTION_VERSION

    # Determine backend & model
    if not backend:
        has_openrouter = bool(config.secret(config.OPENROUTER_KEY_ENV))
        backend = "openrouter" if has_openrouter else "local"

    api_url = None
    api_key = None
    if backend in ("local", "lmstudio"):
        api_url = "http://localhost:1234/v1"
        api_key = "lm-studio"
        model = model_override or "qwen3-4b-instruct-2507"
        batch_size = 8
    else:
        model = model_override or config.SCORE_MODEL
        batch_size = 20

    print("\n" + "=" * 70)
    print("=== NameFinder Continuous Coining & Discovery Engine ===")
    print("=" * 70)
    print(f"Backend: {backend} ({model})")
    print(f"Sampling: Full scored vocabulary with rank-based exponential falloff (half-life: {half_life})")
    print(f"Length filter: {min_len} to {max_len} characters")
    print("Press Ctrl+C anytime to stop.\n")

    cycle = 0
    total_coined = 0
    total_free = 0

    try:
        while True:
            if budget.exhausted():
                print(budget.stop_notice())
                print(budget.summary())
                print(f"[coin] Summary: {total_coined} coined candidates evaluated, {total_free} free domain winners added to rankings.")
                break
            cycle += 1
            # 1. Fetch current rankings (dictionary roots only, never re-coin already coined words)
            with db.session(db_path) as conn:
                ranked = db.ranked_words(conn, qversion, limit=None, dict_only=True)

            if not ranked:
                print("[coin] no scored words in database yet. Run `namefinder score` first.")
                break

            # 2. Draw novel variations where every single variation is an independent sample with replacement
            novel_pairs = coin_stream_batch(
                ranked, batch_size=15, half_life=half_life, min_len=min_len, max_len=max_len, db_path=db_path
            )
            if not novel_pairs:
                print(f"[Cycle {cycle}] Retrying candidate generation...")
                continue

            novel = [p[0] for p in novel_pairs]
            total_coined += len(novel)
            sample_summary = ", ".join(f"{c} (from {b})" for c, b in novel_pairs[:4])
            print(f"[Cycle {cycle}] Sampled {len(novel)} independent variations (e.g. {sample_summary}...)")

            # 4. Check domains via RDAP
            jobs = [(w, dom, role) for w in novel for dom, role in domains.domains_for(w)]
            counts = asyncio.run(domains._run_async(jobs, db_path=db_path))

            # 5. Extract survivors with free domains
            with db.session(db_path) as conn:
                passing = conn.execute(
                    """
                    SELECT word FROM domains
                    WHERE word IN ({}) AND status = 'available'
                    GROUP BY word
                    """.format(",".join("?" * len(novel))),
                    novel,
                ).fetchall()
                free_words = [r["word"] for r in passing]

            total_free += len(free_words)
            print(f"[Cycle {cycle}] Generated: {len(novel)} | RDAP free domains: {len(free_words)} (Cumulative free: {total_free})")

            # 6. Score free domain words with LLM
            if free_words:
                print(f"[Cycle {cycle}] Scoring {len(free_words)} free-domain candidates...")
                with tqdm(total=len(free_words), desc=f"Cycle {cycle} scoring", unit="word", ncols=80) as pbar:
                    asyncio.run(score.score_words_openrouter(
                        free_words, qversion, model, db_path=db_path, pbar=pbar,
                        api_url=api_url, api_key=api_key, batch_size=batch_size
                    ))

                # Render live Top 10 leaderboard for discovered free-domain brands
                with db.session(db_path) as conn:
                    console.show_live_top_10(
                        conn, qversion,
                        title=f"Continuous Discovery: Top 10 Winners with Free Domains (Cycle {cycle})",
                        free_domains_only=True,
                    )

            print("-" * 70)

    except (KeyboardInterrupt, asyncio.CancelledError):
        print(f"\n[coin] Continuous stream halted cleanly by user.")
        print(f"[coin] Summary: {total_coined} coined candidates evaluated, {total_free} free domain winners added to rankings.")
        if budget.calls():
            print(budget.summary())
        print("[coin] Run `namefinder top --free-domains` to view full results.\n")


def run(
    bases: Sequence[str] | None = None,
    top_base_count: int = 25,
    limit: int | None = None,
    min_len: int = 5,
    max_len: int = 10,
    check_domains_flag: bool = True,
    score_flag: bool = True,
    continuous: bool = False,
    half_life: float = 25.0,
    backend: str | None = None,
    db_path=None,
    model_override: str | None = None,
) -> list[str]:
    """Main CLI entrypoint for namefinder coin."""
    if continuous:
        run_continuous_loop(
            half_life=half_life,
            min_len=min_len,
            max_len=max_len,
            backend=backend,
            db_path=db_path,
            model_override=model_override,
        )
        return []

    qversion = config.QUESTION_VERSION

    # Determine base words
    novel_words: list[str] = []
    if bases:
        selected_bases = [b.strip().lower() for b in bases if b.strip()]
        print(f"\n=== Coining Custom Variations ===")
        print(f"Base words ({len(selected_bases)}): {', '.join(selected_bases[:15])}{'...' if len(selected_bases) > 15 else ''}")
        print(f"Length filter: {min_len} to {max_len} characters\n")
        novel_words = coin_candidates(selected_bases, min_len=min_len, max_len=max_len, db_path=db_path)
    else:
        with db.session(db_path) as conn:
            ranked = db.ranked_words(conn, qversion, limit=None, dict_only=True)
        if not ranked:
            raise SystemExit("[coin] no base words found. Provide --bases or run `namefinder score` first.")
        target_count = limit or (top_base_count * 2)
        print(f"\n=== Coining Custom Variations (Rank-Weighted Independent Sampling) ===")
        print(f"Drawing {target_count} independent candidate samples with replacement (half-life: {half_life})")
        print(f"Length filter: {min_len} to {max_len} characters\n")
        pairs = coin_stream_batch(ranked, batch_size=target_count, half_life=half_life, min_len=min_len, max_len=max_len, db_path=db_path)
        novel_words = [p[0] for p in pairs]

    if limit and len(novel_words) > limit:
        novel_words = novel_words[:limit]

    print(f"[coin] generated {len(novel_words)} unique pronounceable candidates registered in database.")

    # Optional RDAP domain check
    survivors_with_free_domains: list[str] = []
    if check_domains_flag and novel_words:
        print("\n--- Checking Domain Availability on Coined Words ---")
        jobs: list[tuple[str, str, str]] = []
        for w in novel_words:
            for dom, role in domains.domains_for(w):
                jobs.append((w, dom, role))

        print(f"[check-domains] words={len(novel_words)} | querying {len(jobs)} RDAP domain checks...")
        counts = asyncio.run(domains._run_async(jobs, db_path=db_path))
        print(f"[check-domains] complete: {counts['available']} free, {counts['registered']} taken")

        with db.session(db_path) as conn:
            passing = conn.execute(
                """
                SELECT word FROM domains
                WHERE word IN ({}) AND status = 'available'
                GROUP BY word
                """.format(",".join("?" * len(novel_words))),
                novel_words,
            ).fetchall()
            survivors_with_free_domains = [r["word"] for r in passing]
            print(f"[check-domains] {len(survivors_with_free_domains)} coined words have at least one free domain!")

    # Optional scoring with LLM
    words_to_score = survivors_with_free_domains if (check_domains_flag and survivors_with_free_domains) else novel_words
    if score_flag and words_to_score:
        if not backend:
            backend = "openrouter" if config.secret(config.OPENROUTER_KEY_ENV) else "local"

        api_url = "http://localhost:1234/v1" if backend in ("local", "lmstudio") else None
        api_key = "lm-studio" if backend in ("local", "lmstudio") else None
        model = model_override or ("qwen3-4b-instruct-2507" if backend in ("local", "lmstudio") else config.SCORE_MODEL)
        batch_size = 8 if backend in ("local", "lmstudio") else 20

        print(f"\n--- Brand Scoring {len(words_to_score)} Coined Words ({backend}: {model}) ---")
        with tqdm(total=len(words_to_score), desc="Scoring coined words", unit="word", ncols=80) as pbar:
            asyncio.run(score.score_words_openrouter(
                words_to_score, qversion, model, db_path=db_path, pbar=pbar,
                api_url=api_url, api_key=api_key, batch_size=batch_size
            ))

    if budget.calls():
        print(budget.summary())
        if budget.exhausted():
            print(budget.stop_notice())
    with db.session(db_path) as conn:
        console.show_live_top_10(
            conn, qversion,
            title="Top Coined Candidates with Free Domains",
            limit=15,
            free_domains_only=True,
        )

    print("Run `namefinder top --free-domains` to view full domain and trademark details.\n")
    return novel_words
