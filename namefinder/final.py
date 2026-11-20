"""Stage 5 - final LLM tagging and the shortlist report.

At most `OPENROUTER_BATCH` words per call and `MAX_FINAL` words overall, so the
whole stage costs 1-2 requests against OpenRouter's free daily limit.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime, timezone
from typing import Any

import httpx
from tqdm import tqdm

from . import config, db, net

DEFAULT_FINAL_SYSTEM_PROMPT = (
    "You are an expert brand naming strategist for modern technology products.\n"
    "Product: A fast, reliable software tool designed for clarity, deep insight, and quiet intellectual utility.\n"
    "Target aesthetic: calm, formidable, quiet utility — like Obsidian, Stripe, Linear, or Raycast. No corporate hype, no cheesy clichés.\n"
    "THE NARRATIVE BRIDGE RULE: Reward names with an intuitive conceptual bridge to clarity, structure, focus, depth, or logical precision. "
    "Penalize disjointed or overly generic metaphors."
)

DEFAULT_FINAL_USER_TEMPLATE = """Score each candidate name for this software product.

Candidates: {words}

Return a JSON array, one object per candidate, in the same order, with exactly these keys:
  "word": the candidate, unchanged
  "fit": 1-5, how strong is the intuitive conceptual bridge to clarity, structure, focus, or utility
  "memorability": 1-5, how easily it is remembered and repeated after one hearing
  "why": at most 12 words on the conceptual bridge (or why it makes no sense)
  "risk": at most 8 words on the main brand risk, or "" if none

Return only the JSON array. No prose, no code fences."""

SYSTEM_PROMPT = config.load_prompt("final_system_prompt.md", DEFAULT_FINAL_SYSTEM_PROMPT)
USER_TEMPLATE = config.load_prompt("final_user_prompt.md", DEFAULT_FINAL_USER_TEMPLATE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(content: str) -> list[dict[str, Any]] | None:
    text = content.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        return None
    try:
        data = json.loads(text[start: end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    return [d for d in data if isinstance(d, dict) and d.get("word")]


def call_openrouter(http: httpx.Client, words: list[str], model: str) -> list[dict[str, Any]] | None:
    key = config.secret(config.OPENROUTER_KEY_ENV)
    if not key:
        raise SystemExit(f"[rank-final] {config.OPENROUTER_KEY_ENV} is not set. {config.WRITE_KEY_HINT}")

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_TEMPLATE.format(words=", ".join(words))},
        ],
        "temperature": 0.2,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    for attempt in range(2):
        try:
            r = http.post(
                f"{config.OPENROUTER_URL}/chat/completions",
                json=body,
                headers=headers,
                timeout=httpx.Timeout(120.0, connect=15.0),
            )
        except httpx.HTTPError as exc:
            print(f"[rank-final] request failed: {exc.__class__.__name__}", file=sys.stderr)
            return None
        if r.status_code == 429:
            print("[rank-final] OpenRouter rate limit hit; stopping (rerun tomorrow)", file=sys.stderr)
            return None
        if r.status_code != 200:
            print(f"[rank-final] OpenRouter HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return None
        try:
            content = r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError):
            print("[rank-final] unexpected response shape", file=sys.stderr)
            return None
        parsed = _parse(content)
        if parsed:
            return parsed
        print("[rank-final] could not parse JSON; retrying once with a firmer instruction", file=sys.stderr)
        body["messages"][-1]["content"] += "\n\nReturn ONLY the JSON array."
    return None


def _domain_cell(conn, word: str) -> str:
    rows = conn.execute(
        "SELECT tld, status FROM active_domains WHERE word = ? ORDER BY tld", (word,)
    ).fetchall()
    label = {"available": "FREE", "registered": "taken", "unknown": "?"}
    return " ".join(f"{r['tld']}={label.get(r['status'], r['status'])}" for r in rows) or "-"


def _mark_cell(conn, word: str) -> str:
    rows = conn.execute(
        "SELECT verdict, highest_risk FROM mark_checks WHERE word = ?", (word,)
    ).fetchall()
    if not rows:
        return "not screened"
    return " ".join(f"{r['verdict']}{'/' + r['highest_risk'] if r['highest_risk'] else ''}" for r in rows)


def write_reports(rows: list[dict[str, Any]], db_path=None) -> tuple[str, str]:
    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    md_path = config.RESULTS_DIR / "shortlist.md"
    csv_path = config.RESULTS_DIR / "shortlist.csv"

    with db.session(db_path) as conn:
        enriched = []
        for r in rows:
            word = r["word"]
            sc = conn.execute(
                "SELECT * FROM scores WHERE word = ? AND qversion = ?",
                (word, config.QUESTION_VERSION),
            ).fetchone()
            enriched.append({
                **r,
                "composite": sc["composite"] if (sc and sc["composite"] is not None) else r.get("composite"),
                "score_fit": sc["fit"] if sc else None,
                "score_natural": sc["natural"] if sc else None,
                "score_spellable": sc["spellable"] if sc else None,
                "domains": _domain_cell(conn, word),
                "marks": _mark_cell(conn, word),
            })

    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "word", "llm_fit", "memorability", "composite", "score_fit", "score_natural",
            "score_spellable", "domains", "marks", "why", "risk",
        ])
        w.writeheader()
        for r in enriched:
            w.writerow({
                "word": r["word"],
                "llm_fit": r.get("fit"),
                "memorability": r.get("memorability"),
                "composite": r.get("composite"),
                "score_fit": r.get("score_fit"),
                "score_natural": r.get("score_natural"),
                "score_spellable": r.get("score_spellable"),
                "domains": r["domains"],
                "marks": r["marks"],
                "why": r.get("why"),
                "risk": r.get("risk"),
            })

    lines = [
        "# Brand name shortlist",
        "",
        f"Generated {_now()} by NameFinder.",
        "",
        "Product: high-speed image provenance engine + LLM agent for finding where a picture came from and tracing how it spread.",
        "",
        f"- Words screened end to end: {len(enriched)}",
        f"- OpenRouter model: `{rows[0].get('model', config.OPENROUTER_MODEL) if rows else config.OPENROUTER_MODEL}`",
        f"- Scoring version: `{config.QUESTION_VERSION}`",
        "",
        "| # | Name | LLM fit | Mem. | Score | Domains | Trademark | Why it works | Risk |",
        "|---|------|---------|------|-------|---------|-----------|--------------|------|",
    ]
    for i, r in enumerate(enriched, 1):
        fit_str = str(r["fit"]) if r.get("fit") is not None else "-"
        mem_str = str(r["memorability"]) if r.get("memorability") is not None else "-"
        comp_str = f"{r['composite']:.3f}" if r.get("composite") is not None else "-"
        lines.append(
            "| {i} | **{word}** | {fit} | {mem} | {comp} | {dom} | {marks} | {why} | {risk} |".format(
                i=i,
                word=r["word"],
                fit=fit_str,
                mem=mem_str,
                comp=comp_str,
                dom=r["domains"],
                marks=r["marks"],
                why=(r.get("why") or "").replace("|", "/"),
                risk=(r.get("risk") or "").replace("|", "/"),
            )
        )

    lines += [
        "",
        "## How to read this",
        "",
        "- **Domains** — `FREE` came back 404 from RDAP, `taken` 200. Verified live at generation time.",
        "- **Trademark** — `clear` means no live or pending USPTO mark in classes 9/42/35/41 at medium or high similarity; `watch` means an identical word exists in another class, or a low-similarity mark in ours; `conflict` should not appear here.",
        "- **Jev** — the composite over fit, naturalness, spellability, memorability and novelty (see `namefinder/config.py`).",
        "",
        "This is a screening pass, not legal advice. Before filing, have a trademark attorney run a full",
        "clearance search, and confirm domains through a registrar at purchase time.",
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return str(md_path), str(csv_path)


def run(limit: int | None = None, refresh: bool = False, model: str | None = None,
        db_path=None, skip_llm: bool = False) -> None:
    qversion = config.QUESTION_VERSION
    model = model or config.OPENROUTER_MODEL
    cap = limit or config.MAX_FINAL

    with db.session(db_path) as conn:
        survivors = db.words_passing_trademarks(conn, qversion, limit=cap)
        words = [r["word"] for r in survivors]

    if not words:
        raise SystemExit("[rank-final] no fully clean survivors - run `check-trademarks` first")

    print(f"[rank-final] tagging {len(words)} survivors with {model} (skip-llm={skip_llm})")

    rows: list[dict[str, Any]] = [
        {"word": r["word"], "model": model, "composite": r["composite"]}
        for r in survivors
    ]
    if not skip_llm:
        with db.session(db_path) as conn:
            cached = {
                r["word"]: r for r in conn.execute(
                    "SELECT * FROM final_tags WHERE model = ?", (model,)
                )
            }
        pending = [w for w in words if refresh or w not in cached]
        calls = 0
        with net.client(timeout=120.0) as http, db.session(db_path) as conn:
            with tqdm(total=len(pending), desc="LLM tagging", unit="word", ncols=80) as pbar:
                for i in range(0, len(pending), config.OPENROUTER_BATCH):
                    chunk = pending[i: i + config.OPENROUTER_BATCH]
                    calls += 1
                    tagged = call_openrouter(http, chunk, model)
                    pbar.update(len(chunk))
                    if not tagged:
                        tqdm.write(f"[rank-final] batch {calls} failed; keeping {len(chunk)} words untagged", file=sys.stderr)
                        continue
                    by_word = {str(t["word"]).lower(): t for t in tagged}
                    for word in chunk:
                        t = by_word.get(word.lower())
                        if not t:
                            continue
                        db.upsert_final_tag(conn, {
                            "word": word,
                            "model": model,
                            "reason": str(t.get("why", ""))[:300],
                            "memorability": _as_int(t.get("memorability")),
                            "fit": _as_int(t.get("fit")),
                            "raw": db.dump_json(t),
                            "tagged_at": _now(),
                        })
                    conn.commit()

    with db.session(db_path) as conn:
        for row in rows:
            t = conn.execute(
                "SELECT * FROM final_tags WHERE word = ? AND model = ?", (row["word"], model)
            ).fetchone()
            if t:
                row.update({
                    "fit": t["fit"], "memorability": t["memorability"],
                    "why": t["reason"], "model": t["model"],
                })
                raw = json.loads(t["raw"] or "{}")
                row["risk"] = raw.get("risk", "")

    rows.sort(key=lambda r: (
        -(r.get("fit") or 0),
        -(r.get("memorability") or 0),
        -(r.get("composite") or 0),
    ))
    md, csv_path = write_reports(rows, db_path)
    print(f"[rank-final] wrote {md}\n[rank-final] wrote {csv_path}")


def _as_int(value: Any) -> int | None:
    try:
        return max(0, min(5, int(float(value))))
    except (TypeError, ValueError):
        return None
