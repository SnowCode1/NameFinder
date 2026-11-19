"""Deterministic pronounceability analysis for candidate names.

Cheap, repeatable checks that do not need an LLM: syllable split, syllable
count, hard consonant clusters, and graphemes that readers pronounce in more
than one way. Output feeds the brand composite and the refine pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

VOWELS = "aeiouy"

# Consonant clusters that may open a syllable in English.
VALID_ONSETS = frozenset("""
bl br cl cr dr fl fr gl gr pl pr sc sk sl sm sn sp st sw tr tw th sh ch ph wh
str spr scr spl thr shr sch squ
""".split())

# Three-consonant runs that are natural in English and should not be penalised.
EASY_TRIPLES = frozenset("str spr scr spl thr shr sch nst nds lds rst rld".split())

# Graphemes with more than one plausible reading.
AMBIGUOUS_PATTERNS = (
    (re.compile(r"g(?=[eiy])"), "soft g (g or j?)"),
    (re.compile(r"ph"), "ph (f)"),
    (re.compile(r"(?<!^)y(?=[aeiou])"), "medial y"),
    (re.compile(r"ough|augh|eigh"), "gh digraph"),
    (re.compile(r"ae|oe|eu|ia|io(?!n)|ua|uo"), "vowel hiatus"),
    (re.compile(r"ei|ie|ou|ea"), "vowel digraph"),
    (re.compile(r"(?<!^)x(?=[aeiou])"), "x (ks or gz?)"),
    (re.compile(r"c(?=[eiy])(?<=[aeiou]c)"), "soft c after vowel"),
    (re.compile(r"sc(?=[eiy])"), "sc (s or sk?)"),
    (re.compile(r"[aeiou]{3}"), "vowel run"),
)

# Junctions that are physically awkward to say across a syllable boundary.
CLASH_JUNCTIONS = re.compile(r"(?:tl|dl|vl|zl|tn|dn|kn|gn|pn|bm|dm|tm|fn|vn)$")


@dataclass
class Phonetics:
    word: str
    syllables: list[str]
    score: float              # 0..1, higher is easier to say and spell
    ambiguity: float          # 0..1, higher is more likely to be misread
    notes: list[str] = field(default_factory=list)

    @property
    def split(self) -> str:
        return "-".join(self.syllables)


def _is_vowel_at(w: str, i: int) -> bool:
    ch = w[i]
    if ch in "aeiou":
        return True
    if ch == "y":
        # y is a vowel unless it starts a syllable before another vowel
        return not (i == 0 and len(w) > 1 and w[1] in "aeiou")
    return False


def syllabify(word: str) -> list[str]:
    """Heuristic English syllabification (good enough for ranking, not for linguistics)."""
    w = word.lower()
    if not w:
        return []

    # Vowel nuclei as [start, end) groups
    nuclei: list[list[int]] = []
    i = 0
    while i < len(w):
        if _is_vowel_at(w, i):
            j = i
            while j + 1 < len(w) and _is_vowel_at(w, j + 1):
                j += 1
            nuclei.append([i, j + 1])
            i = j + 1
        else:
            i += 1

    # Silent final e: drop its nucleus unless it follows consonant + l (table)
    if len(nuclei) > 1 and w.endswith("e") and nuclei[-1] == [len(w) - 1, len(w)]:
        if not (len(w) >= 3 and w[-2] == "l" and w[-3] not in VOWELS):
            nuclei.pop()

    if len(nuclei) <= 1:
        return [w]

    cuts: list[int] = []
    for a, b in zip(nuclei, nuclei[1:]):
        gap = w[a[1] : b[0]]
        if len(gap) == 0:
            cuts.append(b[0])           # hiatus: split between vowels
        elif len(gap) == 1:
            cuts.append(a[1])           # V-CV
        else:
            # keep the longest valid onset with the next syllable
            cut = a[1] + 1
            for k in range(len(gap) - 1, -1, -1):
                if gap[k:] in VALID_ONSETS:
                    cut = a[1] + k
                    break
            cuts.append(max(cut, a[1] + (1 if len(gap) > 1 else 0)))

    parts: list[str] = []
    prev = 0
    for c in cuts:
        parts.append(w[prev:c])
        prev = c
    parts.append(w[prev:])
    return [p for p in parts if p]


def analyze(word: str) -> Phonetics:
    w = word.lower().strip()
    syls = syllabify(w)
    notes: list[str] = []
    n = len(syls)

    if n == 2 or n == 3:
        syl_score = 1.0
    elif n == 1:
        syl_score = 0.85
    elif n == 4:
        syl_score = 0.7
    else:
        syl_score = 0.4
    if n >= 4:
        notes.append(f"{n} syllables")

    hits = [label for pat, label in AMBIGUOUS_PATTERNS if pat.search(w)]
    if hits:
        notes.extend(hits)
    ambiguity = min(1.0, 0.15 * len(hits))

    penalty = 0.0
    for m in re.finditer(r"[^aeiouy]{3,}", w):
        run = m.group()
        splittable = any(
            run[k:] in VALID_ONSETS and k <= 2 for k in range(1, len(run) - 1)
        )
        if not splittable and not any(run[k : k + 3] in EASY_TRIPLES for k in range(len(run) - 2)):
            penalty += 0.08
            notes.append(f"hard cluster '{run}'")
    for s in syls[:-1]:
        if CLASH_JUNCTIONS.search(s + "") and len(s) > 1:
            penalty += 0.06
            notes.append(f"clash at '{s}'")
    if re.search(r"(.)\1", w):
        # doubled letters are fine, but triple-syllable echoes (nana, tata) read as childish
        pass
    if n >= 2 and syls[0][-2:] == syls[1][:2] and len(syls[0]) > 1:
        penalty += 0.05
        notes.append("echoing syllables")

    score = syl_score * (1.0 - 0.6 * ambiguity) - penalty
    return Phonetics(w, syls, round(max(0.0, min(1.0, score)), 4), round(ambiguity, 4), notes)


def phonetic_gate(score: float, weight: float = 0.3) -> float:
    """Multiplier applied to a composite: 1.0 for perfect, (1-weight) for unpronounceable."""
    return (1.0 - weight) + weight * max(0.0, min(1.0, score))


# --- verb-likeness ----------------------------------------------------------
# Heuristic for "I just ___ it and found what I needed": short, punchy names that
# end on a hard consonant and take -ed / -ing cleanly (ping, grep, zoom, slack).
_NOUNISH_ENDINGS = ("ion", "ness", "ity", "ment", "ics", "ism", "ist", "ance", "ence", "ia", "ium", "ous", "ive", "ology")
_VERBISH_ENDINGS = ("ify", "ize", "ise", "ate")


def verb_score(word: str) -> float:
    """0..1 deterministic guess at how well a name works as a verb (no LLM)."""
    w = word.lower().strip()
    if not w:
        return 0.0
    n = len(syllabify(w))

    length = 1.0 if 4 <= len(w) <= 7 else 0.8 if len(w) == 3 else 0.7 if len(w) == 8 else 0.4
    syl = 1.0 if n <= 2 else 0.7 if n == 3 else 0.35

    if w.endswith(_VERBISH_ENDINGS):
        end = 0.9
    elif w.endswith(_NOUNISH_ENDINGS):
        end = 0.3
    elif w[-1] in "ptkxbdgc":
        end = 1.0
    elif w[-1] in "lmnrszvfh" or w.endswith(("le", "ge", "ce", "se", "ze", "ve")):
        end = 0.8
    elif w[-1] == "e":
        end = 0.6          # silent e: probe, trace
    elif w[-1] == "y":
        end = 0.55
    else:
        end = 0.4          # open vowel: -a, -o, -i, -u

    score = 0.3 * length + 0.3 * syl + 0.4 * end
    if len(re.search(r"[^aeiouy]*$", w).group()) >= 3:
        score -= 0.15      # tracing a 3-consonant coda (-ed / -ing) gets clumsy
    return round(max(0.0, min(1.0, score)), 4)
