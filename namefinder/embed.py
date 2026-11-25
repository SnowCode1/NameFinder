"""Meaning embeddings: base words, ideas and names as vectors, cached in SQLite.

Vectors come from any OpenAI-compatible /embeddings endpoint (OpenRouter by default, `config.EMBED_*`),
unit-normalised and stored as float32 keyed by the exact text embedded, so nothing is paid for twice.

What they are used for:
- Idea seeds (`idea_seeds`): for every approved idea, the base words (dictionary and lexicon layers,
  embedded with their gloss) closest in meaning to it, minus EMBED_CATEGORY_WEIGHT x their closeness to
  the category anchors (`category_anchors.txt`: phrases that name what the product literally is). `coin` gives part of the LLM engine's parents
  from these seeds, each shown with its idea, so the cheap high-volume loop explores the product's features
  instead of its category word. Seeded names carry operator 'seed:<idea id>' (`engines --by operator`).
- Obliqueness (`obliqueness`): a name's closeness to the ideas minus its closeness to the category anchors.
  Invented words embed through their sub-word pieces, so this is noisy: `namefinder embed --lift` checks
  whether it tracks the tournament judge before anything is weighted on it.
- `namefinder embed --near TEXT`: the base words and names closest to a phrase.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Sequence

import numpy as np

from . import budget, config, console, db, families, net

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def model_id() -> str:
    return config.EMBED_MODEL


# --- cache -------------------------------------------------------------------
def cached(conn, texts: Sequence[str], model: str | None = None) -> dict[str, np.ndarray]:
    model = model or model_id()
    out: dict[str, np.ndarray] = {}
    uniq = list(dict.fromkeys(texts))
    for i in range(0, len(uniq), 500):
        part = uniq[i : i + 500]
        marks = ",".join("?" * len(part))
        for r in conn.execute(f"SELECT text, vec FROM embeddings WHERE model = ? AND text IN ({marks})",
                              [model, *part]):
            out[r["text"]] = np.frombuffer(r["vec"], dtype=np.float32)
    return out


async def _fetch(texts: list[str], model: str, bar=None) -> dict[str, np.ndarray]:
    key = config.secret(config.EMBED_KEY_ENV)
    if not key:
        raise SystemExit(f"{config.EMBED_KEY_ENV} is not set in .env. {config.WRITE_KEY_HINT}")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    url = f"{config.EMBED_URL}/embeddings"
    sem = asyncio.Semaphore(config.EMBED_CONCURRENCY)
    out: dict[str, np.ndarray] = {}
    chunks = [texts[i : i + config.EMBED_BATCH] for i in range(0, len(texts), config.EMBED_BATCH)]

    async with net.async_client(timeout=120.0) as http:
        async def one(chunk: list[str]) -> None:
            for attempt in range(5):
                if budget.exhausted():
                    return
                try:
                    async with sem:
                        r = await http.post(url, json={"model": model, "input": chunk}, headers=headers)
                except Exception as exc:
                    console.log(f"[embed] network glitch ({exc.__class__.__name__}); retrying")
                    await asyncio.sleep(min(60.0, 3.0 * 2 ** attempt))
                    continue
                if r.status_code == 429 or r.status_code >= 500:
                    await asyncio.sleep(net.llm_backoff_delay(attempt, r.headers.get("retry-after")))
                    continue
                if r.status_code != 200:
                    console.log(f"[embed] HTTP {r.status_code}: {r.text[:200]}")
                    return
                payload = r.json()
                budget.record(payload)
                data = sorted(payload.get("data") or [], key=lambda d: d.get("index", 0))
                if len(data) != len(chunk):
                    console.log(f"[embed] expected {len(chunk)} vectors, got {len(data)}; retrying")
                    continue
                for text, d in zip(chunk, data):
                    v = np.asarray(d["embedding"], dtype=np.float32)
                    out[text] = v / (np.linalg.norm(v) or 1.0)
                if bar:
                    bar.update(len(chunk))
                return

        await asyncio.gather(*(one(c) for c in chunks))
    return out


def ensure(conn, texts: Sequence[str], model: str | None = None, label: str = "Embedding",
           quiet: bool = False) -> dict[str, np.ndarray]:
    """Vectors for every text, embedding (and storing) the ones not cached yet."""
    model = model or model_id()
    have = cached(conn, texts, model)
    missing = [t for t in dict.fromkeys(texts) if t not in have]
    if missing:
        if quiet:
            new = asyncio.run(_fetch(missing, model))
        else:
            with console.RichBar(f"{label} ({len(missing)} texts, {model.split('/')[-1]})",
                                 total=len(missing), unit="text", style="cyan") as bar:
                new = asyncio.run(_fetch(missing, model, bar))
        ts = _now()
        conn.executemany(
            "INSERT OR REPLACE INTO embeddings (text, model, vec, created_at) VALUES (?, ?, ?, ?)",
            [(t, model, v.astype(np.float32).tobytes(), ts) for t, v in new.items()],
        )
        conn.commit()
        have.update(new)
    return have


def matrix(vecs: dict[str, np.ndarray], texts: Sequence[str]) -> tuple[list[str], np.ndarray]:
    """(texts that have a vector, stacked matrix in that order)."""
    keep = [t for t in texts if t in vecs]
    if not keep:
        return [], np.zeros((0, 1), dtype=np.float32)
    return keep, np.stack([vecs[t] for t in keep])


# --- what gets embedded --------------------------------------------------------
def base_text(word: str, gloss: str | None) -> str:
    return f"{word}: {gloss}" if gloss else word


def base_rows(conn) -> list[tuple[str, str]]:
    """(base word, text to embed) for dictionary and lexicon words that break no ban."""
    rows = conn.execute(
        "SELECT word, gloss FROM words WHERE source IN ('wordfreq', 'lexicon') AND length(word) BETWEEN 4 AND 11"
    ).fetchall()
    return [(r["word"], base_text(r["word"], r["gloss"])) for r in rows if not families.banned(r["word"])]


def idea_rows(conn) -> list[tuple[int, str]]:
    return [(r["id"], r["text"]) for r in db.list_ideas(conn, "approved")]


def anchors() -> list[str]:
    return list(config.EMBED_CATEGORY_ANCHORS)


# --- idea seeds -----------------------------------------------------------------
def _category_penalty(m: np.ndarray, anchor_m: np.ndarray) -> np.ndarray:
    if not len(anchor_m):
        return np.zeros(len(m), dtype=np.float32)
    return (m @ anchor_m.T).max(axis=1)


def seeds_by_idea(conn, k: int | None = None, build: bool = True, quiet: bool = False,
                  ) -> dict[int, list[tuple[str, float]]]:
    """{idea id: [(base word, score)]}, best first: similarity to the idea minus EMBED_CATEGORY_WEIGHT x
    similarity to the category anchors."""
    k = k or config.EMBED_SEEDS_PER_IDEA
    ideas = idea_rows(conn)
    bases = base_rows(conn)
    if not ideas or not bases:
        return {}
    texts = [t for _, t in bases] + [t for _, t in ideas] + anchors()
    vecs = ensure(conn, texts, label="Embedding bases and ideas", quiet=quiet) if build else cached(conn, texts)
    base_texts, bm = matrix(vecs, [t for _, t in bases])
    if not base_texts:
        return {}
    word_of = {t: w for w, t in bases}
    _, am = matrix(vecs, anchors())
    score_base = -config.EMBED_CATEGORY_WEIGHT * _category_penalty(bm, am)
    out: dict[int, list[tuple[str, float]]] = {}
    for iid, text in ideas:
        if text not in vecs:
            continue
        s = bm @ vecs[text] + score_base
        top = np.argsort(-s)[:k]
        out[iid] = [(word_of[base_texts[i]], float(s[i])) for i in top]
    return out


def idea_seeds(conn, k: int | None = None, build: bool = True, quiet: bool = True) -> dict[str, tuple[int, str]]:
    """{base word: (idea id, idea text)} for the LLM coining engine; a base retrieved for several ideas keeps
    the idea it scored highest for."""
    by_idea = seeds_by_idea(conn, k, build=build, quiet=quiet)
    text_of = dict(idea_rows(conn))
    best: dict[str, tuple[float, int]] = {}
    for iid, items in by_idea.items():
        for w, s in items:
            if w not in best or s > best[w][0]:
                best[w] = (s, iid)
    return {w: (iid, text_of[iid]) for w, (_, iid) in best.items()}


# --- obliqueness ----------------------------------------------------------------
def obliqueness(conn, names: Sequence[str], build: bool = True) -> dict[str, dict[str, float]]:
    """{name: {"idea": best similarity to an approved idea, "category": best similarity to a category
    anchor, "oblique": idea - EMBED_CATEGORY_WEIGHT x category}} for the names given."""
    ideas = [t for _, t in idea_rows(conn)]
    texts = list(names) + ideas + anchors()
    vecs = ensure(conn, texts, label="Embedding names") if build else cached(conn, texts)
    _, im = matrix(vecs, ideas)
    _, am = matrix(vecs, anchors())
    kept, nm = matrix(vecs, list(names))
    if not kept:
        return {}
    idea_sim = (nm @ im.T).max(axis=1) if len(im) else np.zeros(len(kept))
    cat_sim = (nm @ am.T).max(axis=1) if len(am) else np.zeros(len(kept))
    w = config.EMBED_CATEGORY_WEIGHT
    return {n: {"idea": float(i), "category": float(c), "oblique": float(i - w * c)}
            for n, i, c in zip(kept, idea_sim, cat_sim)}


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    if len(x) < 3:
        return float("nan")
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    return float(np.corrcoef(rx, ry)[0, 1])


# --- commands --------------------------------------------------------------------
def build(names_top: int = 300, db_path=None) -> None:
    """Embed every base word, approved idea and category anchor, plus the best `names_top` rated names."""
    from . import tournament
    with db.session(db_path) as conn:
        bases = base_rows(conn)
        ideas = idea_rows(conn)
        judge, _ = tournament.resolve_judge(conn)
        names = [r["word"] for r in conn.execute(
            "SELECT word FROM tournament_ratings WHERE judge = ? ORDER BY rating DESC LIMIT ?",
            (judge or "", names_top))]
        texts = [t for _, t in bases] + [t for _, t in ideas] + anchors() + names
        before = len(cached(conn, texts))
        ensure(conn, texts)
        total = conn.execute("SELECT COUNT(*) FROM embeddings WHERE model = ?", (model_id(),)).fetchone()[0]
    console.log(f"[embed] {len(bases)} base words, {len(ideas)} approved ideas, {len(anchors())} category anchors, "
                f"{len(names)} rated names; {len(set(texts)) - before} newly embedded, {total} cached for {model_id()}")
    console.log(budget.summary())


def show_seeds(k: int | None = None, db_path=None, limit_ideas: int = 0) -> None:
    from rich import box
    from rich.table import Table
    with db.session(db_path) as conn:
        by_idea = seeds_by_idea(conn, k)
        text_of = dict(idea_rows(conn))
    if not by_idea:
        console.log("[embed] no approved ideas or base words to seed from.")
        return
    t = Table(box=box.SIMPLE_HEAD, title="Base words retrieved per idea (closest in meaning, away from the category)")
    t.add_column("id", justify="right", style="dim")
    t.add_column("idea", max_width=60)
    t.add_column("bases", style="cyan")
    items = sorted(by_idea.items())
    for iid, seeds in (items[:limit_ideas] if limit_ideas else items):
        t.add_row(str(iid), text_of.get(iid, ""), ", ".join(w for w, _ in seeds))
    console._console.print(t)
    console.log(budget.summary())


def near(query: str, k: int = 25, db_path=None) -> None:
    """Base words and rated names closest in meaning to a phrase."""
    from . import tournament
    with db.session(db_path) as conn:
        q = ensure(conn, [query], quiet=True)[query]
        bases = base_rows(conn)
        vecs = cached(conn, [t for _, t in bases])
        texts, m = matrix(vecs, [t for _, t in bases])
        judge, _ = tournament.resolve_judge(conn)
        names = [r["word"] for r in conn.execute(
            "SELECT word FROM tournament_ratings WHERE judge = ? ORDER BY rating DESC", (judge or "",))]
        nvecs = cached(conn, names)
        ntexts, nm = matrix(nvecs, names)
    word_of = {t: w for w, t in bases}
    if texts:
        s = m @ q
        top = np.argsort(-s)[:k]
        console.log(f"[embed] bases nearest '{query}': " + ", ".join(f"{word_of[texts[i]]} {s[i]:.2f}" for i in top))
    else:
        console.log("[embed] no base vectors cached yet; run `namefinder embed` first.")
    if ntexts:
        s = nm @ q
        top = np.argsort(-s)[: min(k, len(ntexts))]
        console.log(f"[embed] rated names nearest '{query}': " + ", ".join(f"{ntexts[i]} {s[i]:.2f}" for i in top))


def lift(top: int = 300, db_path=None) -> None:
    """Does obliqueness (or idea / category closeness) track the tournament judge? Spearman correlations
    over the best `top` rated names, and the mean of each measure by rating band."""
    from . import tournament
    with db.session(db_path) as conn:
        judge, note = tournament.resolve_judge(conn)
        if not judge:
            console.log(note or "[embed] no tournament ratings yet.")
            return
        refs = set(tournament.load_refs())
        rows = conn.execute(
            "SELECT word, rating FROM tournament_ratings WHERE judge = ? AND games >= 4 ORDER BY rating DESC LIMIT ?",
            (judge, top + len(refs))).fetchall()
        rows = [r for r in rows if r["word"] not in refs][:top]
        names = [r["word"] for r in rows]
        ob = obliqueness(conn, names)
    rated = [(r["word"], r["rating"]) for r in rows if r["word"] in ob]
    if len(rated) < 10:
        console.log("[embed] too few rated names with vectors.")
        return
    ratings = [r for _, r in rated]
    console.log(f"[embed] judge {judge}: {len(rated)} names (references left out)")
    for key in ("idea", "category", "oblique"):
        vals = [ob[w][key] for w, _ in rated]
        rho = spearman(vals, ratings)
        console.log(f"  {key:9s} rank correlation with rating: {rho:+.3f}")
    bands = np.array_split(np.arange(len(rated)), 4)
    console.log("  mean by rating quarter (best first):")
    for i, idx in enumerate(bands, 1):
        ws = [rated[j][0] for j in idx]
        console.log(f"   Q{i}: idea {np.mean([ob[w]['idea'] for w in ws]):.3f}  "
                    f"category {np.mean([ob[w]['category'] for w in ws]):.3f}  "
                    f"oblique {np.mean([ob[w]['oblique'] for w in ws]):.3f}")
    order = sorted(rated, key=lambda x: -ob[x[0]]["oblique"])
    console.log("  most oblique: " + ", ".join(w for w, _ in order[:12]))
    console.log("  most literal: " + ", ".join(w for w, _ in order[-12:]))
    console.log(budget.summary())

