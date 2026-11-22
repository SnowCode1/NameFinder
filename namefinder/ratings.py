"""Plackett-Luce ratings for the tournament, fitted in one batch over every logged match.

A match is one best-first ranking of a group. Plackett-Luce models it as successive choices: the
winner is picked from the whole group with probability proportional to its strength, then the next
from the rest, and so on. Fitting all matches at once (Hunter's MM algorithm) uses every comparison
jointly, so it does not depend on the order matches were played in. On this project's data it is
far more reliable than the running Elo it replaces (split-half rank correlation 0.76 vs 0.30 on the
top 100), and it gives each name a standard error, which is what pruning needs.

Strengths are reported on an Elo-like scale (1500 average, 400 points = 10x odds) so the existing
tables and thresholds keep their meaning.
"""

from __future__ import annotations

import math
from typing import Sequence

SCALE = 400.0 / math.log(10.0)   # log-strength -> Elo-like points
BASE = 1500.0
PRIOR = 1.0                      # one virtual win and one virtual loss against an average name


def fit(
    matches: Sequence[Sequence[str]], init: dict[str, float] | None = None,
    iters: int = 300, tol: float = 1e-6,
) -> tuple[dict[str, float], dict[str, float], dict[str, int]]:
    """(rating, standard error, games) per name, on the Elo-like scale.

    `init` (ratings from a previous fit) warm-starts the iteration, which makes refitting after
    each round fast. The weak prior keeps names with only wins or only losses finite.
    """
    items = sorted({w for m in matches for w in m})
    if not items:
        return {}, {}, {}
    w = {i: math.exp(((init or {}).get(i, BASE) - BASE) / SCALE) for i in items}
    wins = {i: PRIOR for i in items}
    games = {i: 0 for i in items}
    for m in matches:
        for k in m[:-1]:
            wins[k] += 1
        for k in m:
            games[k] += 1

    for _ in range(iters):
        denom = {i: 2 * PRIOR / (1.0 + w[i]) for i in items}
        for m in matches:
            tail = _tails([w[x] for x in m])
            acc = 0.0
            for i in range(len(m) - 1):
                acc += 1.0 / tail[i]
                denom[m[i]] += acc
            denom[m[-1]] += acc
        new = {i: wins[i] / denom[i] for i in items}
        g = math.exp(sum(math.log(v) for v in new.values()) / len(new))
        delta = max(abs(math.log(new[i] / g) - math.log(w[i])) for i in items)
        w = {i: new[i] / g for i in items}
        if delta < tol:
            break

    # standard errors from the diagonal of the observed information
    info = {i: 2 * PRIOR * w[i] / (1.0 + w[i]) ** 2 for i in items}
    for m in matches:
        ws = [w[x] for x in m]
        tail = _tails(ws)
        for stage in range(len(m) - 1):
            for j in range(stage, len(m)):
                p = ws[j] / tail[stage]
                info[m[j]] += p * (1.0 - p)
    rating = {i: BASE + SCALE * math.log(w[i]) for i in items}
    se = {i: SCALE / math.sqrt(info[i]) for i in items}
    return rating, se, games


def _tails(ws: Sequence[float]) -> list[float]:
    tail = [0.0] * len(ws)
    s = 0.0
    for i in range(len(ws) - 1, -1, -1):
        s += ws[i]
        tail[i] = s
    return tail
