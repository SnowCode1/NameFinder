"""Base-word value: how often a base produces top-tier coined names, fairly across sample sizes.

A raw maximum rewards bases that have simply been tried more (the best of 700 draws beats
the best of 40), and a mean rewards consistent mediocrity. Instead each base gets a
tail rate: the share of its scored coined names that land in the global top
(1 - BASE_TAIL_QUANTILE) of composites, shrunk toward the prior Beta(a, b) so a base with
three lucky names does not top the table.

The coin loop uses this as a Thompson-sampling bandit: once per cycle every base draws a
rate from Beta(a + hits, b + misses) over its most recent BASE_WINDOW names (so a base that
is running dry loses weight), and that draw, relative to the prior mean, multiplies the
base's sampling weight. Untried bases draw from the prior, so they keep getting explored.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from . import config


@dataclass
class BaseStat:
    word: str
    n: int               # scored coined names built on this base
    hits: int            # of those, how many reached the global tail
    best: float          # highest composite
    best_word: str
    recent_n: int        # names in the recent window
    recent_hits: int

    @property
    def rate(self) -> float:
        a, b = config.BASE_PRIOR
        return (self.hits + a) / (self.n + a + b)

    @property
    def recent_rate(self) -> float:
        a, b = config.BASE_PRIOR
        return (self.recent_hits + a) / (self.recent_n + a + b)


def _base_of(parents: str | None) -> str:
    return (parents or "").split(",")[0]


def tail_threshold(composites: list[float], quantile: float | None = None) -> float:
    """Composite at the given quantile of all scored coined names (inf when there are none)."""
    if not composites:
        return float("inf")
    q = config.BASE_TAIL_QUANTILE if quantile is None else quantile
    ordered = sorted(composites)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


SPOKEN_CLAUSE = "AND (s.say_aloud IS NOT NULL OR s.refined = 1)"


def base_stats(
    conn, qversion: str, window: int | None = None, spoken_only: bool = False,
) -> tuple[dict[str, BaseStat], float]:
    """Per-base tail statistics over every scored coined name with a recorded base. Returns (stats, threshold).

    `spoken_only` counts only names the LLM spelled out (brand scoring or refine); the tail
    threshold is then taken from those names too, so everything is judged on one footing.
    """
    window = config.BASE_WINDOW if window is None else window
    rows = conn.execute(
        f"""
        SELECT w.word, w.parents, s.composite FROM words w
        JOIN scores s ON s.word = w.word AND s.qversion = ?
        WHERE w.source = 'coined' AND w.parents IS NOT NULL AND w.parents != ''
          AND s.composite IS NOT NULL {SPOKEN_CLAUSE if spoken_only else ""}
        ORDER BY w.created_at, w.rowid
        """,
        (qversion,),
    ).fetchall()
    thr = tail_threshold([r["composite"] for r in rows])

    by_base: dict[str, list[tuple[str, float]]] = {}
    for r in rows:
        by_base.setdefault(_base_of(r["parents"]), []).append((r["word"], r["composite"]))

    stats: dict[str, BaseStat] = {}
    for base, items in by_base.items():
        recent = items[-window:]
        best_word, best = max(items, key=lambda t: t[1])
        stats[base] = BaseStat(
            word=base,
            n=len(items),
            hits=sum(c >= thr for _, c in items),
            best=best,
            best_word=best_word,
            recent_n=len(recent),
            recent_hits=sum(c >= thr for _, c in recent),
        )
    return stats, thr


@dataclass
class Bandit:
    """One cycle's Thompson draw: per-base weight multiplier and evidence count."""
    mult: dict[str, float]
    n: dict[str, int]
    spoken: bool = False   # True when judged on spoken-reviewed names only

    def factor(self, word: str) -> float:
        """Multiplier for a base; untried bases draw fresh from the prior each call."""
        m = self.mult.get(word)
        return m if m is not None else _draw(0, 0)

    def evidence(self, word: str) -> int:
        return self.n.get(word, 0)

    def favoured(self, k: int = 6) -> list[tuple[str, float]]:
        return sorted(self.mult.items(), key=lambda t: -t[1])[:k]


def _draw(hits: int, n: int) -> float:
    a, b = config.BASE_PRIOR
    sample = random.betavariate(a + hits, b + max(0, n - hits))
    return min(config.BASE_MULT_CAP, sample / (a / (a + b)))


def thompson(conn, qversion: str, spoken_only: bool | None = None) -> Bandit:
    """Draw this cycle's per-base multipliers from the recent-window posteriors.

    Spoken-only (default BASE_SPOKEN_ONLY) falls back to all names until at least
    BASE_SPOKEN_MIN_NAMES spoken-reviewed names exist.
    """
    spoken = config.BASE_SPOKEN_ONLY if spoken_only is None else spoken_only
    stats = None
    if spoken:
        stats, _ = base_stats(conn, qversion, spoken_only=True)
        if sum(s.n for s in stats.values()) < config.BASE_SPOKEN_MIN_NAMES:
            stats, spoken = None, False
    if stats is None:
        stats, _ = base_stats(conn, qversion)
    return Bandit(
        mult={b: _draw(s.recent_hits, s.recent_n) for b, s in stats.items()},
        n={b: s.n for b, s in stats.items()},
        spoken=spoken,
    )
