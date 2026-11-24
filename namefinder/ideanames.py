"""Stage 2 of idea-first naming: turn each approved idea into names, with the reasoning for every name.

One request per idea (so the model thinks about one idea at a time), each name returned with a short
construction note ("imago + -ome: the whole body of copies of one image"). Names pass the same quality
gate as the older engines, get registered with engine='idea', and have their domains checked, so the
tournament can draw them in with `--source ideas`.

`register` and `check_domains` are shared with `evolve.py` (stage 3).
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Sequence

from rich import box
from rich.table import Table

from . import budget, coin, config, console, db, domains, families, llm, net, tournament

DEFAULT_PER_IDEA = 20

NAME_RULES = (
    "Rules for every name:\n"
    "- 5 to {max_len} letters (never fewer than 5), two to four syllables, easy to say on first sight.\n"
    "- A TERM THAT OUGHT TO EXIST. The name should sound like a sober technical term beside codex, lexicon, genome "
    "or nexus: a classical root that an educated reader already half-knows through a common English word, shaped "
    "with a standard ending (-gram, -ome, -us, -eme, -ix, -dex, -el) or prefix (sub-, inter-, pluri-, retro-, "
    "ante-). Literal is fine when the result is a well-made term. Say it aloud: the ending must sound natural on "
    "its root; an arbitrary Latin ending bolted on (-an, -on, -um) usually sounds wrong. A plain English compound "
    "is acceptable only when both halves are strong and the story is true. A root the reader would have to look "
    "up (hodos, fons, origo, rog-, pakhus, syndesm-) fails, as does a soft half (-span, -map, -field, -carry), a "
    "cute question compound (whence-), and any reading as surveillance, medicine or a photo-editing app.\n"
    "- Spread the names across roots: at most 4 names per root; vary the ending and the prefix; no more than three "
    "names may share the same last two letters.\n"
    "- Spelled the way it sounds: no k-for-c respellings, no stacked consonants, no silent letters. It must not read "
    "as a misspelling of a real word.\n"
    "- Not an ordinary dictionary word, not a known brand or a sound-alike of one in any sector (Instagram, Imgur, "
    "Polaroid, Pokedex, Rolodex, Kleenex), and not a drug name or crude sound-alike.\n"
    "- Plain and technical in feel. Nothing cute, nothing that claims truth or certainty (verity, oracle, proof), "
    "no decorative metaphor (loom, weave, prism).\n"
    "- Lowercase letters a to z only."
)

DEFAULT_NAMES_PROMPT = (
    "You are a brand-naming linguist inventing names for an image provenance product.\n"
    "Product: {product}\n\n"
    "You will be given ONE idea (a true statement about the product) and the roots that may carry it. Invent names "
    "that express that idea and no other. Think about the idea first: what is the single clearest thing a reader "
    "should half-hear in the name?\n\n"
    + NAME_RULES
)

NAMES_USER = """Idea: {idea}
Roots to consider (not exclusive; prefer the familiar ones, and add a familiar English root of your own where it carries the idea better): {roots}

Invent {n} different candidate names for this idea.
{feedback}
For every name add "built": ONE clean line of at most 14 words saying which parts it is made of and what it means, e.g. "trail + sift: sorting every copy back to the first upload". Never correct yourself or add commentary in this field; if a name is wrong, leave it out.
Return ONLY a JSON array, no prose or fences, of objects: {{"name": "...", "built": "..."}}"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def names_prompt(max_len: int = 10) -> str:
    text = config.load_prompt("names_prompt.md", DEFAULT_NAMES_PROMPT)
    return text.replace("{product}", config.PRODUCT_BLURB).replace("{max_len}", str(max_len))


def _clean(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    w = name.strip().lower()
    return w if re.fullmatch(r"[a-z]{3,16}", w) else None


_SELF_CORRECT = re.compile(r"\b(?:no,? wait|wait,|restate|actually,|oops|hmm|scratch that|dropped)\b[:\s—-]*", re.I)


def clean_note(text: str) -> str:
    """Keep only the clean final line of a construction note; models sometimes leak a self-correction."""
    text = (text or "").strip()
    parts = _SELF_CORRECT.split(text)
    if len(parts) > 1:
        text = parts[-1].strip(" :,;.-—")
    return text[:200] if len(text) >= 8 else ""


def judge_feedback(conn, n_best: int = 12, n_flaws: int = 8) -> tuple[list[str], list[str]]:
    """(best-rated names, sample of flaws the judge names for the strongest names), or empty lists."""
    judge, _ = tournament.resolve_judge(conn)
    if not judge:
        return [], []
    rated = db.tournament_ratings(conn, judge)
    order = [w for w in sorted(rated, key=lambda w: rated[w]["rating"], reverse=True) if not families.banned(w)]
    best = order[:n_best]
    flaws = db.tournament_flaws(conn, judge, n=1)
    pool = [flaws[w][0] for w in order[:30] if flaws.get(w)]
    random.shuffle(pool)
    return best, pool[:n_flaws]


# --- shared: gate, register, domains -----------------------------------------
class Candidate(dict):
    """{"word", "idea_id", "parent_word", "construction"}"""


STEM_LEN = 4


def register(
    cands: Sequence[Candidate], kind: str, engine: str, db_path=None, min_len: int = 5, max_len: int = 10,
    stem_cap_group: int = 3, stem_cap_batch: int = 10,
) -> tuple[list[Candidate], Counter]:
    """Gate, dedupe and store candidates as coined words. Returns (accepted, reject counts by reason).

    A name already in the old coined pool (never rated) is not re-inserted, so its legacy record is
    untouched; it is kept in `accepted` with `rediscovered=True` and an origin row, which lets the
    tournament pool include it. Names already rated or in the references are dropped.

    A stem (the first letters of the name) may appear at most `stem_cap_group` times per idea (per
    parent, for variants) and `stem_cap_batch` times overall, so one root with many endings is not
    mistaken for many names.
    """
    stats: Counter = Counter()
    accepted: list[Candidate] = []
    with db.session(db_path) as conn:
        known = {r[0] for r in conn.execute("SELECT word FROM words")}
        refs = set(tournament.load_refs())
        # near-duplicates are judged against names this system made or rated, not the 60k legacy names
        mine = {r[0] for r in conn.execute("SELECT word FROM words WHERE engine IN ('idea', 'variant')")}
        rated = {r[0] for r in conn.execute("SELECT DISTINCT word FROM tournament_ratings")}
        mine |= rated | refs
        near = coin.NearIndex(mine, set(), config.COIN_NEAR_DUP)
        gate = families.Gate(conn)
        seen: set[str] = set()
        group_stems: Counter = Counter()
        batch_stems: Counter = Counter()
        ts = _now()
        for c in cands:
            w = c["word"]
            if w in seen:
                stats["repeat in this batch"] += 1
                continue
            seen.add(w)
            if w in rated or w in refs or w == c.get("parent_word"):
                stats["already rated"] += 1
                continue
            if w in known:
                # an independent rediscovery of a name the old engines made: keep it, flag it
                has_origin = conn.execute("SELECT 1 FROM name_origins WHERE word = ?", (w,)).fetchone()
                if has_origin or kind != "idea":
                    stats["already known"] += 1
                    continue
                db.add_origin(conn, w, kind, c.get("idea_id"), None, c.get("construction"), ts)
                c["rediscovered"] = True
                accepted.append(c)
                continue
            if not coin.acceptable(w, min_len=min_len, max_len=max_len):
                stats["banned root" if families.banned(w) else "failed quality gate"] += 1
                continue
            verdict = gate.check(w, (c.get("parent_word") or "",))
            if not verdict.ok:
                stats[gate.last_category] += 1
                continue
            stem = w[:STEM_LEN]
            group = c.get("idea_id") or c.get("parent_word")
            if group_stems[(group, stem)] >= stem_cap_group or batch_stems[stem] >= stem_cap_batch:
                stats["too many on one stem"] += 1
                continue
            if not near.admit(w):
                stats["one letter from an existing name"] += 1
                continue
            group_stems[(group, stem)] += 1
            batch_stems[stem] += 1
            parents = f"idea:{c['idea_id']}" if c.get("idea_id") else (c.get("parent_word") or "")
            conn.execute(
                "INSERT OR IGNORE INTO words (word, pos, zipf, source, created_at, engine, parents) "
                "VALUES (?, 'n', 3.0, 'coined', ?, ?, ?)",
                (w, ts, engine, parents),
            )
            db.add_origin(conn, w, kind, c.get("idea_id"), c.get("parent_word"), c.get("construction"), ts)
            accepted.append(c)
    return accepted, stats


def check_domains(words: Sequence[str], db_path=None, roles: Sequence[str] | None = None) -> dict[str, int]:
    """RDAP-check the domains of the given words; returns counts.

    `roles` limits which domain roles are checked (default: `config.GENERATION_DOMAIN_ROLES`, i.e. .com);
    pass None-equivalent `config.active_domain_roles()` for all. Domains already checked are skipped.
    """
    roles = tuple(roles) if roles else config.GENERATION_DOMAIN_ROLES
    with db.session(db_path) as conn:
        have = {(r[0], r[1]) for r in conn.execute("SELECT word, tld FROM domains")}
    jobs = [(w, dom, role) for w in words for dom, role in domains.domains_for(w)
            if role in roles and (w, role) not in have]
    if not jobs:
        return {}
    console.log(f"[domains] checking {len(jobs)} domains ({', '.join('.' + r.replace('use-com', 'use…com') for r in roles)}) "
                f"for {len(words)} names...")
    return asyncio.run(domains._run_async(jobs, db_path=db_path))


def free_com(words: Sequence[str], tld: str = "com", db_path=None) -> set[str]:
    if not words:
        return set()
    with db.session(db_path) as conn:
        q = ",".join("?" * len(words))
        rows = conn.execute(
            f"SELECT word FROM domains WHERE tld = ? AND status = 'available' AND word IN ({q})", [tld, *words]
        ).fetchall()
    return {r["word"] for r in rows}


def show_new_names(accepted: Sequence[Candidate], free: set[str], title: str, show_parent: bool = False,
                   limit: int = 40, tld: str = "com") -> None:
    """Table of new names whose .com is free (the ones that can enter the tournament)."""
    rows = [c for c in accepted if c["word"] in free][:limit]
    if not rows:
        return
    t = Table(title=f"[bold cyan]{title}[/]", box=box.ROUNDED, expand=True, header_style="bold cyan",
              border_style="dim", caption=f"{len(free)} of {len(accepted)} names have a free .{tld}  ·  "
                      f"{sum(1 for c in accepted if c.get('rediscovered'))} rediscovered from the old pool",
              caption_style="dim")
    t.add_column("Name", style="bold white", no_wrap=True)
    t.add_column("From" if show_parent else "Idea", style="dim", no_wrap=True)
    t.add_column("Built", style="italic", ratio=1)
    for c in rows:
        src = c.get("parent_word") if show_parent else f"#{c.get('idea_id')}"
        name = f"{c['word']} [dim](old pool too)[/]" if c.get("rediscovered") else c["word"]
        t.add_row(name, str(src or ""), c.get("construction") or "")
    console._console.print(t)


# --- the stage ------------------------------------------------------------------
async def _propose(ideas, per_idea, feedback, url, key, model, system, concurrency, bar, avoid=""):
    sem = asyncio.Semaphore(max(1, concurrency))
    out: list[Candidate] = []
    best, flaws = feedback

    async with net.async_client(timeout=180.0) as http:
        async def one(row) -> None:
            try:
                roots = ", ".join(f"{r['root']} ({r.get('language', '')}: {r.get('meaning', '')})"
                                  for r in json.loads(row["roots"] or "[]")) or "(none given; choose your own)"
            except ValueError:
                roots = "(none given; choose your own)"
            fb = ""
            if best:
                fb += f"Names a judge currently rates best for this product (match their quality, do not copy them): {', '.join(best)}.\n"
            if flaws:
                fb += "Flaws the judge still finds even in those: " + "; ".join(flaws) + ". Avoid them.\n"
            if avoid:
                fb += avoid + "\n"
            user = NAMES_USER.format(idea=row["text"], roots=roots, n=per_idea, feedback=fb)
            async with sem:
                parsed = await llm.chat_json(http, url, key, model, system, user,
                                             temperature=config.COIN_LLM_TEMPERATURE, max_tokens=3500, label="generate")
            for item in parsed if isinstance(parsed, list) else []:
                if not isinstance(item, dict):
                    continue
                w = _clean(item.get("name"))
                if w:
                    out.append(Candidate(word=w, idea_id=row["id"], parent_word=None,
                                         construction=clean_note(str(item.get("built") or ""))))
            bar.update(1)
            bar.note(f"[magenta]{len(out)} names[/]")

        await asyncio.gather(*(one(r) for r in ideas))
    return out


def run(
    per_idea: int = DEFAULT_PER_IDEA,
    idea_ids: Sequence[int] | None = None,
    model: str | None = None,
    backend: str | None = None,
    concurrency: int | None = None,
    min_len: int = 5,
    max_len: int = 10,
    check: bool = True,
    use_feedback: bool = True,
    all_tlds: bool = False,
    db_path=None,
) -> None:
    url, key, model = llm.resolve(model, config.NAMEGEN_MODEL, backend)
    with db.session(db_path) as conn:
        ideas = db.list_ideas(conn, "approved")
        if idea_ids:
            ideas = [r for r in ideas if r["id"] in set(idea_ids)]
        feedback = judge_feedback(conn) if use_feedback else ([], [])
        avoid = families.avoid_text(conn)
    if not ideas:
        console.log("[generate] no approved ideas. Run `namefinder ideas generate`, then `namefinder ideas review`.")
        return

    system = names_prompt(max_len)
    with db.session(db_path) as conn:   # names made from here on use the transparent-root prompt
        if not db.get_meta(conn, "idea_v2_since"):
            db.set_meta(conn, "idea_v2_since", _now())
    console.log(f"[generate] model={model}  ideas={len(ideas)}  names per idea={per_idea}"
                + ("  (judge feedback included)" if feedback[0] else ""))
    with console.RichBar("Generating names", total=len(ideas), unit="idea", style="magenta") as bar:
        raw = asyncio.run(_propose(ideas, per_idea, feedback, url, key, model, system,
                                   concurrency or config.SCORE_CONCURRENCY, bar, avoid))
    random.shuffle(raw)   # so the near-duplicate gate does not always favour the first idea
    accepted, stats = register(raw, "idea", "idea", db_path, min_len, max_len)
    console.log(f"[generate] {len(raw)} proposed, {len(accepted)} kept"
                + ("; dropped: " + ", ".join(f"{n} {why}" for why, n in stats.most_common()) if stats else ""))
    if not accepted:
        console.log(budget.summary())
        return

    words = [c["word"] for c in accepted]
    if check:
        counts = check_domains(words, db_path, config.active_domain_roles() if all_tlds else None)
        console.log(f"[domains] {counts.get('available', 0)} free, {counts.get('registered', 0)} taken")
    free = free_com(words, "com", db_path) if check else set()
    show_new_names(accepted, free, "New names from ideas (free .com)")
    console.log(budget.summary())
    if budget.exhausted():
        console.log(budget.stop_notice())
    console.log("[generate] next: `namefinder tournament --source ideas` to see how they rank against the old pool.")
