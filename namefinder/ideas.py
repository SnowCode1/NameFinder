"""Stage 1 of idea-first naming: generate ideas, merge duplicates, then review them by hand.

An idea is one statement that is true of the product, plus the roots (Latin or Greek by default) that
could carry it into a name, for example "every copy has a parent" with stemma, propago, gens.
Ideas come at two levels: MECHANISM (how the product works) and PURPOSE (what it lets a journalist do
or understand: where a picture came from, where the trail goes cold). Ideas are generated per angle,
weighted toward the product's identity, then near-duplicates are merged (their root lists are united),
and only `approved` ideas feed name generation (`namefinder generate`).
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any

from rich import box
from rich.panel import Panel
from rich.table import Table

from . import budget, config, console, db, llm, net

DEFAULT_PER_ANGLE = 5
MAX_ROOTS = 12

# (name, description, weight). The weight scales --per-angle: central angles get more ideas, data-feature
# angles fewer. One request per angle keeps the idea set varied.
ANGLES: tuple[tuple[str, str, float], ...] = (
    ("origin", "where an image began: the first upload, the original capture, the camera, what came before every copy", 1.2),
    ("spread", "how a picture travels: every route from the first upload to a viral copy, reposts, shares, networks of sites", 1.0),
    ("evidence", "matches and their support: visual similarity, metadata, timestamps, archive captures, a record of who posted what", 1.0),
    ("discovery", "PURPOSE: finding what no single search shows: a crop of a crop, an edit of an edit, "
                  "the copy that came first, links a person would not notice checking one site at a time", 1.2),
    ("gaps", "PURPOSE: what the record does not yet show: where the trail goes cold or dates conflict, open questions, "
                 "copies with no known source, gaps in the archive, and helping people see them", 1.2),
    ("synthesis", "gathering two kinds of source (news archives and social platforms) into one reference: an index, a catalogue, an archive", 0.8),
    ("inquiry", "CORE FEATURE: asking the record in plain language: questions, queries, inquiry, an agent that answers "
                "by querying the matches, the act of investigating an image", 1.2),
    ("time", "MINOR: precedence (which copy came first). Dates are only a data field; "
             "propose an idea here only if it says something about the history of an image", 0.4),
    ("alteration", "MINOR: typed edits: crop, filter, splice, recaption; what changed between a copy and its parent", 0.4),
    ("exploration", "MINOR: seeing and navigating structure: a family tree of copies laid out and explored, a map of where an image went", 0.4),
)

DEFAULT_IDEAS_PROMPT = (
    "You are a brand-naming linguist preparing the raw material for names, before any name exists.\n"
    "Product: {product}\n\n"
    "Your job is to propose IDEAS: each is one short statement, plus the roots (Latin or Greek by default; another "
    "language only when it carries the idea markedly better) that could carry it into a coined word, in the manner "
    "of codex, lexicon, phoneme, genome, syntax. Ideas come at two levels, and both are welcome:\n"
    "- MECHANISM: something true of how the product works (matches, copies, edits, sources).\n"
    "- PURPOSE: something true of what the product is FOR: what a journalist can do or understand with it that "
    "they could not easily do before. Go further than the mechanics here. For example it can make visible what "
    "the record does not yet show (where the trail goes cold, where dates conflict, which copies have no known "
    "source), and so help people see what is not yet known about an image.\n"
    "Honesty: the tool does not rule on whether an image is real; it helps a person see, ask and notice. "
    "Phrase purposes as what it helps a person do, never as certainty, truth or breakthroughs.\n\n"
    "Rules:\n"
    "- Never frame an idea as catching fakes or issuing verdicts: this is not a deepfake detector or a "
    "lie detector.\n"
    "- Plain and technical, like a verification desk's notes. No hype, no marketing language.\n"
    "- Each idea must differ in SUBSTANCE (a different fact or purpose) from the others in your answer and from "
    "all the existing ideas you are shown, not merely in wording or angle. Two ideas that make the same point "
    "with different roots are one idea: put the extra roots in the same idea instead.\n"
    "- Give 3 to 6 roots per idea, each with its language and literal meaning. AT LEAST HALF must be FAMILIAR: an "
    "English word or stem a journalist already recognises at a glance (image, copy, frame, trace, trail, source, "
    "origin, match, record, mirror, lens, stem, archive, light), because the names built from them must be "
    "guessable without explanation. The rest may be classical roots (Latin or Greek), but only ones a reader knows "
    "through a common English word (stemma from stem, vestigium from vestige, imago from image, archivum); "
    "a root they would have to look up (hodos, fons, origo, rog-, pakhus, syndesm-) is a poor choice, however apt "
    "its meaning. Prefer short roots (1 to 2 syllables). Do not invent roots or guess at meanings you are unsure "
    "of."
)

IDEAS_USER = """Angle: {angle} ({desc})

Propose {n} ideas for this angle.
{existing}
Return ONLY a JSON array, no prose or fences, of objects in this form:
{{"idea": "one statement, at most 25 words", "roots": [{{"root": "stemma", "language": "Greek", "meaning": "garland; a family tree of copies"}}]}}"""

MERGE_SYSTEM = (
    "You curate a list of naming ideas for one product. Two ideas are DUPLICATES when they make the same point "
    "(the same underlying fact or purpose), even if worded differently or filed under different angles. Ideas "
    "that merely share a topic but make different points are NOT duplicates and stay separate."
)

MERGE_USER = """Ideas (id, angle, text, roots):

{ideas}

Group the duplicates. For each group of two or more, return:
{{"keep": <id of the clearest idea>, "merge": [<ids of the others>], "text": "the clearest single wording, at most 25 words"}}
Leave ideas that have no duplicate out of the answer. Return ONLY a JSON array (empty if there are no duplicates)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ideas_prompt() -> str:
    return config.load_prompt("ideas_prompt.md", DEFAULT_IDEAS_PROMPT).replace("{product}", config.PRODUCT_BLURB)


def _roots(raw: Any) -> list[dict[str, str]]:
    out = []
    for r in raw if isinstance(raw, list) else []:
        if isinstance(r, dict) and r.get("root"):
            out.append({k: str(r.get(k, "")).strip() for k in ("root", "language", "meaning")})
        elif isinstance(r, str) and r.strip():
            out.append({"root": r.strip(), "language": "", "meaning": ""})
    return out[:MAX_ROOTS]


def _root_key(root: str) -> str:
    return re.sub(r"[^a-z]", "", root.lower().split("/")[0].split("(")[0])


def _union_roots(*lists: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for lst in lists:
        for r in lst:
            k = _root_key(r.get("root", ""))
            if k and k not in seen:
                seen.add(k)
                out.append(r)
    return out[:MAX_ROOTS]


def _n_for(per_angle: int, weight: float) -> int:
    return max(1, round(per_angle * weight))


async def _propose(angles, per_angle, existing_text, url, key, model, system, concurrency, bar):
    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[tuple[str, list[dict[str, Any]]]] = []

    async with net.async_client(timeout=180.0) as http:
        async def one(angle: str, desc: str, weight: float) -> None:
            user = IDEAS_USER.format(angle=angle, desc=desc, n=_n_for(per_angle, weight), existing=existing_text)
            async with sem:
                parsed = await llm.chat_json(http, url, key, model, system, user,
                                             temperature=0.9, max_tokens=4000, label="ideas")
            items = [p for p in (parsed if isinstance(parsed, list) else []) if isinstance(p, dict) and p.get("idea")]
            results.append((angle, items))
            bar.update(1)

        await asyncio.gather(*(one(a, d, w) for a, d, w in angles))
    return results


def _existing_text(conn) -> str:
    """All ideas so far, across angles, so the model does not restate a point under another angle."""
    rows = [r for r in db.list_ideas(conn) if r["status"] != "merged"]
    if not rows:
        return ""
    kept = [r["text"] for r in rows if r["status"] != "rejected"][-60:]
    dropped = [r["text"] for r in rows if r["status"] == "rejected"][-20:]
    text = ""
    if kept:
        text += "Existing ideas, from every angle (do not repeat their point; find different ones):\n"
        text += "\n".join(f"- {t}" for t in kept) + "\n"
    if dropped:
        text += "Ideas already rejected by the reviewer (do not propose similar ones):\n"
        text += "\n".join(f"- {t}" for t in dropped) + "\n"
    return text


def generate(
    per_angle: int = DEFAULT_PER_ANGLE,
    angles: list[str] | None = None,
    model: str | None = None,
    backend: str | None = None,
    concurrency: int | None = None,
    merge_after: bool = True,
    db_path=None,
) -> None:
    url, key, model = llm.resolve(model, config.IDEAS_MODEL, backend)
    chosen = [a for a in ANGLES if not angles or a[0] in angles]
    if not chosen:
        raise SystemExit(f"unknown angle(s). Choose from: {', '.join(a[0] for a in ANGLES)}")
    system = ideas_prompt()
    with db.session(db_path) as conn:
        existing = _existing_text(conn)

    total = sum(_n_for(per_angle, w) for _, _, w in chosen)
    console.log(f"[ideas] model={model}  angles={len(chosen)}  ideas requested={total}")
    with console.RichBar("Generating ideas", total=len(chosen), unit="angle", style="magenta") as bar:
        results = asyncio.run(_propose(chosen, per_angle, existing, url, key, model, system,
                                       concurrency or config.SCORE_CONCURRENCY, bar))
    added = dupes = 0
    ts = _now()
    with db.session(db_path) as conn:
        for angle, items in results:
            for it in items:
                new_id = db.add_idea(conn, str(it["idea"]), _roots(it.get("roots")), angle, model, ts)
                if new_id is None:
                    dupes += 1
                else:
                    added += 1
    console.log(f"[ideas] added {added} new ideas ({dupes} exact duplicates skipped).")
    if merge_after and added:
        merge(model=model, backend=backend, db_path=db_path, announce=False)
    with db.session(db_path) as conn:
        pending = len(db.list_ideas(conn, "pending"))
    console.log(f"[ideas] {pending} awaiting review: run `namefinder ideas review`.")
    console.log(budget.summary())


# --- merging duplicates --------------------------------------------------------
def _fmt_for_merge(rows) -> str:
    lines = []
    for r in rows:
        roots = ", ".join(x["root"] for x in json.loads(r["roots"] or "[]"))
        lines.append(f"{r['id']} | {r['angle']} | {r['text']} | {roots}")
    return "\n".join(lines)


def merge(model: str | None = None, backend: str | None = None, db_path=None, announce: bool = True) -> int:
    """Merge pending ideas that repeat a point (their roots are united into the idea that is kept).

    Candidates are pending and approved ideas. A group is merged only if it has a pending member;
    when it contains an approved idea that one is kept, its wording untouched, and only pending
    members are folded in.
    """
    url, key, model = llm.resolve(model, config.IDEAS_MODEL, backend)
    with db.session(db_path) as conn:
        rows = [r for r in db.list_ideas(conn) if r["status"] in ("pending", "approved")]
    by_id = {r["id"]: r for r in rows}
    if sum(1 for r in rows if r["status"] == "pending") < 1 or len(rows) < 2:
        if announce:
            console.log("[ideas] nothing to merge.")
        return 0

    async def ask():
        async with net.async_client(timeout=180.0) as http:
            return await llm.chat_json(http, url, key, model, MERGE_SYSTEM,
                                       MERGE_USER.format(ideas=_fmt_for_merge(rows)),
                                       temperature=0.0, max_tokens=4000, label="ideas-merge")

    groups = asyncio.run(ask())
    used: set[int] = set()
    merged = 0
    with db.session(db_path) as conn:
        for g in groups if isinstance(groups, list) else []:
            try:
                ids = [int(g["keep"])] + [int(x) for x in g.get("merge", [])]
            except (KeyError, TypeError, ValueError):
                continue
            ids = [i for i in dict.fromkeys(ids) if i in by_id and i not in used]
            if len(ids) < 2 or not any(by_id[i]["status"] == "pending" for i in ids):
                continue
            approved = [i for i in ids if by_id[i]["status"] == "approved"]
            keeper = approved[0] if approved else ids[0]
            folded = [i for i in ids if i != keeper and by_id[i]["status"] == "pending"]
            if not folded:
                continue
            roots = _union_roots(json.loads(by_id[keeper]["roots"] or "[]"),
                                 *(json.loads(by_id[i]["roots"] or "[]") for i in folded))
            text = None if approved else (str(g.get("text") or "").strip() or None)
            db.set_idea(conn, keeper, text=text, roots=roots)
            for i in folded:
                db.set_idea(conn, i, status="merged")
            used.update(ids)
            merged += len(folded)
    console.log(f"[ideas] merged {merged} duplicate ideas into the ones kept (their roots were combined).")
    return merged


# --- display and review ------------------------------------------------------
def _roots_text(roots_json: str | None) -> str:
    try:
        roots = json.loads(roots_json or "[]")
    except ValueError:
        return ""
    parts = []
    for r in roots:
        meaning = f" ({r['meaning']})" if r.get("meaning") else ""
        lang = f" [dim]{r['language']}[/]" if r.get("language") else ""
        parts.append(f"[bold cyan]{r['root']}[/]{meaning}{lang}")
    return "   ".join(parts)


STATUS_STYLE = {"approved": "[bold green]approved[/]", "rejected": "[red]rejected[/]",
                "pending": "[yellow]pending[/]", "merged": "[dim]merged[/]"}


def show_list(status: str | None = None, db_path=None) -> None:
    with db.session(db_path) as conn:
        rows = db.list_ideas(conn, status)
        if not status:
            rows = [r for r in rows if r["status"] != "merged"]
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM ideas GROUP BY status")}
    if not rows:
        console.log("[ideas] none yet. Run `namefinder ideas generate`.")
        return
    t = Table(title="[bold cyan]Ideas[/]", box=box.ROUNDED, expand=True, header_style="bold cyan", border_style="dim",
              caption="  ·  ".join(f"{n} {s}" for s, n in sorted(counts.items())), caption_style="dim")
    t.add_column("ID", justify="right", style="bold cyan", no_wrap=True)
    t.add_column("Status", no_wrap=True)
    t.add_column("Angle", style="dim", no_wrap=True)
    t.add_column("Idea", ratio=2)
    t.add_column("Roots", ratio=3)
    for r in rows:
        t.add_row(str(r["id"]), STATUS_STYLE.get(r["status"], r["status"]), r["angle"] or "", r["text"],
                  _roots_text(r["roots"]))
    console._console.print(t)


def mark(approve: list[int], reject: list[int], db_path=None) -> None:
    with db.session(db_path) as conn:
        for i in approve:
            db.set_idea(conn, i, status="approved")
        for i in reject:
            db.set_idea(conn, i, status="rejected")
    console.log(f"[ideas] approved {len(approve)}, rejected {len(reject)}.")


def review(status: str = "pending", db_path=None) -> None:
    """Walk through ideas one at a time: approve, reject, edit, skip or quit."""
    import sys
    if not sys.stdin.isatty():
        raise SystemExit("`ideas review` needs an interactive terminal. Use `ideas list` and `ideas mark` instead.")
    with db.session(db_path) as conn:
        todo = db.list_ideas(conn, status)
    if not todo:
        console.log(f"[ideas] nothing {status}. Run `namefinder ideas generate` for more.")
        return
    c = console._console
    c.print(f"\n[bold cyan]{len(todo)} ideas to review.[/] "
            "[bold green]a[/]pprove  [red]r[/]eject  [yellow]e[/]dit text  [dim]s[/]kip  [dim]q[/]uit\n")
    approved = rejected = 0
    for n, row in enumerate(todo, 1):
        body = f"[bold white]{row['text']}[/]\n\n{_roots_text(row['roots'])}"
        c.print(Panel(body, title=f"[bold cyan]{n}/{len(todo)}[/]  [dim]#{row['id']} · {row['angle']}[/]",
                      border_style="cyan", padding=(1, 2)))
        while True:
            try:
                choice = input("  [a/r/e/s/q] > ").strip().lower()[:1]
            except (EOFError, KeyboardInterrupt):
                choice = "q"
            if choice == "e":
                try:
                    new = input("  new text > ").strip()
                except (EOFError, KeyboardInterrupt):
                    new = ""
                if new:
                    with db.session(db_path) as conn:
                        db.set_idea(conn, row["id"], text=new)
                    c.print(f"  [dim]updated.[/] Now: [bold white]{new}[/]")
                continue
            if choice in ("a", "r", "s", "q"):
                break
        if choice == "q":
            break
        if choice == "a":
            with db.session(db_path) as conn:
                db.set_idea(conn, row["id"], status="approved")
            approved += 1
        elif choice == "r":
            with db.session(db_path) as conn:
                db.set_idea(conn, row["id"], status="rejected")
            rejected += 1
    with db.session(db_path) as conn:
        total_ok = len(db.list_ideas(conn, "approved"))
    c.print(f"\n[bold]Review done:[/] approved {approved}, rejected {rejected}. {total_ok} approved ideas in total. "
            "Next: `namefinder generate`.")
