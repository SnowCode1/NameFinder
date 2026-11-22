"""Tournament: rank names against each other instead of scoring each in isolation.

Each round sorts the pool by current rating, cuts it into groups of `GROUP_SIZE`, and shows every
group to the judge twice (the second time in reverse order, which cancels position bias). The judge
returns a best-to-worst ranking plus one short flaw per name. Rankings become pairwise Elo updates.

Ratings are stored per `judge` = "<model>|<prompt hash>": switching the judge model or editing
`prompts/tournament_prompt.md` starts a fresh rating set, and the old one stays in the database.
Reference names (`prompts/tournament_refs.txt`, `--ref`) always enter, even when they are not in
`words`, so a rating reads as "better or worse than origalia".
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import random
import re
from datetime import datetime, timezone
from typing import Any, Sequence

from . import budget, config, console, db, net

GROUP_SIZE = 8           # measured: 2.4x cheaper per pairwise comparison than 5, same consistency
START_RATING = 1500.0
ELO_K = 24.0
DEFAULT_POOL = 300
DEFAULT_ROUNDS = 6
DEFAULT_PRUNE_RANK = 100 # by default, names clearly below the 100th best stop playing
PRUNE_Z = 2.0            # "clearly": rating + Z standard errors is still below the cut
MIN_GAMES = 4

DEFAULT_JUDGE_PROMPT = (
    "You are a senior brand-naming strategist choosing between candidate names for one product.\n"
    "Product: {product}\n\n"
    "You will be shown a group of candidate names in arbitrary order. Rank them from best to worst "
    "as the name of THIS product. Judge each name on:\n"
    "- a short, TRUE answer to 'why is it called that?' in this product's terms (origins and copies, spread, "
    "matches and their evidence, asking the record, two kinds of source, time and precedence, and its purpose: seeing what "
    "is documented, disputed, uncertain or untraced, and finding links no single search shows). A name may "
    "bridge to the mechanism or to the purpose; either is a true bridge;\n"
    "- fit with the plain technical voice (sounds like a sober term that ought to exist; no hype, no cute or "
    "decorative metaphor, no overclaiming);\n"
    "- how it sounds and reads as a lowercase wordmark: easy to say once, spelled as it sounds, clear stress, "
    "no mishearing;\n"
    "- trap checks: reads as a misspelling of a real word, echoes a well-known brand in any sector, "
    "clinical or pharmaceutical ring, or overclaims (promises certainty, discovery or answers on its own).\n"
    "Be decisive and comparative: you must separate the names, no ties. Ignore the order they are shown in.\n"
    "For every name give its single biggest flaw in at most 10 words (use 'none' only if it truly has none)."
)

JUDGE_USER = """Rank these {n} candidate names, best first:

{words}

Return ONLY a JSON object, no prose or fences, in this form:
{{"ranking": ["best", "...", "worst"], "flaws": {{"name": "biggest flaw, at most 10 words"}}}}
"ranking" must contain every name above exactly once, spelled as given."""


JUDGE_USER_BARE = """Rank these {n} candidate names, best first:

{words}

Return ONLY a JSON object, no prose or fences: {{"ranking": ["best", "...", "worst"]}}
"ranking" must contain every name above exactly once, spelled as given."""


def judge_prompt() -> str:
    text = config.load_prompt("tournament_prompt.md", DEFAULT_JUDGE_PROMPT)
    return text.replace("{product}", config.PRODUCT_BLURB)


def judge_id(model: str, prompt: str | None = None) -> str:
    fp = hashlib.sha1(((prompt if prompt is not None else judge_prompt()) + JUDGE_USER).encode("utf-8")).hexdigest()[:6]
    return f"{model}|{fp}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- reference names -------------------------------------------------------
def load_refs(extra: Sequence[str] | None = None) -> list[str]:
    """Reference names: prompts/tournament_refs.txt (one per line, '#' comments) plus any `--ref`."""
    out: list[str] = []
    p = config.PROMPTS_DIR / "tournament_refs.txt"
    if p.exists():
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip().lower()
                if line:
                    out.append(line)
        except OSError:
            pass
    out += [w.strip().lower() for w in (extra or []) if w.strip()]
    return list(dict.fromkeys(out))


# --- rating maths ----------------------------------------------------------
def expected(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))


NEW_NAME_GAMES = 4       # names with fewer games than this move faster
NEW_NAME_BOOST = 2.0


def apply_ranking(
    ratings: dict[str, float], ranking: Sequence[str], k: float = ELO_K,
    games: dict[str, int] | None = None,
) -> dict[str, float]:
    """New ratings after one ranked group: every pair counts, using ratings from before the group.

    Each name's change is averaged over its n-1 opponents so a group moves ratings about as far
    as one head-to-head game would.
    """
    n = len(ranking)
    delta = {w: 0.0 for w in ranking}
    for i, j in itertools.combinations(range(n), 2):   # i is ranked above j
        a, b = ranking[i], ranking[j]
        e = expected(ratings[a], ratings[b])
        delta[a] += (1.0 - e)
        delta[b] -= (1.0 - e)
    boost = {w: (NEW_NAME_BOOST if games is not None and games.get(w, 0) < NEW_NAME_GAMES else 1.0) for w in ranking}
    return {w: ratings[w] + k * boost[w] * delta[w] / max(1, n - 1) for w in ranking}


def pair_agreement(r1: Sequence[str], r2: Sequence[str]) -> float:
    """Share of word pairs ordered the same way by two rankings of the same group (1.0 = identical)."""
    pos1 = {w: i for i, w in enumerate(r1)}
    pos2 = {w: i for i, w in enumerate(r2)}
    pairs = list(itertools.combinations(r1, 2))
    if not pairs:
        return 1.0
    same = sum(1 for a, b in pairs if (pos1[a] < pos1[b]) == (pos2[a] < pos2[b]))
    return same / len(pairs)


# --- grouping --------------------------------------------------------------
def make_groups(
    words: Sequence[str], ratings: dict[str, float], rng: random.Random, first_round: bool,
    size: int = GROUP_SIZE, se: dict[str, float] | None = None,
) -> list[list[str]]:
    """Swiss-style: sort by rating (random on round one; jittered by each name's standard error
    afterwards, so uncertain names meet a wider range of opponents), cut into groups of `size`.

    A trailing group smaller than 3 is topped up with random names from the pool so every group
    has enough names to compare.
    """
    ordered = list(words)
    if first_round:
        rng.shuffle(ordered)
    else:
        # a name with no rating yet is placed at a random point of the current rating spread, so its
        # first opponents are a fair cross-section (clustering newcomers at one value biased results)
        spread = [ratings[w] for w in words if w in ratings]
        if spread:
            ratings = dict(ratings)
            for w in words:
                if w not in ratings:
                    ratings[w] = rng.choice(spread)
                    se = dict(se or {})
                    se.setdefault(w, 150.0)
        ordered.sort(key=lambda w: ratings.get(w, START_RATING) + rng.gauss(0, (se or {}).get(w, 30.0)),
                     reverse=True)
    groups = [ordered[i : i + size] for i in range(0, len(ordered), size)]
    if len(groups) > 1 and len(groups[-1]) < 3:
        short = groups.pop()
        donors = [w for w in ordered if w not in short]
        short += rng.sample(donors, 3 - len(short))
        groups.append(short)
    return [g for g in groups if len(g) >= 2]


# --- judge calls -----------------------------------------------------------
def parse_judgement(content: str | None, group: Sequence[str]) -> tuple[list[str], dict[str, str]] | None:
    """(ranking, flaws) if the answer ranks exactly the names in `group`, else None."""
    if not content or not isinstance(content, str):
        return None
    text = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    raw = data.get("ranking") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return None
    ranking = [str(w).strip().lower() for w in raw]
    if sorted(ranking) != sorted(group):
        return None
    flaws = data.get("flaws") if isinstance(data.get("flaws"), dict) else {}
    notes = {str(k).strip().lower(): str(v).strip()[:120] for k, v in flaws.items() if str(k).strip().lower() in group}
    return ranking, notes


async def _judge(
    http, url, headers, model, system, group: list[str], flaws: bool = True,
) -> tuple[list[str], dict[str, str]] | None:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": (JUDGE_USER if flaws else JUDGE_USER_BARE).format(
                n=len(group), words="\n".join(f"- {w}" for w in group))},
        ],
        "temperature": 0.2,
        "max_tokens": 1500,
        **config.openrouter_extras(url),
    }
    for attempt in range(5):
        if budget.exhausted():
            return None
        try:
            r = await http.post(f"{url}/chat/completions", json=body, headers=headers)
        except Exception as exc:
            wait_s = min(60.0, 3.0 * (2 ** attempt))
            console.log(f"[tournament] network glitch ({exc.__class__.__name__}); retrying in {wait_s:.0f}s")
            await asyncio.sleep(wait_s)
            continue
        if r.status_code == 429:
            await asyncio.sleep(net.backoff_delay(attempt, r.headers.get("retry-after")))
            continue
        if r.status_code >= 500:
            await asyncio.sleep(min(60.0, 3.0 * (2 ** attempt)))
            continue
        if r.status_code != 200:
            console.log(f"[tournament] HTTP {r.status_code}: {r.text[:160]}")
            return None
        try:
            payload = r.json()
            budget.record(payload)
            content = (payload.get("choices") or [{}])[0].get("message", {}).get("content")
            parsed = parse_judgement(content, group)
            if parsed:
                return parsed
        except (ValueError, AttributeError, KeyError, IndexError):
            pass
        await asyncio.sleep(1.5)
    return None


async def play_round(
    groups: list[list[str]], model: str, system: str, concurrency: int, pbar: Any = None,
    api_url: str | None = None, api_key: str | None = None, rng: random.Random | None = None,
) -> list[tuple[list[str], dict[str, str], list[str]]]:
    """Judge every group in two orders. Returns (ranking, flaws, shown_order) per successful call."""
    url = api_url or config.OPENROUTER_URL
    key = api_key or config.secret(config.OPENROUTER_KEY_ENV)
    if not key and "localhost" not in url and "127.0.0.1" not in url:
        raise SystemExit(f"[tournament] OPENROUTER_API_KEY is not set in .env. {config.WRITE_KEY_HINT}")
    headers = {"Authorization": f"Bearer {key or 'local'}", "Content-Type": "application/json"}
    rng = rng or random.Random()
    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[tuple[list[str], dict[str, str], list[str]]] = []

    async with net.async_client(timeout=180.0) as http:
        async def one(order: list[str], flaws: bool) -> None:
            async with sem:
                out = await _judge(http, url, headers, model, system, order, flaws)
            if out:
                results.append((out[0], out[1], order))
            if pbar:
                pbar.update(1)
                pbar.set_postfix_str(budget.metrics_text(), refresh=False)

        jobs = []
        for g in groups:
            first = list(g)
            rng.shuffle(first)
            jobs.append(one(first, True))
            # reverse order cancels position bias; flaw notes from one ordering are enough
            jobs.append(one(list(reversed(first)), False))
        await asyncio.gather(*jobs)
    return results


def resolve_judge(conn, model: str | None = None) -> tuple[str | None, str | None]:
    """(judge id, note) whose ratings `top` should read.

    The judge for the configured model and current prompt if it has ratings; otherwise the most
    recently updated rating set, with a note saying so. (None, None) when nothing is rated yet.
    """
    wanted = judge_id(model or config.TOURNAMENT_MODEL)
    if conn.execute("SELECT 1 FROM tournament_ratings WHERE judge = ? LIMIT 1", (wanted,)).fetchone():
        return wanted, None
    row = conn.execute(
        "SELECT judge FROM tournament_ratings ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None, None
    if model:
        return None, f"no tournament ratings for judge {wanted}"
    return row["judge"], f"no ratings for the current judge ({wanted}); showing the latest set, {row['judge']}"


# --- driver ----------------------------------------------------------------
def refit(
    conn, judge: str, init: dict[str, float] | None = None, notes: dict[str, str] | None = None,
    ts: str | None = None,
) -> tuple[dict[str, float], dict[str, float], dict[str, int]]:
    """Refit Plackett-Luce ratings for this judge from every logged match and store them."""
    from . import ratings as pl
    matches = db.tournament_match_rankings(conn, judge)
    if not matches:
        return {}, {}, {}
    if init is None:
        init = {w: r["rating"] for w, r in db.tournament_ratings(conn, judge).items()}
    rating, se, games = pl.fit(matches, init=init)
    db.save_tournament_fit(conn, judge, rating, se, games, notes or {}, ts or _now())
    return rating, se, games


def prune_threshold(
    ratings: dict[str, float], bench: str | None = None, rank_cut: int | None = None, margin: float = 50.0,
) -> float | None:
    """Rating below which a name is out: the benchmark's rating minus `margin`, and/or the rating of
    the `rank_cut`-th best name minus `margin`; the stricter (higher) of the two when both are given."""
    cuts: list[float] = []
    if bench and bench in ratings:
        cuts.append(ratings[bench] - margin)
    if rank_cut and len(ratings) >= rank_cut:
        cuts.append(sorted(ratings.values(), reverse=True)[rank_cut - 1] - margin)
    return max(cuts) if cuts else None


def prune_pool(
    pool: Sequence[str], ratings: dict[str, float], games: dict[str, int], keep: Sequence[str],
    threshold: float | None, min_games: int = MIN_GAMES,
    se: dict[str, float] | None = None, z: float = 0.0,
) -> tuple[list[str], list[str]]:
    """(active, pruned): names whose rating plus `z` standard errors is below `threshold`, after at
    least `min_games` games, stop playing (so a name is cut only when it is clearly out).

    `keep` names (the reference names) always stay. Names with too few games always stay, since
    their rating is still mostly noise. Pruned names keep their stored ratings.
    """
    if threshold is None:
        return list(pool), []
    keepset = set(keep)
    active, pruned = [], []
    for w in pool:
        upper = ratings.get(w, START_RATING) + z * (se or {}).get(w, 0.0)
        if w not in keepset and games.get(w, 0) >= min_games and upper < threshold:
            pruned.append(w)
        else:
            active.append(w)
    return active, pruned


def plan_pool(
    conn, qversion: str, size: int, refs: Sequence[str], tld: str,
    source: str = "score", judge: str | None = None, carry: int = 60,
) -> tuple[list[str], dict[str, list[str]]]:
    """The names to play, and the parts they came from (rated / ideas / bulk).

    `score`: top coined names by the old composite.
    `ideas`: idea, variant and rediscovered names plus the best-rated `carry` names as competition
             (taken from the latest earlier rating set when this judge has none yet).
    `rated`: the top `size` names already rated under this judge, plus idea/variant names not yet rated
             (a confirmation pass that gives the leaders more games, and a place for new variants to join).
    `both`:  everything already rated under this judge, the idea names, and the top `size` names by
             the old composite, so unselected old names play against the idea names and the old elite.
    """
    judge = judge or ""
    rated = list(db.tournament_ratings(conn, judge))
    parts: dict[str, list[str]] = {"rated": rated, "ideas": [], "bulk": []}
    if source == "ideas":
        carry_judge = judge if rated else (resolve_judge(conn)[0] or judge)
        parts["ideas"] = db.tournament_pool_ideas(conn, carry_judge, tld, carry)
        pool = parts["ideas"]
    elif source == "both":
        parts["ideas"] = db.tournament_pool_ideas(conn, judge, tld, 0)
        # the control must be untouched: skip names any earlier tournament already rated (those were pre-filtered)
        seen = {r["word"] for r in conn.execute("SELECT DISTINCT word FROM tournament_ratings")}
        wide = db.tournament_pool(conn, qversion, size + len(seen), tld)
        parts["bulk"] = [w for w in wide if w not in seen][:size]
        pool = rated + parts["ideas"] + parts["bulk"]
    elif source == "rated":
        top = [r["word"] for r in conn.execute(
            "SELECT word FROM tournament_ratings WHERE judge = ? ORDER BY rating DESC LIMIT ?", (judge, size))]
        parts["ideas"] = [w for w in db.tournament_pool_ideas(conn, judge, tld, 0) if w not in set(rated)]
        pool = top + parts["ideas"]
    else:
        pool = db.tournament_pool(conn, qversion, size, tld)
    return list(dict.fromkeys(list(refs) + pool)), parts


def build_pool(conn, qversion, size, refs, tld, source="score", judge=None, carry=60) -> list[str]:
    return plan_pool(conn, qversion, size, refs, tld, source, judge, carry)[0]


def label_pool(conn, judge: str, refs: Sequence[str], parts: dict[str, list[str]], source: str, carry: int) -> None:
    """Tag old-pool names so the results can be read by group: `elite` (selected by an earlier set) and `bulk`.

    `elite` is backfilled once per judge from the best `carry` names of the most recent other rating set;
    `bulk` marks old names added unselected in `both` mode (never rated under this judge before).
    """
    if source not in ("ideas", "both"):
        return
    if not db.tournament_tags(conn, judge):
        other = conn.execute(
            "SELECT judge FROM tournament_ratings WHERE judge != ? ORDER BY updated_at DESC LIMIT 1", (judge,)
        ).fetchone()
        if other:
            mine = set(parts["rated"])
            elite = [r["word"] for r in conn.execute(
                "SELECT word FROM tournament_ratings WHERE judge = ? ORDER BY rating DESC LIMIT ?",
                (other["judge"], carry))]
            db.tag_words(conn, judge, [w for w in elite if w in mine], "elite")
    if source == "both":
        skip = set(parts["rated"]) | set(parts["ideas"]) | set(refs)
        db.tag_words(conn, judge, [w for w in parts["bulk"] if w not in skip], "bulk")


def ranking_rows(
    conn, judge: str, refs: Sequence[str], tld: str,
    prev_rank: dict[str, int] | None = None, delta: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Rated names best first, as rows for `console.build_tournament_table`."""
    rated = db.tournament_ratings(conn, judge)
    words = sorted(rated, key=lambda w: rated[w]["rating"], reverse=True)
    doms = db.domain_map(conn, words)
    old = {
        r["word"]: r["composite"]
        for r in conn.execute("SELECT word, composite FROM scores WHERE qversion = ?", (config.QUESTION_VERSION,))
    }
    refset = set(refs)
    tags = db.tournament_tags(conn, judge)
    engines = {}
    created: dict[str, str] = {}
    v2_since = db.get_meta(conn, "idea_v2_since")
    found_by_ideas: set[str] = set()
    for i in range(0, len(words), 500):
        part = words[i : i + 500]
        q = ",".join("?" * len(part))
        engines.update({r["word"]: r["engine"] for r in conn.execute(
            f"SELECT word, engine FROM words WHERE word IN ({q})", part)})
        created.update({r["word"]: r["created_at"] for r in conn.execute(
            f"SELECT word, created_at FROM words WHERE word IN ({q})", part)})
        found_by_ideas.update(r["word"] for r in conn.execute(
            f"SELECT word FROM name_origins WHERE word IN ({q})", part))
    rows = []
    for i, w in enumerate(words, 1):
        r = rated[w]
        origin = "ref" if w in refset else {"idea": "idea", "variant": "variant"}.get(
            engines.get(w) or "", "both" if w in found_by_ideas else (tags.get(w) or "old"))
        if origin == "idea" and v2_since and (created.get(w) or "") >= v2_since:
            origin = "idea2"   # made after the transparent-root retune
        rows.append({
            "origin": origin,
            "word": w, "rating": r["rating"], "games": r["games"], "rank": i,
            "move": (prev_rank[w] - i) if prev_rank is not None and w in prev_rank else None,
            "delta": (delta or {}).get(w),
            "domain": doms.get(w, {}).get(tld),
            "old": old.get(w), "note": r["last_note"], "is_ref": w in refset,
        })
    return rows


def _benchmark(rows: Sequence[dict[str, Any]], refs: Sequence[str]) -> tuple[str | None, float | None]:
    """The first reference name that has a rating (origalia, if listed first), and its rating."""
    by = {r["word"]: r["rating"] for r in rows}
    for w in refs:
        if w in by:
            return w, by[w]
    return None, None


def _table(rows, refs, limit, tld, title, caption=None):
    bench, bench_rating = _benchmark(rows, refs)
    return console.build_tournament_table(rows, title=title, limit=limit, bench=bench,
                                          bench_rating=bench_rating, tld=tld, caption=caption)


def _composition(rows, n: int = 20) -> str:
    """Where the current top n came from, e.g. 'top 20: 9 idea, 6 old, 3 variant, 2 ref'."""
    from collections import Counter
    counts = Counter(r.get("origin", "old") for r in rows[:n])
    if len(counts) <= 1 and counts.get("old", counts.get("elite", 0)):
        return ""   # nothing but legacy names: no comparison to draw
    order = ("idea2", "idea", "variant", "both", "elite", "bulk", "old", "ref")
    return f"top {min(n, len(rows))}: " + ", ".join(f"{counts[k]} {k}" for k in order if counts.get(k))


def _beats_line(rows, refs) -> str:
    bench, bench_rating = _benchmark(rows, refs)
    if bench is None:
        return ""
    refset = set(refs)
    above = [r["word"] for r in rows if r["rating"] > bench_rating and r["word"] not in refset]
    line = f"{len(above)} pool names rated above {bench}"
    comp = _composition(rows)
    return f"{line}  ·  {comp}" if comp else line


def run(
    pool_size: int = DEFAULT_POOL,
    rounds: int = DEFAULT_ROUNDS,
    model: str | None = None,
    refs: Sequence[str] | None = None,
    tld: str = "com",
    concurrency: int | None = None,
    backend: str | None = None,
    show: int = 30,
    show_only: bool = False,
    db_path=None,
    seed: int | None = None,
    source: str = "score",
    carry: int = 60,
    prune_below: str | None = None,
    prune_rank: int | None = DEFAULT_PRUNE_RANK,
    margin: float = 0.0,
    min_games: int = MIN_GAMES,
    z: float = PRUNE_Z,
    group_size: int = GROUP_SIZE,
    refit_only: bool = False,
) -> None:
    from shutil import get_terminal_size

    from rich.text import Text

    qversion = config.QUESTION_VERSION
    if not backend:
        backend = "openrouter" if config.secret(config.OPENROUTER_KEY_ENV) else "local"
    if backend in ("local", "lmstudio"):
        api_url, api_key = "http://localhost:1234/v1", "lm-studio"
        model = model or "qwen3-4b-instruct-2507"
        concurrency = 1
    else:
        api_url = api_key = None
        model = model or config.TOURNAMENT_MODEL
        concurrency = concurrency or config.SCORE_CONCURRENCY

    system = judge_prompt()
    judge = judge_id(model, system)
    ref_list = load_refs(refs)
    rng = random.Random(seed)

    with db.session(db_path) as conn:
        pool, parts = plan_pool(conn, qversion, pool_size, ref_list, tld, source, judge, carry)
        label_pool(conn, judge, ref_list, parts, source, carry)
        if refit_only or not show_only:   # ratings come from a batch fit over every logged match
            refit(conn, judge)
        stored = db.tournament_ratings(conn, judge)
        ratings = {w: r["rating"] for w, r in stored.items()}
        games = {w: r["games"] for w, r in stored.items()}
        se = {w: (r["se"] if "se" in r.keys() and r["se"] is not None else 60.0) for w, r in stored.items()}
        start_round = db.tournament_round_count(conn, judge)

    if show_only or refit_only:
        show_leaderboard(judge, ref_list, show, tld, db_path)
        return
    if len(pool) < 3:
        console.log(f"[tournament] only {len(pool)} names qualify (free .{tld}, scored with the current version). "
                    "Run `namefinder coin` / `check-domains` first or lower the filters.")
        return

    n_groups = -(-len(pool) // group_size)
    header = Text()
    for label, value, extra in (
        ("judge", judge, ""),
        ("pool", str(len(pool)), f" ({len(ref_list)} reference, source: {source})"),
        ("groups/round", str(n_groups), ""),
        ("resuming after round", str(start_round), ""),
    ):
        header.append(("   " if header.plain else "") + f"{label} ", style="dim")
        header.append(value, style="bold white")
        if extra:
            header.append(extra, style="dim")

    agree: list[float] = []
    played_rounds = 0
    table_rows = min(show, max(6, get_terminal_size(fallback=(120, 40)).lines - 14))

    state = {"active": len(pool), "threshold": None}

    def facts() -> dict[str, str]:
        out = {"rounds played": f"{played_rounds}/{rounds}"}
        if prune_below or prune_rank:
            out["active names"] = f"{state['active']}/{len(pool)}"
            if state["threshold"] is not None:
                out["prune below"] = f"{state['threshold']:.0f}"
        if agree:
            out["judge consistency"] = f"{100 * sum(agree) / len(agree):.0f}%"
        return out

    interrupted = False
    try:
        with console.TournamentDashboard(header, rounds, table_rows=table_rows) as dash:
            if stored:   # resuming: show where things stand before the first call
                with db.session(db_path) as conn:
                    rows = ranking_rows(conn, judge, ref_list, tld)
                dash.set_table(_table(rows, ref_list, table_rows, tld, "Current ranking (before this run)",
                                      _beats_line(rows, ref_list)))
            dash.set_facts(**facts())
            for n in range(1, rounds + 1):
                if budget.exhausted():
                    break
                round_no = start_round + n
                with db.session(db_path) as conn:
                    prev_rows = ranking_rows(conn, judge, ref_list, tld)
                prev_rank = {r["word"]: r["rank"] for r in prev_rows}
                active = pool
                if prune_below or prune_rank:
                    thr = prune_threshold({w: ratings[w] for w in pool if w in ratings},
                                          prune_below.lower() if prune_below else None, prune_rank, margin)
                    active, dropped = prune_pool(pool, ratings, games, ref_list, thr, min_games, se, z)
                    state["active"], state["threshold"] = len(active), thr
                    if dropped:
                        console.log(f"[tournament] pruned {len(dropped)} names rated below {thr:.0f} "
                                    f"({len(active)} still playing)")
                    if len(active) < 3:
                        console.log("[tournament] fewer than 3 names left after pruning; stopping.")
                        break
                groups = make_groups(active, ratings, rng, first_round=(round_no == 1), size=group_size, se=se)
                dash.begin_round(round_no, n, len(groups) * 2)
                played = asyncio.run(play_round(groups, model, system, concurrency, dash, api_url, api_key, rng))
                if not played:
                    console.log("[tournament] no usable judgements this round; stopping.")
                    break
                by_group: dict[frozenset, list[list[str]]] = {}
                for ranking, _, order in played:
                    by_group.setdefault(frozenset(order), []).append(ranking)
                agree += [pair_agreement(rs[0], rs[1]) for rs in by_group.values() if len(rs) == 2]

                ts = _now()
                snapshot = {w: ratings.get(w, START_RATING) for w in pool}
                notes_by_word: dict[str, str] = {}
                with db.session(db_path) as conn:
                    for ranking, flaws, _ in played:
                        db.save_tournament_match(conn, judge, round_no, ranking, flaws, ts)
                        notes_by_word.update({w: f for w, f in flaws.items() if f})
                    new_r, new_se, new_g = refit(conn, judge, init=ratings, notes=notes_by_word, ts=ts)
                    ratings, se, games = dict(new_r), dict(new_se), dict(new_g)
                    delta = {w: ratings[w] - snapshot[w] for w in pool if w in ratings and w in snapshot}
                    rows = ranking_rows(conn, judge, ref_list, tld, prev_rank=prev_rank, delta=delta)
                played_rounds += 1
                dash.set_facts(**facts())
                dash.set_table(_table(rows, ref_list, table_rows, tld, f"After round {round_no}",
                                      _beats_line(rows, ref_list)))
    except (KeyboardInterrupt, asyncio.CancelledError):
        interrupted = True

    if interrupted:
        console.log("[tournament] interrupted; finished rounds are saved, the current round was discarded.")
    if agree:
        console.log(f"[tournament] judge consistency: {100 * sum(agree) / len(agree):.0f}% of name pairs were ordered "
                    "the same in both shown orders (50% = coin flip, 100% = fully stable).")
    console.log(budget.summary())
    if budget.exhausted():
        console.log(budget.stop_notice())

    show_leaderboard(judge, ref_list, show, tld, db_path)


def show_leaderboard(judge: str, refs: Sequence[str], limit: int, tld: str, db_path=None) -> None:
    with db.session(db_path) as conn:
        rows = ranking_rows(conn, judge, refs, tld)
    if not rows:
        console.log(f"[tournament] no ratings yet for judge {judge}. Run `namefinder tournament`.")
        return
    parts = [_beats_line(rows, refs), f"{len(rows)} names rated", "magenta = your reference names"]
    console._console.print(_table(rows, refs, limit, tld, f"Tournament ranking  ({judge})", "  ·  ".join(x for x in parts if x)))
    if len({r["origin"] for r in rows}) > 1:
        console._console.print(console.build_origin_table(rows))
