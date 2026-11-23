"""Engine report: which coining engines (and which name features) reach the top tier.

A "hit" is a scored coined name whose composite is at or above a tail threshold: by default
the global top (1 - BASE_TAIL_QUANTILE) of coined composites, or a pinned value (`--threshold`)
so before/after comparisons across pool changes stay on one footing.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import bases, config


def _rows(conn, qversion: str, spoken_only: bool, since: timedelta | None = None, engine: str | None = None):
    """Scored coined names. `since` keeps only names coined within that window (by words.created_at);
    `engine` keeps one engine ('unknown' = names coined before engines were recorded)."""
    clause = bases.SPOKEN_CLAUSE if spoken_only else ""
    params: list = [qversion]
    if engine == "unknown":
        clause += " AND w.engine IS NULL"
    elif engine:
        clause += " AND w.engine = ?"
        params.append(engine)
    if since is not None:
        clause += " AND w.created_at >= ?"
        params.append((datetime.now(timezone.utc) - since).isoformat(timespec="seconds"))
    return conn.execute(
        f"""
        SELECT w.word, COALESCE(w.engine, 'unknown') AS engine, s.composite
        FROM words w JOIN scores s ON s.word = w.word AND s.qversion = ?
        WHERE w.source = 'coined' AND s.composite IS NOT NULL {clause}
        """,
        params,
    ).fetchall()


def engine_table(conn, qversion: str, threshold: float | None = None, spoken_only: bool = False,
                 since: timedelta | None = None):
    """Returns (rows, threshold). Each row: (engine, names, mean, tail_rate, best_word, best).

    With `since`, only recent names are counted, but the default threshold is still taken from
    all names so the cutoff does not move with the window.
    """
    data = _rows(conn, qversion, spoken_only, since)
    pool = _rows(conn, qversion, spoken_only) if since is not None else data
    thr = threshold if threshold is not None else bases.tail_threshold([r["composite"] for r in pool])
    by: dict[str, list] = defaultdict(list)
    for r in data:
        by[r["engine"]].append((r["word"], r["composite"]))
    out = []
    for eng, items in by.items():
        best_word, best = max(items, key=lambda t: t[1])
        out.append((
            eng, len(items), statistics.fmean(c for _, c in items),
            sum(c >= thr for _, c in items) / len(items), best_word, best,
        ))
    out.sort(key=lambda t: -t[3])
    return out, thr


def breakdown_table(conn, qversion: str, by: str, threshold: float | None = None, spoken_only: bool = False,
                    since: timedelta | None = None, engine: str | None = None, min_names: int = 1):
    """Tail rate per operator ('engine:operator') or per base layer. Returns (rows, threshold).

    Rows have the engine_table shape: (label, names, mean, tail_rate, best_word, best), best rate first.
    A base's layer is core, reinstated, jargon or foreign (words.lexicon); only the first parent counts.
    """
    clause = bases.SPOKEN_CLAUSE if spoken_only else ""
    params: list = [qversion]
    if engine:
        clause += " AND w.engine = ?"
        params.append(engine)
    if since is not None:
        clause += " AND w.created_at >= ?"
        params.append((datetime.now(timezone.utc) - since).isoformat(timespec="seconds"))
    data = conn.execute(
        f"""
        SELECT w.word, COALESCE(w.engine, 'unknown') AS engine, w.operator, w.parents, s.composite
        FROM words w JOIN scores s ON s.word = w.word AND s.qversion = ?
        WHERE w.source = 'coined' AND s.composite IS NOT NULL {clause}
        """,
        params,
    ).fetchall()
    pool = _rows(conn, qversion, spoken_only) if (engine or since is not None) else data
    thr = threshold if threshold is not None else bases.tail_threshold([r["composite"] for r in pool])
    layer = {r["word"]: (r["lexicon"] or "core") for r in
             conn.execute("SELECT word, lexicon FROM words WHERE source != 'coined'")} if by == "layer" else {}
    groups: dict[str, list] = defaultdict(list)
    for r in data:
        if by == "operator":
            if not r["operator"]:
                continue
            label = f"{r['engine']}:{r['operator']}"
        else:
            base = (r["parents"] or "").split(",")[0]
            label = layer.get(base, "unknown")
        groups[label].append((r["word"], r["composite"]))
    out = []
    for label, items in groups.items():
        if len(items) < min_names:
            continue
        best_word, best = max(items, key=lambda t: t[1])
        out.append((label, len(items), statistics.fmean(c for _, c in items),
                    sum(c >= thr for _, c in items) / len(items), best_word, best))
    out.sort(key=lambda t: -t[3])
    return out, thr


FEATURES: dict[str, Callable[[str], object]] = {
    "ending": lambda w: w[-2:],
    "last": lambda w: w[-1],
    "start": lambda w: w[:2],
    "length": len,
}


def lift_table(conn, qversion: str, feature: str, threshold: float | None = None,
               min_names: int = 200, spoken_only: bool = False, engine: str | None = None,
               since: timedelta | None = None):
    """Tail-rate lift of each feature value over the overall tail rate. Returns (rows, base_rate, threshold).

    Each row: (value, names, tail_rate, lift). Sorted best lift first.
    """
    data = _rows(conn, qversion, spoken_only, since, engine)
    # the default threshold is the global tail, so an engine's lift reads against all coined names
    pool = _rows(conn, qversion, spoken_only) if (engine or since is not None) else data
    thr = threshold if threshold is not None else bases.tail_threshold([r["composite"] for r in pool])
    if not data:
        return [], 0.0, thr
    base_rate = sum(r["composite"] >= thr for r in data) / len(data)   # the engine's own rate when filtered
    key = FEATURES[feature]
    counts: dict[object, list[int]] = defaultdict(lambda: [0, 0])
    for r in data:
        c = counts[key(r["word"])]
        c[0] += 1
        c[1] += r["composite"] >= thr
    rows = [(v, n, h / n, (h / n) / base_rate if base_rate else 0.0)
            for v, (n, h) in counts.items() if n >= min_names]
    rows.sort(key=lambda t: -t[3])
    return rows, base_rate, thr
