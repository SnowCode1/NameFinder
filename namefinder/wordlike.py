"""Wordlikeness: how much a string looks like an English word, from a letter trigram model.

The model is trained on frequent English words (wordfreq, weighted by frequency), so common
letter sequences score high and "xqzvt"-style strings score low. `score` is the mean log2
probability per letter (end of word included); higher is more wordlike. It is deterministic,
needs no LLM, and replaces hand-written consonant rules as the soft part of the coin gate.

`threshold(pct)` gives the score that `pct` of held-out dictionary words reach or exceed,
so a gate of 0.05 keeps everything about as wordlike as the least wordlike 5% of real words.
"""

from __future__ import annotations

import functools
import math
import re
from collections import defaultdict

_ALPHA = re.compile(r"^[a-z]+$")
_START, _END = "^", "$"
_K = 0.05            # add-k smoothing
_ALPHABET = 28        # a-z plus the end marker (and a little slack)
_HOLDOUT = 10         # one word in this many is held out to calibrate thresholds


class Model:
    """Interpolated letter trigram model."""

    def __init__(self, words: list[tuple[str, float]]):
        self.tri: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.bi: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for w, weight in words:
            s = _START + _START + w + _END
            for i in range(2, len(s)):
                self.tri[s[i - 2 : i]][s[i]] += weight
                self.bi[s[i - 1]][s[i]] += weight
        self.tri_tot = {k: sum(v.values()) for k, v in self.tri.items()}
        self.bi_tot = {k: sum(v.values()) for k, v in self.bi.items()}

    def _p(self, ctx2: str, ch: str) -> float:
        b = (self.bi[ctx2[1]].get(ch, 0.0) + _K) / (self.bi_tot.get(ctx2[1], 0.0) + _K * _ALPHABET)
        t_tot = self.tri_tot.get(ctx2, 0.0)
        if not t_tot:
            return b
        t = self.tri[ctx2].get(ch, 0.0) / t_tot
        lam = t_tot / (t_tot + 20.0)          # trust the trigram more as its context gets more evidence
        return lam * t + (1.0 - lam) * b

    def score(self, word: str) -> float:
        s = _START + _START + word.lower() + _END
        total = sum(math.log2(self._p(s[i - 2 : i], s[i])) for i in range(2, len(s)))
        return total / (len(s) - 2)


def _training_words() -> list[tuple[str, float]]:
    from wordfreq import top_n_list, zipf_frequency
    out = []
    for w in top_n_list("en", 60000):
        if 3 <= len(w) <= 12 and _ALPHA.match(w):
            out.append((w, 1.0 + max(0.0, zipf_frequency(w, "en") - 2.0)))
    return out


@functools.lru_cache(maxsize=1)
def _models() -> tuple[Model, list[float]]:
    """(model trained on all words, sorted held-out scores from a model that never saw them)."""
    words = _training_words()
    held = [w for i, (w, _) in enumerate(words) if i % _HOLDOUT == 0]
    train = [t for i, t in enumerate(words) if i % _HOLDOUT != 0]
    check = Model(train)
    held_scores = sorted(check.score(w) for w in held)
    return Model(words), held_scores


def score(word: str) -> float:
    """Mean log2 probability per letter; around -2.2 for ordinary words, below -3.5 for gibberish."""
    return _models()[0].score(word)


def threshold(pct: float) -> float:
    """Score reached or exceeded by (1 - pct) of held-out real words (pct=0.05 -> the 5th percentile)."""
    held = _models()[1]
    return held[min(len(held) - 1, max(0, int(len(held) * pct)))]


def is_wordlike(word: str, pct: float) -> bool:
    return score(word) >= threshold(pct)
