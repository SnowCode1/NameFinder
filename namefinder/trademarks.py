"""Stage 4 - trademark and unregistered-AI-tool screening.

Four cheap, mostly keyless checks on the Stage 3 survivors:

1. Markbase (USPTO) clearance, generously interpreted: live *and* pending marks
   whose Nice classes touch software / SaaS / data / education count, and the
   goods-and-services text is kept so a human can read it.
2. IP Australia IPGOD bulk CSVs, if the user dropped them in ./data/ipgod/.
3. PyPI package-name collision (unregistered tool check).
4. GitHub repo names mentioning the word next to AI (unregistered agent check).
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Iterator

import httpx
from tqdm import tqdm

from . import config, db, net

NORM_RE = re.compile(r"[^a-z0-9]")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm(text: str) -> str:
    return NORM_RE.sub("", (text or "").lower())


# ---------------------------------------------------------------- USPTO ----
def status_liveness(code: str | None) -> str:
    """Map a USPTO/TSDR status code to live | pending | dead | unknown."""
    c = (code or "").strip()
    if not c:
        return "unknown"
    if c in config.DEAD_STATUS_CODES or c.startswith(config.DEAD_STATUS_PREFIXES):
        return "dead"
    if c.startswith("8") or c.startswith("7"):
        return "live"
    if c[0] in "12345":
        return "pending"
    return "unknown"


def _nice_overlap(codes: Iterable[str] | None, wanted: tuple[str, ...]) -> bool:
    got = {str(c).strip().zfill(3) for c in (codes or [])}
    return bool(got & set(wanted))


def _headers() -> dict[str, str]:
    key = config.secret(config.MARKBASE_KEY_ENV)
    return {"Authorization": f"Bearer {key}"} if key else {}


def _judge_markbase(payload: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    """Decide clear / watch / conflict from one /clearance payload.

    Truth table (see README): a mark blocks the name only when it is live or
    pending AND its Nice classes overlap ours AND Markbase rates the similarity
    medium or high. An identical word mark in an unrelated class is a "watch"
    (a real brand-squatting risk), never silently treated as clear.
    """
    matches = payload.get("matches") or []
    hits: list[dict[str, Any]] = []
    worst = "none"
    order = {"none": 0, "low": 1, "medium": 2, "high": 3}
    conflict = False
    watch = False

    for m in matches:
        state = status_liveness(m.get("status_code"))
        if state == "dead":
            continue
        risk = (m.get("risk") or "low").lower()
        similarity = m.get("similarity")
        exact = bool((m.get("signals") or {}).get("exact")) or (
            similarity is not None and float(similarity) >= 0.999
        )
        inclass = _nice_overlap(m.get("international_codes"), config.NICE_CLASSES_PADDED)
        codes = m.get("international_codes") or []

        hit = {
            "serial": str(m.get("serial_number") or ""),
            "mark": m.get("word_mark"),
            "owner": m.get("owner_name"),
            "status_code": m.get("status_code"),
            "live": 1,
            "nice": ",".join(codes),
            "similarity": float(similarity) if similarity is not None else None,
            "risk": risk,
            "goods": None,
            "url": (
                "https://tsdr.uspto.gov/#caseNumber="
                f"{m.get('serial_number')}&caseType=SERIAL_NO&searchType=statusSearch"
            ),
        }
        hits.append(hit)
        if order.get(risk, 1) > order.get(worst, 0):
            worst = risk
        if exact and order.get(worst, 0) < 3:
            worst = "high"

        if inclass and risk in config.MARKBASE_RISK_BLOCK:
            conflict = True
        elif inclass or exact:
            watch = True

    if conflict:
        return "conflict", worst, hits
    if watch:
        return "watch", worst, hits
    return "clear", worst, hits


def check_markbase(words: list[str], refresh: bool = False, db_path=None) -> None:
    """Screen words against USPTO marks via Markbase (keyless by default).

    The class filter is applied here in code on all four classes at once, so a
    single name costs one unit of the free monthly quota (not four).
    """
    scope = "uspto:" + ",".join(config.NICE_CLASSES)
    key = config.secret(config.MARKBASE_KEY_ENV)
    mode = "batch" if key else "single"

    with db.session(db_path) as conn:
        cutoff = None if refresh else (
            datetime.now(timezone.utc) - timedelta(days=config.TRADEMARK_STALE_DAYS)
        ).isoformat(timespec="seconds")
        cached = set()
        if cutoff:
            cached = {
                r["word"]
                for r in conn.execute(
                    "SELECT word FROM mark_checks WHERE scope = ? AND checked_at >= ? AND verdict != 'error'",
                    (scope, cutoff),
                )
            }
    todo = [w for w in words if w not in cached]
    print(f"[check-trademarks] USPTO via Markbase ({mode}, classes {','.join(config.NICE_CLASSES)}): "
          f"{len(todo)} to check, {len(words) - len(todo)} cached")
    if not todo:
        return

    budget = config.MARKBASE_MONTHLY_BUDGET
    if len(todo) > budget:
        print(f"[check-trademarks] WARNING: {len(todo)} names exceeds the free monthly budget "
              f"of {budget}; stopping at {budget}. Use --limit to stay inside it.", file=sys.stderr)
        todo = todo[:budget]

    pacer = net.SyncPacer(config.MARKBASE_RATE_PER_MIN / 60.0)
    headers = _headers()
    processed = 0

    with net.client(timeout=45.0) as http, db.session(db_path) as conn:
        idx = 0
        chunk_size = 25 if mode == "batch" else 1
        with tqdm(total=len(todo), desc="USPTO clearance", unit="name", ncols=80) as pbar:
            while idx < len(todo):
                chunk = todo[idx: idx + chunk_size]
                idx += len(chunk)
                payloads: dict[str, dict[str, Any]] = {}

                if mode == "batch":
                    pacer.wait()
                    try:
                        r = http.post(
                            f"{config.MARKBASE_URL}/clearance/batch",
                            json={"marks": chunk, "limit": config.MARKBASE_MATCH_LIMIT},
                            headers=headers,
                        )
                    except httpx.HTTPError as exc:
                        tqdm.write(f"[check-trademarks] batch error {exc.__class__.__name__}", file=sys.stderr)
                        r = None
                    if r is not None and r.status_code == 200:
                        for item in r.json().get("results", []):
                            payloads[item.get("mark", "")] = item
                    elif r is not None and r.status_code in (401, 403, 404):
                        tqdm.write("[check-trademarks] batch endpoint unavailable for this key; using single clearance calls", file=sys.stderr)
                        mode = "single"
                        chunk_size = 1
                        idx -= len(chunk)
                        continue
                    elif r is not None and r.status_code == 429:
                        time.sleep(net.backoff_delay(1, r.headers.get("retry-after")))
                        idx -= len(chunk)
                        continue

                if not payloads:
                    for word in chunk:
                        status, payload = net.get_json(
                            http,
                            f"{config.MARKBASE_URL}/clearance",
                            params={"mark": word, "limit": config.MARKBASE_MATCH_LIMIT},
                            headers=headers,
                            pacer=pacer,
                        )
                        if status == 200 and isinstance(payload, dict):
                            payloads[word] = payload
                        else:
                            payloads[word] = {"_status": status}

                for word in chunk:
                    payload = payloads.get(word) or {"_status": 0}
                    if "_status" in payload:
                        db.upsert_mark_check(conn, {
                            "word": word, "scope": scope, "verdict": "error",
                            "highest_risk": None, "n_hits": 0, "checked_at": _now(),
                        })
                    else:
                        verdict, worst, hits = _judge_markbase(payload)
                        db.upsert_mark_check(conn, {
                            "word": word, "scope": scope, "verdict": verdict,
                            "highest_risk": worst, "n_hits": len(hits), "checked_at": _now(),
                        })
                        db.replace_mark_hits(conn, word, scope, hits)
                    processed += 1
                    pbar.update(1)
                conn.commit()

    print(f"[check-trademarks] USPTO done: {processed} names screened")


# ------------------------------------------------------- IP Australia ----
IPGOD_ALIASES = {
    "mark": ("word_mark", "trademark", "mark", "tm_text", "mark_text", "word"),
    "nice": ("nice_class", "class", "nice", "classes", "tmd_class"),
    "status": ("status", "status_desc", "tm_status", "mark_status", "state"),
    "owner": ("owner", "owner_name", "applicant", "holder", "trademark_owner"),
}


def _pick(fieldnames: list[str], aliases: tuple[str, ...]) -> str | None:
    lowered = {f.lower().strip(): f for f in fieldnames if f}
    for a in aliases:
        if a in lowered:
            return lowered[a]
    for a in aliases:
        for low, orig in lowered.items():
            if a in low:
                return orig
    return None


def index_ipgod(db_path=None, force: bool = False) -> int:
    """Index active IPGOD marks in the target Nice classes. No-op if empty."""
    folder = config.IPGOD_DIR
    files = sorted([p for p in folder.glob("*.csv")]) if folder.exists() else []
    if not files:
        print(f"[check-trademarks] no IPGOD CSVs in {folder} - skipping (IP Australia check disabled)")
        return 0

    with db.session(db_path) as conn:
        signature = json.dumps([[f.name, int(f.stat().st_mtime)] for f in files])
        if not force and db.get_meta(conn, "ipgod_signature") == signature and \
                conn.execute("SELECT COUNT(*) c FROM ipgod_marks").fetchone()["c"] > 0:
            n = conn.execute("SELECT COUNT(*) c FROM ipgod_marks").fetchone()["c"]
            print(f"[check-trademarks] IPGOD already indexed ({n} marks)")
            return n

        conn.execute("DELETE FROM ipgod_marks")
        total = 0
        wanted = set(config.NICE_CLASSES)
        for path in files:
            try:
                with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
                    reader = csv.DictReader(fh)
                    if not reader.fieldnames:
                        continue
                    cols = {k: _pick(list(reader.fieldnames), v) for k, v in IPGOD_ALIASES.items()}
                    if not cols["mark"]:
                        print(f"[check-trademarks] {path.name}: no mark column, skipped", file=sys.stderr)
                        continue
                    for row in reader:
                        mark = row.get(cols["mark"]) or ""
                        nice_raw = row.get(cols["nice"] or "") or ""
                        classes = {c.strip().lstrip("0") or "0" for c in re.split(r"[^0-9]+", nice_raw) if c.strip()}
                        if wanted and not (classes & wanted):
                            continue
                        status = (row.get(cols["status"] or "") or "").lower()
                        if any(bad in status for bad in ("expired", "removed", "ceased", "refused", "withdrawn")):
                            continue
                        conn.execute(
                            "INSERT INTO ipgod_marks(mark_norm, mark, nice, status, owner, source) VALUES(?,?,?,?,?,?)",
                            (norm(mark), mark, nice_raw, status, row.get(cols["owner"] or ""), path.name),
                        )
                        total += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[check-trademarks] {path.name}: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        conn.commit()
        db.set_meta(conn, "ipgod_signature", signature)
    print(f"[check-trademarks] IPGOD indexed {total} active marks in classes {sorted(wanted)}")
    return total


def check_ipgod(words: list[str], db_path=None) -> dict[str, dict[str, Any]]:
    """Local exact/fuzzy match against indexed IP Australia marks."""
    out: dict[str, dict[str, Any]] = {}
    with db.session(db_path) as conn:
        if conn.execute("SELECT COUNT(*) c FROM ipgod_marks").fetchone()["c"] == 0:
            return out
        for word in words:
            key = norm(word)
            exact = conn.execute("SELECT * FROM ipgod_marks WHERE mark_norm = ? LIMIT 5", (key,)).fetchall()
            near = []
            if not exact:
                for cand in conn.execute(
                    "SELECT DISTINCT mark_norm FROM ipgod_marks WHERE mark_norm LIKE ? LIMIT 400",
                    (key[:3] + "%",),
                ):
                    ratio = SequenceMatcher(None, key, cand["mark_norm"]).ratio()
                    if ratio >= 0.86:
                        near.append({"mark_norm": cand["mark_norm"], "ratio": round(ratio, 3)})
            if exact:
                out[word] = {"verdict": "conflict", "hits": [dict(r) for r in exact]}
            elif near:
                out[word] = {"verdict": "watch", "hits": near}
            else:
                out[word] = {"verdict": "clear", "hits": []}
    return out


# ------------------------------------------------ PyPI and GitHub ----
def check_pypi(http: httpx.Client, word: str) -> dict[str, Any]:
    status, payload = net.get_json(http, f"https://pypi.org/pypi/{word}/json", attempts=2)
    if status == 200:
        info = (payload or {}).get("info", {})
        return {
            "word": word, "source": "pypi", "verdict": "taken", "n_hits": 1,
            "top": f'{payload.get("info", {}).get("name")} {info.get("version", "")}'.strip(),
            "checked_at": _now(),
        }
    if status == 404:
        return {"word": word, "source": "pypi", "verdict": "clear", "n_hits": 0, "top": None, "checked_at": _now()}
    return {"word": word, "source": "pypi", "verdict": "error", "n_hits": 0, "top": f"http {status}", "checked_at": _now()}


def check_github(http: httpx.Client, word: str) -> dict[str, Any]:
    headers = {}
    token = config.secret(config.GITHUB_TOKEN_ENV)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, payload = net.get_json(
        http,
        "https://api.github.com/search/repositories",
        params={"q": f"{word} AI in:name", "sort": "stars", "per_page": 5},
        headers=headers,
        attempts=2,
    )
    if status != 200 or not isinstance(payload, dict):
        return {"word": word, "source": "github", "verdict": "error", "n_hits": 0,
                "top": f"http {status}", "checked_at": _now()}

    items = payload.get("items") or []
    hits = [i for i in items if word.lower() in (i.get("name") or "").lower()]
    exact = [i for i in hits if norm(i.get("name") or "") == norm(word)]
    strong = [i for i in hits if (i.get("stargazers_count") or 0) >= config.GITHUB_MIN_STARS]
    top = None
    if items:
        best = max(items, key=lambda i: i.get("stargazers_count") or 0)
        top = f'{best.get("full_name")} ({best.get("stargazers_count")} stars)'
    if exact or strong:
        verdict = "taken"
    elif hits:
        verdict = "watch"
    else:
        verdict = "clear"
    return {"word": word, "source": "github", "verdict": verdict,
            "n_hits": len(hits) or payload.get("total_count", 0), "top": top, "checked_at": _now()}


def run(limit: int | None = None, refresh: bool = False, db_path=None,
        do_markbase: bool = True, do_ipgod: bool = True,
        do_pypi: bool = True, do_github: bool = True, skip_repos: bool = False,
        top_n: int | None = None, words: list[str] | None = None) -> None:
    """Screen `words` if given (for example the tournament leaders); else the old pipeline's survivors."""
    qversion = config.QUESTION_VERSION
    explicit = bool(words)
    if not explicit:
        with db.session(db_path) as conn:
            rows = db.words_passing_domains(conn, qversion, limit=top_n)
            words = [r["word"] for r in rows][: limit or None]

    if not words:
        raise SystemExit("[check-trademarks] no Stage 3 survivors with available domains - run `check-domains` first")

    print(f"[check-trademarks] screening {len(words)} words"
          + ("" if explicit else " that have a free domain"))

    if do_ipgod:
        index_ipgod(db_path)
        ipgod = check_ipgod(words, db_path)
        if ipgod:
            with db.session(db_path) as conn:
                for word, res in ipgod.items():
                    db.upsert_mark_check(conn, {
                        "word": word, "scope": "au:ipgod", "verdict": res["verdict"],
                        "highest_risk": "exact" if res["verdict"] == "conflict" else None,
                        "n_hits": len(res["hits"]), "checked_at": _now(),
                    })
            counts: dict[str, int] = {}
            for res in ipgod.values():
                counts[res["verdict"]] = counts.get(res["verdict"], 0) + 1
            print(f"[check-trademarks] IPGOD local matches: {counts}")

    if do_markbase:
        check_markbase(words, refresh=refresh, db_path=db_path)

    if do_pypi or do_github:
        with db.session(db_path) as conn:
            done = {(r["word"], r["source"]) for r in conn.execute(
                "SELECT word, source FROM repo_checks WHERE checked_at >= ?",
                ((datetime.now(timezone.utc) - timedelta(days=config.TRADEMARK_STALE_DAYS)).isoformat(timespec="seconds"),),
            )}
        todo = [w for w in words if (w, "pypi") not in done or (w, "github") not in done]
        print(f"[check-trademarks] PyPI/GitHub: {len(todo)} words to screen")
        with net.client(timeout=25.0) as http, db.session(db_path) as conn:
            with tqdm(total=len(todo), desc="PyPI/GitHub check", unit="word", ncols=80) as pbar:
                for i, word in enumerate(todo, 1):
                    if do_pypi:
                        db.upsert_repo_check(conn, check_pypi(http, word))
                    if do_github and not skip_repos:
                        time.sleep(1.2)
                        db.upsert_repo_check(conn, check_github(http, word))
                    pbar.update(1)
                    if i % 10 == 0:
                        conn.commit()
                conn.commit()

    if explicit:
        show_summary(words, db_path)
        return
    with db.session(db_path) as conn:
        survivors = db.words_passing_trademarks(conn, qversion)
        print(f"\n[check-trademarks] completely clear survivors: {len(survivors)}")
        for r in survivors[:25]:
            print(f"  {r['word']:<9} composite={r['composite']:.3f}")


def show_summary(words: list[str], db_path=None) -> None:
    """One table for the screened words: USPTO verdict and closest in-class mark, PyPI and GitHub collisions."""
    from rich import box
    from rich.table import Table
    from . import console

    t = Table(title="[bold cyan]Trademark and name-collision screen[/]", box=box.ROUNDED, expand=True,
              header_style="bold cyan", border_style="dim",
              caption="USPTO via Markbase (software, SaaS, data and education classes). A screen, not legal clearance.",
              caption_style="dim")
    t.add_column("Name", style="bold white", no_wrap=True)
    t.add_column("USPTO", no_wrap=True)
    t.add_column("Closest live or pending mark", ratio=2)
    t.add_column("PyPI", no_wrap=True)
    t.add_column("GitHub", no_wrap=True)
    style = {"clear": "[bold green]clear[/]", "watch": "[yellow]watch[/]", "conflict": "[bold red]conflict[/]",
             "taken": "[bold red]taken[/]", "error": "[dim]error[/]"}
    with db.session(db_path) as conn:
        for w in words:
            mc = conn.execute("SELECT * FROM mark_checks WHERE word = ? AND scope LIKE 'uspto:%'", (w,)).fetchone()
            if mc is None:
                us, close = "[dim]not checked[/]", ""
            else:
                risk = f" ({mc['highest_risk']})" if mc["highest_risk"] else ""
                us = style.get(mc["verdict"], mc["verdict"]) + risk
                h = conn.execute(
                    "SELECT mark, owner, similarity, live FROM mark_hits WHERE word = ? AND scope = ? "
                    "ORDER BY similarity DESC LIMIT 1", (w, mc["scope"])).fetchone()
                close = (f"{h['mark']}  [dim]{(h['owner'] or '')[:28]}, similarity {h['similarity']:.2f}[/]"
                         if h and h["similarity"] is not None else "[dim]none[/]" if mc["n_hits"] == 0 else "")
            repos = {r["source"]: r for r in conn.execute("SELECT * FROM repo_checks WHERE word = ?", (w,))}
            def repo(src: str) -> str:
                r = repos.get(src)
                if r is None:
                    return "[dim]-[/]"
                return style.get(r["verdict"], r["verdict"])
            t.add_row(w, us, close, repo("pypi"), repo("github"))
    console._console.print(t)
