"""Taste check: do the scorer and the tournament judge agree with your own ratings?

`prompts/taste.txt` lists names you have rated yourself, one per line:

    vestigome  A   # real root (vestigium), says cleanly
    traceln    D   # cannot be read aloud
    trailsift  ?   # not rated yet (ignored)

Tiers: A (would ship it), B (good), C (meh), D (reject). Every pair of names in different tiers is a
test, and a ranking passes it when it puts the higher-tier name above the other. Agreement is the
share of tests passed: 50% is a coin flip, 100% is your taste.

`namefinder taste` scores every stored rating set (one per judge model + prompt hash) and the old
composite against these pairs, with no API calls. `--eval N` plays N groups of your labelled names
with the current judge prompt (or `--prompt FILE`) and saves nothing, so a prompt change can be
measured for cents before it starts a new rating set.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import random
from collections import Counter
from pathlib import Path
from typing import Sequence

from rich.table import Table

from . import budget, config, console, db

TIERS = {"a": 4, "b": 3, "c": 2, "d": 1}
TIER_NAME = {v: k.upper() for k, v in TIERS.items()}


def load(path: Path | None = None) -> tuple[dict[str, int], dict[str, str]]:
    """(tier per name, your reason per name) from the taste file; unlabelled lines are skipped."""
    p = path or config.PROMPTS_DIR / "taste.txt"
    labels: dict[str, int] = {}
    reasons: dict[str, str] = {}
    if not p.exists():
        return labels, reasons
    for line in p.read_text(encoding="utf-8").splitlines():
        body, _, reason = line.partition("#")
        parts = body.split()
        if len(parts) >= 2 and parts[1].lower() in TIERS:
            w = parts[0].lower()
            labels[w] = TIERS[parts[1].lower()]
            if reason.strip():
                reasons[w] = reason.strip()
    return labels, reasons


def held_out(path: Path | None = None) -> set[str]:
    """Names marked `[held out]` in the taste file: never to be used as examples in a prompt."""
    _, reasons = load(path)
    return {w for w, r in reasons.items() if "[held out]" in r}


def examples(path: Path | None = None, n: int = 6) -> tuple[list[str], list[str]]:
    """(best, worst) rated names that may be shown to a generation model: A then B names, and D names,
    never held-out ones. Shuffled per call so the model does not fixate on one list."""
    labels, _ = load(path)
    hold = held_out(path)
    pool = [w for w in labels if w not in hold]
    rng = random.Random()
    best = sorted((w for w in pool if labels[w] >= 3), key=lambda w: (-labels[w], rng.random()))[:n]
    worst = [w for w in pool if labels[w] == 1]
    rng.shuffle(worst)
    return best, worst[:n]


def agreement(
    labels: dict[str, int], scores: dict[str, float],
) -> tuple[float | None, int, int, list[tuple[str, str]]]:
    """(share of cross-tier pairs ordered as you would, pairs, names covered, worst misses).

    Misses are (your higher-tier name, the name it lost to), biggest tier gap first."""
    names = [w for w in labels if w in scores]
    passed = 0.0
    n = 0
    misses: list[tuple[int, float, str, str]] = []
    for a, b in itertools.combinations(names, 2):
        if labels[a] == labels[b]:
            continue
        hi, lo = (a, b) if labels[a] > labels[b] else (b, a)
        n += 1
        if scores[hi] > scores[lo]:
            passed += 1
        elif scores[hi] == scores[lo]:
            passed += 0.5
        else:
            misses.append((labels[hi] - labels[lo], scores[lo] - scores[hi], hi, lo))
    misses.sort(reverse=True)
    return (passed / n if n else None), n, len(names), [(hi, lo) for _, _, hi, lo in misses]


def _miss_text(labels: dict[str, int], misses: Sequence[tuple[str, str]], k: int = 3) -> str:
    return ", ".join(f"{lo} ({TIER_NAME[labels[lo]]}) > {hi} ({TIER_NAME[labels[hi]]})" for hi, lo in misses[:k])


def _no_labels(path: Path | None) -> None:
    p = path or config.PROMPTS_DIR / "taste.txt"
    console.log(f"[taste] no rated names in {p}. Add lines like `vestigome A  # why` (tiers A-D); "
                "see prompts/taste.example.txt.")


def report(db_path=None, path: Path | None = None) -> None:
    """Agreement of every stored rating set and of the old composite with your tiers (no API calls)."""
    labels, _ = load(path)
    if not labels:
        _no_labels(path)
        return
    from . import tournament
    tiers = Counter(TIER_NAME[t] for t in labels.values())
    rows = []
    with db.session(db_path) as conn:
        current = tournament.judge_id(config.TOURNAMENT_MODEL)
        judges = [r["judge"] for r in conn.execute(
            "SELECT judge, MAX(updated_at) u FROM tournament_ratings GROUP BY judge ORDER BY u DESC")]
        for j in judges:
            rated = {w: r["rating"] for w, r in db.tournament_ratings(conn, j).items()}
            rows.append((j + ("  (current)" if j == current else ""), *agreement(labels, rated)))
        comp = {r["word"]: r["composite"] for r in conn.execute(
            "SELECT word, composite FROM scores WHERE qversion = ? AND composite IS NOT NULL",
            (config.QUESTION_VERSION,))}
        rows.append(("scorer composite", *agreement(labels, comp)))

    t = Table(title=f"Taste agreement  ({len(labels)} rated names: "
                    + ", ".join(f"{tiers[k]} {k}" for k in "ABCD" if tiers.get(k)) + ")",
              title_justify="left", header_style="bold")
    t.add_column("Ranking")
    t.add_column("Names", justify="right")
    t.add_column("Pairs", justify="right")
    t.add_column("Agree", justify="right")
    t.add_column("Worst misses (ranked above your pick)", overflow="fold")
    for name, acc, pairs, covered, misses in rows:
        t.add_row(name, str(covered), str(pairs), "-" if acc is None else f"{100 * acc:.0f}%",
                  _miss_text(labels, misses))
    console._console.print(t)
    console.log("50% = coin flip. A pair counts only when both names are in that ranking; "
                "rate more names to cover more pairs.")


def evaluate(
    groups: int = 20, prompt_file: Path | None = None, model: str | None = None, seed: int | None = None,
    concurrency: int | None = None, group_size: int = 8, path: Path | None = None,
) -> None:
    """Play `groups` random groups of your rated names (two orders each) with a judge prompt and report
    agreement with your tiers, consistency between orders and fatal flags by tier. Nothing is saved."""
    from . import ratings as pl
    from . import tournament

    labels, _ = load(path)
    if not labels:
        _no_labels(path)
        return
    if len(labels) < 4:
        console.log("[taste] rate at least 4 names to run --eval.")
        return
    if len(set(labels.values())) < 2:
        console.log("[taste] every rated name has the same tier; --eval needs at least two tiers.")
        return
    model = model or config.TOURNAMENT_MODEL
    if prompt_file:
        system = Path(prompt_file).read_text(encoding="utf-8").replace("{product}", config.PRODUCT_BLURB)
    else:
        system = tournament.judge_prompt()
    size = min(group_size, len(labels))
    rng = random.Random(seed)
    names = sorted(labels)
    sets = [rng.sample(names, size) for _ in range(groups)]
    console.log(f"[taste] judge {tournament.judge_id(model, system)}: {groups} groups of {size} from "
                f"{len(labels)} rated names, two orders each ({2 * groups} calls); nothing is saved.")
    played = asyncio.run(tournament.play_round(sets, model, system, concurrency or config.SCORE_CONCURRENCY,
                                               rng=rng))
    if not played:
        console.log("[taste] no usable judgements.")
        return

    per_call = [a for a in (agreement(labels, {w: -i for i, w in enumerate(r)})[0] for r, _, _ in played)
                if a is not None]
    by_set: dict[frozenset, list[list[str]]] = {}
    for r, _, order in played:
        by_set.setdefault(frozenset(order), []).append(r)
    consistency = [tournament.pair_agreement(rs[0], rs[1]) for rs in by_set.values() if len(rs) == 2]
    rating, _, _ = pl.fit([r for r, _, _ in played])
    pooled, pairs, covered, misses = agreement(labels, rating)
    hold = held_out(path)
    held, held_pairs, _, _ = agreement({w: t for w, t in labels.items() if w in hold}, rating)

    judged: Counter = Counter()
    fatal: Counter = Counter()
    for r, notes, _ in played:
        for w in r:
            judged[labels[w]] += 1
            if notes.get(w, "").startswith("fatal ("):
                fatal[labels[w]] += 1

    console.log(f"[taste] agreement with your tiers: {100 * sum(per_call) / len(per_call):.0f}% per call, "
                f"{100 * (pooled or 0):.0f}% pooled over {len(played)} calls ({pairs} pairs, {covered} names)")
    if held is not None:
        console.log(f"[taste] held-out names only: {100 * held:.0f}% ({held_pairs} pairs); the honest number once "
                    "prompts carry examples")
    if consistency:
        console.log(f"[taste] judge consistency between the two orders: "
                    f"{100 * sum(consistency) / len(consistency):.0f}%")
    console.log("[taste] fatal flags by your tier: " + ", ".join(
        f"{TIER_NAME[t]} {100 * fatal[t] / judged[t]:.0f}%" for t in sorted(judged, reverse=True)))
    if misses:
        console.log("[taste] worst misses: " + _miss_text(labels, misses, 6))
    console.log(budget.summary())


def evaluate_scorer(
    passes: int = 2, prompt_file: Path | None = None, model: str | None = None, seed: int | None = None,
    batch_size: int = 20, concurrency: int | None = None, path: Path | None = None, db_path=None,
) -> None:
    """Score your rated names `passes` times with the brand scorer (current prompt or `--prompt FILE`),
    in shuffled batches like a real run, and report how the averaged composite and 'fit' agree with your
    tiers. Nothing is saved."""
    from . import net, score

    labels, _ = load(path)
    if not labels or len(set(labels.values())) < 2:
        _no_labels(path) if not labels else console.log("[taste] the scorer eval needs at least two tiers.")
        return
    model = model or config.SCORE_MODEL
    system, user = score.prompts_for(False)
    if prompt_file:
        system = Path(prompt_file).read_text(encoding="utf-8").strip().replace("{product}", config.PRODUCT_BLURB)
    fp = hashlib.sha1(system.encode("utf-8")).hexdigest()[:6]
    names = sorted(labels)
    rng = random.Random(seed)
    batches: list[list[str]] = []
    for _ in range(max(1, passes)):
        order = names[:]
        rng.shuffle(order)
        batches += [order[i : i + batch_size] for i in range(0, len(order), batch_size)]
    console.log(f"[taste] scorer {model}|{fp}: {len(names)} rated names x {passes} passes "
                f"({len(batches)} calls); nothing is saved.")
    url = config.OPENROUTER_URL
    key = config.secret(config.OPENROUTER_KEY_ENV)
    headers = {"Authorization": f"Bearer {key or 'local'}", "Content-Type": "application/json"}
    rows: dict[str, list[dict]] = {}

    async def run_all() -> None:
        sem = asyncio.Semaphore(concurrency or config.SCORE_CONCURRENCY)
        async with net.async_client(timeout=120.0) as http:
            async def one(chunk: list[str]) -> None:
                async with sem:
                    parsed = await score.request_scores(http, url, headers,
                                                        score.brand_body(model, system, user, chunk, url))
                if not parsed:
                    return
                by_word = score.merge_items(parsed)
                with db.session(db_path) as conn:      # read-only: the generic-prefix check
                    for w in chunk:
                        row = score.score_row(conn, w, by_word.get(w, {}), False)
                        if row:
                            rows.setdefault(w, []).append(row)
            await asyncio.gather(*(one(c) for c in batches))

    asyncio.run(run_all())
    if not rows:
        console.log("[taste] no usable scores.")
        return

    def mean(key: str) -> dict[str, float]:
        return {w: sum(r[key] for r in rs) / len(rs) for w, rs in rows.items()}

    comp, fit = mean("composite"), mean("fit")
    hold = held_out(path)
    acc, pairs, covered, misses = agreement(labels, comp)
    fit_acc = agreement(labels, fit)[0]
    held = agreement({w: t for w, t in labels.items() if w in hold}, comp)[0]
    console.log(f"[taste] composite agreement: {100 * (acc or 0):.0f}% ({pairs} pairs, {covered} names); "
                f"'fit' alone: {100 * (fit_acc or 0):.0f}%")
    if held is not None:
        console.log(f"[taste] held-out names only: {100 * held:.0f}%")
    if passes > 1:
        twice = {w: rs for w, rs in rows.items() if len(rs) >= 2}
        a = {w: rs[0]["composite"] for w, rs in twice.items()}
        b = {w: rs[1]["composite"] for w, rs in twice.items()}
        same = [1.0 if (a[x] - a[y]) * (b[x] - b[y]) > 0 else 0.5 if a[x] == a[y] or b[x] == b[y] else 0.0
                for x, y in itertools.combinations(sorted(twice), 2)]
        if same:
            console.log(f"[taste] scorer consistency between passes: {100 * sum(same) / len(same):.0f}%")
    tier_mean = {t: [comp[w] for w in comp if labels[w] == t] for t in TIERS.values()}
    console.log("[taste] mean composite by your tier: " + ", ".join(
        f"{TIER_NAME[t]} {sum(v) / len(v):.3f}" for t, v in sorted(tier_mean.items(), reverse=True) if v))
    if misses:
        console.log("[taste] worst misses: " + _miss_text(labels, misses, 6))
    console.log(budget.summary())
