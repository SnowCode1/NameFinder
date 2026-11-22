"""Stage 3 of idea-first naming: improve the current leaders using the judge's own critique.

For each of the best-rated names, the model is shown what the tournament judge found wrong with it and
asked for variants that keep what works (the root, the idea) but fix that flaw. Variants are registered
with engine='variant' and remember their parent, then enter the next tournament with `--source ideas`.
Run it again after each tournament: generate -> tournament -> evolve -> tournament ... until the top
stops changing.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any, Sequence

from . import budget, config, console, db, ideanames, llm, net, tournament

DEFAULT_VARIANTS = 4
PARENTS_PER_REQUEST = 4

DEFAULT_EVOLVE_PROMPT = (
    "You are a brand-naming linguist improving names for an image provenance product.\n"
    "Product: {product}\n\n"
    "You will be given names that a judge has ranked among the best so far, each with the flaws the judge "
    "named. For every name, invent variants that KEEP WHAT WORKS (its root and the idea it carries) and FIX THE "
    "STATED FLAWS. A variant is a real redesign: change the ending, swap the prefix, shorten or lengthen the root, "
    "reorder the parts, or choose a sibling root with the same meaning. Changing a single letter is not a variant "
    "and will be thrown away.\n"
    "Judge the flaws honestly: if a flaw cannot be fixed without losing what makes the name good, say so by "
    "returning fewer variants.\n\n"
    + ideanames.NAME_RULES
)

EVOLVE_USER = """Improve these names. For each, the judge's current rating and the flaws it found:

{parents}

Invent up to {n} variants per name.
Names already in play (do not repeat them): {taken}
For every variant add "change": at most 14 words on what you changed and which flaw it fixes.
Return ONLY a JSON array, no prose or fences, one object per name, in order:
{{"parent": "<name>", "variants": [{{"name": "...", "change": "..."}}]}}"""


def evolve_prompt(max_len: int = 10) -> str:
    text = config.load_prompt("evolve_prompt.md", DEFAULT_EVOLVE_PROMPT)
    return text.replace("{product}", config.PRODUCT_BLURB).replace("{max_len}", str(max_len))


def pick_parents(conn, judge: str, top: int, include_refs: bool, refs: Sequence[str]) -> list[dict[str, Any]]:
    rated = db.tournament_ratings(conn, judge)
    flaws = db.tournament_flaws(conn, judge, n=3)
    origins = {r["word"]: r for r in conn.execute("SELECT * FROM name_origins")}
    out = []
    for w in sorted(rated, key=lambda x: rated[x]["rating"], reverse=True):
        if not include_refs and w in refs:
            continue
        o = origins.get(w)
        out.append({
            "word": w, "rating": rated[w]["rating"], "flaws": flaws.get(w, []),
            "built": (o["construction"] if o else "") or "",
        })
        if len(out) >= top:
            break
    return out


async def _propose(parents, n, taken, url, key, model, system, concurrency, bar):
    sem = asyncio.Semaphore(max(1, concurrency))
    out: list[ideanames.Candidate] = []
    chunks = [parents[i : i + PARENTS_PER_REQUEST] for i in range(0, len(parents), PARENTS_PER_REQUEST)]

    async with net.async_client(timeout=180.0) as http:
        async def one(chunk: list[dict[str, Any]]) -> None:
            lines = []
            for p in chunk:
                flaw = "; ".join(p["flaws"]) if p["flaws"] else "none named (find its weakest point yourself)"
                built = f"  [built: {p['built']}]" if p["built"] else ""
                lines.append(f"- {p['word']} (rating {p['rating']:.0f}){built}  flaws: {flaw}")
            user = EVOLVE_USER.format(parents="\n".join(lines), n=n, taken=", ".join(taken) or "(none)")
            async with sem:
                parsed = await llm.chat_json(http, url, key, model, system, user,
                                             temperature=config.COIN_LLM_TEMPERATURE, max_tokens=4000, label="evolve")
            names = {p["word"] for p in chunk}
            for item in parsed if isinstance(parsed, list) else []:
                if not isinstance(item, dict):
                    continue
                parent = str(item.get("parent", "")).strip().lower()
                if parent not in names:
                    continue
                for v in item.get("variants") or []:
                    if not isinstance(v, dict):
                        continue
                    w = ideanames._clean(v.get("name"))
                    if w:
                        out.append(ideanames.Candidate(word=w, idea_id=None, parent_word=parent,
                                                       construction=ideanames.clean_note(str(v.get("change") or ""))))
            bar.update(1)
            bar.note(f"[magenta]{len(out)} variants[/]")

        await asyncio.gather(*(one(c) for c in chunks))
    return out


def run(
    top: int = 20,
    variants: int = DEFAULT_VARIANTS,
    include_refs: bool = True,
    model: str | None = None,
    backend: str | None = None,
    concurrency: int | None = None,
    min_len: int = 5,
    max_len: int = 10,
    check: bool = True,
    judge_model: str | None = None,
    all_tlds: bool = False,
    db_path=None,
) -> None:
    url, key, model = llm.resolve(model, config.NAMEGEN_MODEL, backend)
    refs = tournament.load_refs()
    with db.session(db_path) as conn:
        judge, note = tournament.resolve_judge(conn, judge_model)
        if not judge:
            console.log(note or "[evolve] no tournament ratings yet. Run `namefinder tournament` first.")
            return
        parents = pick_parents(conn, judge, top, include_refs, refs)
        taken = [r["word"] for r in conn.execute(
            "SELECT word FROM tournament_ratings WHERE judge = ? ORDER BY rating DESC LIMIT 60", (judge,))]
    if note:
        console.log(f"[evolve] note: {note}")
    if not parents:
        console.log("[evolve] no rated names to improve.")
        return

    system = evolve_prompt(max_len)
    console.log(f"[evolve] model={model}  judge={judge}  parents={len(parents)}  variants per name={variants}")
    with console.RichBar("Evolving names", total=-(-len(parents) // PARENTS_PER_REQUEST),
                         unit="request", style="magenta") as bar:
        raw = asyncio.run(_propose(parents, variants, taken, url, key, model, system,
                                   concurrency or config.SCORE_CONCURRENCY, bar))
    random.shuffle(raw)
    accepted, stats = ideanames.register(raw, "variant", "variant", db_path, min_len, max_len,
                                         stem_cap_group=4, stem_cap_batch=8)
    console.log(f"[evolve] {len(raw)} proposed, {len(accepted)} kept"
                + ("; dropped: " + ", ".join(f"{n} {why}" for why, n in stats.most_common()) if stats else ""))
    if not accepted:
        console.log(budget.summary())
        return

    words = [c["word"] for c in accepted]
    if check:
        counts = ideanames.check_domains(words, db_path, config.active_domain_roles() if all_tlds else None)
        console.log(f"[domains] {counts.get('available', 0)} free, {counts.get('registered', 0)} taken")
    free = ideanames.free_com(words, "com", db_path) if check else set()
    ideanames.show_new_names(accepted, free, "New variants (free .com)", show_parent=True)
    console.log(budget.summary())
    if budget.exhausted():
        console.log(budget.stop_notice())
    console.log("[evolve] next: `namefinder tournament --source ideas` to rate the variants against their parents.")
