"""Name families, bans and collisions: deterministic checks on spelling and sound (no model, no network).

- Bans: roots, prefixes and endings no new name may use (`config.BANNED_ROOTS`, `BANNED_PREFIXES`,
  `BANNED_ENDINGS`). Checked in `coin.acceptable`, so every engine obeys them.
- Families: a name's head (its first letters) and tail (its ending). `orig-` and `-ome` are families.
- Saturation: families that fill too much of the current best names (the tournament leaderboard, else the
  composite ranking). Saturated families are named in the generation prompts and most new names in them
  are dropped (`config.FAMILY_KEEP` survive), so generation moves to new ground. The list updates itself
  as the leaderboard changes, unlike the hard bans.
- Collisions: names that look or sound like a reference term (competitors, field vocabulary, well-known
  brands: `prompts/collision_terms.txt`), or sound like a common English word (fotogram ~ photogram).
"""

from __future__ import annotations

import functools
import random
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from . import config, phonetics

HEAD_LEN = 4
# Endings recognised as one family unit (longest match wins); other names fall back to their last 3 letters.
FAMILY_ENDINGS = (
    "graph", "gram", "path", "lect", "ome", "eme", "dex", "lex", "set", "net", "ium", "ent",
    "sis", "ata", "ant", "ism", "ics", "lux", "fold", "line", "mark", "copy", "trace", "frame",
)


# --- bans ------------------------------------------------------------------
def banned(word: str) -> str | None:
    """The ban a name breaks (e.g. 'verit' or 'deep-'), or None."""
    w = word.lower()
    for root in config.BANNED_ROOTS:
        if root in w:
            return root
    for pre in config.BANNED_PREFIXES:
        if w.startswith(pre):
            return pre + "-"
    for end in config.BANNED_ENDINGS:
        if w.endswith(end):
            return "-" + end
    return None


def ban_labels() -> list[str]:
    return ([f"{r}" for r in config.BANNED_ROOTS] + [f"{p}-" for p in config.BANNED_PREFIXES]
            + [f"-{e}" for e in config.BANNED_ENDINGS])


# --- families --------------------------------------------------------------
def head(word: str) -> str:
    w = word.lower()
    if w.startswith("ae"):          # aether- and ether- are one family
        w = w[1:]
    return w[:HEAD_LEN] + "-"


def tail(word: str) -> str:
    w = word.lower()
    for e in sorted(FAMILY_ENDINGS, key=len, reverse=True):
        if w.endswith(e) and len(w) > len(e) + 1:
            return "-" + e
    return "-" + w[-3:]


def keys(word: str) -> tuple[str, str]:
    return head(word), tail(word)


@dataclass
class Saturation:
    """Family counts among the current best `n` names and the families over the cap."""
    n: int = 0
    counts: Counter = field(default_factory=Counter)
    cap: int = 0
    source: str = ""

    @property
    def saturated(self) -> list[tuple[str, int]]:
        if not self.n:
            return []
        free = set(config.FAMILY_FREE_ENDINGS)
        return [(f, c) for f, c in self.counts.most_common() if c >= self.cap and f not in free]

    def hits(self, word: str) -> list[str]:
        sat = {f for f, _ in self.saturated}
        return [k for k in keys(word) if k in sat]

    def admit(self, word: str, rng: random.Random | None = None) -> bool:
        """False for most names in a saturated family (FAMILY_KEEP of them still pass, so a family is never
        closed for good: a strong new member can still prove itself)."""
        if not self.hits(word):
            return True
        return (rng or random).random() < config.FAMILY_KEEP

    def labels(self, skip_banned: bool = True) -> list[str]:
        out = []
        for f, c in self.saturated:
            stem = f.strip("-")
            if skip_banned and any(stem.startswith(b) or b in stem for b in config.BANNED_ROOTS):
                continue
            out.append(f"{f} ({c})")
        return out


def compute_saturation(words: Sequence[str], share: float | None = None, minimum: int | None = None,
                       source: str = "") -> Saturation:
    share = config.FAMILY_SHARE if share is None else share
    minimum = config.FAMILY_MIN if minimum is None else minimum
    counts: Counter = Counter()
    for w in words:
        for k in set(keys(w)):
            counts[k] += 1
    cap = max(minimum, int(round(share * len(words))))
    return Saturation(n=len(words), counts=counts, cap=cap, source=source)


def best_names(conn, n: int | None = None) -> tuple[list[str], str]:
    """The current best coined names, best first, and where they came from: the active tournament ratings
    (reference names excluded), else the composite ranking."""
    from . import tournament
    n = n or config.FAMILY_TOP
    judge, _ = tournament.resolve_judge(conn)
    refs = set(tournament.load_refs())
    if judge:
        rows = conn.execute(
            "SELECT word FROM tournament_ratings WHERE judge = ? ORDER BY rating DESC LIMIT ?",
            (judge, n + len(refs)),
        ).fetchall()
        words = [r["word"] for r in rows if r["word"] not in refs][:n]
        if len(words) >= min(n, 30):
            return words, f"tournament ({judge})"
    rows = conn.execute(
        """
        SELECT s.word FROM scores s JOIN words w ON w.word = s.word AND w.source = 'coined'
        WHERE s.qversion = ? AND s.composite IS NOT NULL ORDER BY s.composite DESC LIMIT ?
        """,
        (config.QUESTION_VERSION, n),
    ).fetchall()
    return [r["word"] for r in rows], "composite"


_CACHE: dict[str, tuple[float, Saturation]] = {}


def saturation(conn, max_age: float = 600.0) -> Saturation:
    """Saturation over the current best names, recomputed at most every `max_age` seconds."""
    hit = _CACHE.get("sat")
    if hit and time.monotonic() - hit[0] < max_age:
        return hit[1]
    words, source = best_names(conn)
    sat = compute_saturation(words, source=source)
    _CACHE["sat"] = (time.monotonic(), sat)
    return sat


def avoid_text(conn) -> str:
    """Prompt lines naming the bans and the overused families, for every generation prompt."""
    lines = []
    bans = ban_labels()
    if bans:
        lines.append("Never use these roots, prefixes or endings (banned: they collide with a competitor, carry "
                     "the wrong meaning, or are worn out): " + ", ".join(bans) + ".")
    if config.FAMILY_GATE:
        sat = saturation(conn).labels()
        if sat:
            lines.append("Overused among our best names so far, so avoid these openings and endings and explore "
                         "other ground (most new names in them are thrown away): " + ", ".join(sat) + ".")
    return "\n".join(lines)


# --- sound -----------------------------------------------------------------
_RULES = (
    (r"^ae", "e"), (r"^oe", "e"), (r"ae", "e"), (r"oe", "e"),
    (r"tch", "C"), (r"sch", "sk"), (r"chr", "kr"), (r"ch(?=[^aeiouy]|$)", "k"), (r"ch", "C"),
    (r"ph", "f"), (r"th", "0"), (r"sh", "S"), (r"wh", "w"), (r"gh", ""), (r"ck", "k"), (r"qu", "kw"), (r"q", "k"),
    (r"x", "ks"), (r"sc(?=[eiy])", "s"), (r"c(?=[eiy])", "s"), (r"c", "k"), (r"g(?=[eiy])", "j"),
    (r"dg", "j"), (r"tio(?=n)", "S"), (r"sio(?=n)", "S"), (r"wr", "r"), (r"^kn", "n"), (r"^gn", "n"), (r"^ps", "s"),
    (r"(?<=[^aeiou])y(?=[^aeiou]|$)", "i"), (r"y$", "i"), (r"z", "s"),
)
_RULES_C = [(re.compile(p), r) for p, r in _RULES]
_VOWEL_CLASS = str.maketrans({"a": "a", "e": "e", "i": "i", "o": "o", "u": "u", "y": "i"})


@functools.lru_cache(maxsize=200_000)
def sound_key(word: str) -> str:
    """Rough phonetic spelling: consonant digraphs merged, soft c/g resolved, doubled letters and a silent
    final e dropped, vowel runs reduced to their first vowel. Applied the same way to every word, so its
    errors cancel when two keys are compared."""
    w = re.sub(r"[^a-z]", "", word.lower())
    if len(w) > 3 and w.endswith("e") and w[-2] not in "aeiouy" and w[-3] in "aeiouy":
        w = w[:-1]
    for pat, rep in _RULES_C:
        w = pat.sub(rep, w)
    w = re.sub(r"(.)\1+", r"\1", w)
    w = re.sub(r"([aeiou])[aeiou]+", r"\1", w)
    return w.translate(_VOWEL_CLASS)


def skeleton(key: str) -> str:
    """A sound key with every vowel run reduced to one 'V' (fotogram and photogram get the same key)."""
    return re.sub(r"[aeiou]+", "V", key)


def lev(a: str, b: str, cap: int = 99) -> int:
    """Levenshtein distance, stopping early once every path exceeds `cap`."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        best = cur[0]
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            best = min(best, cur[j])
        if best > cap:
            return cap + 1
        prev = cur
    return prev[-1]


# Consonant pairs a listener confuses easily (sound-key letters: 0 = th, S = sh, C = ch, j = soft g)
_NEAR = {frozenset(p) for p in ("t0", "d0", "dt", "sz", "fv", "pb", "kg", "mn", "sS", "SC", "Cj", "f0", "ks", "td")}
_VOWELS = set("aeiou")


def _sub_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    if a in _VOWELS and b in _VOWELS:
        return 0.3
    if frozenset((a, b)) in _NEAR:
        return 0.5
    return 1.0


def _indel_cost(c: str) -> float:
    return 0.5 if c in _VOWELS else 1.0


def sound_distance(a: str, b: str) -> float:
    """Edit distance between two sound keys where near sounds are cheap: a vowel change costs 0.3, a
    confusable consonant pair 0.5, a vowel inserted or dropped 0.5, any other edit 1. Listeners hear the
    onset reliably, so keys that start with a different sound cost 0.5 more."""
    onset = 0.5 if a[:1] != b[:1] else 0.0
    return onset + _edit_cost(a, b)


def _edit_cost(a: str, b: str) -> float:
    prev = [0.0]
    for c in b:
        prev.append(prev[-1] + _indel_cost(c))
    for ca in a:
        cur = [prev[0] + _indel_cost(ca)]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + _indel_cost(ca), cur[j - 1] + _indel_cost(cb), prev[j - 1] + _sub_cost(ca, cb)))
        prev = cur
    return prev[-1]


def similarity(a: str, b: str) -> float:
    """1 - edit distance / longer length."""
    n = max(len(a), len(b)) or 1
    return 1.0 - lev(a, b) / n


# --- collisions with reference terms ---------------------------------------
DEFAULT_COLLISION_TERMS = """
# Used only when the prompt set has no collision_terms.txt. List your competitors and the field's own
# vocabulary there; these are well-known brands that sit close to common coining patterns.
google kleenex pokedex rolodex netflix spotify xerox tesla nexus codex
"""


def load_terms() -> list[str]:
    text = config.load_prompt("collision_terms.txt", DEFAULT_COLLISION_TERMS)
    out: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        out += [t for t in re.split(r"[\s,]+", line.lower()) if re.fullmatch(r"[a-z]{3,}", t)]
    return list(dict.fromkeys(out))


@dataclass
class Collision:
    term: str
    score: float       # 0..1, higher is closer
    how: str           # spelling | sound | contains


def collisions(word: str, terms: Sequence[str] | None = None, threshold: float | None = None) -> list[Collision]:
    """Reference terms this name looks or sounds like, closest first."""
    terms = load_terms() if terms is None else terms
    threshold = config.COLLISION_SIM if threshold is None else threshold
    w = word.lower()
    kw = sound_key(w)
    out = []
    for t in terms:
        if t == w:
            out.append(Collision(t, 1.0, "same"))
            continue
        spell = similarity(w, t)
        sound = similarity(kw, sound_key(t))
        contains = 0.0
        if len(t) >= 5 and t in w:
            contains = 0.9
        elif len(w) >= 5 and w in t:
            contains = 0.9
        best = max((spell, "spelling"), (sound, "sound"), (contains, "contains"))
        if best[0] >= threshold:
            out.append(Collision(t, round(best[0], 3), best[1]))
    return sorted(out, key=lambda c: -c.score)


# --- sound-alikes of common English words ----------------------------------
class SoundIndex:
    """Common English words (and the collision terms) by sound and by spelling, for a near-neighbour search.

    Two flags:
    - sounds like: the sound keys are within SOUNDALIKE_MAX_DIST by `sound_distance` (fotogram ~ photogram:
      ph and f share one key, distance 0). Candidates come from a one-deletion index on the consonant skeleton.
    - reads as a misspelling: one letter from a common word of 6+ letters (relbound ~ rebound).
    Names with a short sound key are not checked (every short word has neighbours).
    """

    def __init__(self, words: Iterable[str], spelled: Iterable[str] = ()):
        self.full: dict[str, str] = {}           # word -> sound key
        self.deletes: dict[str, set[str]] = {}   # skeleton or one deletion of it -> words
        self.letters: dict[str, set[str]] = {}   # spelling or one deletion of it -> common words
        for w in words:
            if w in self.full:
                continue
            k = sound_key(w)
            self.full[w] = k
            for d in self._variants(skeleton(k)):
                self.deletes.setdefault(d, set()).add(w)
        for w in spelled:
            for d in self._variants(w):
                self.letters.setdefault(d, set()).add(w)

    @staticmethod
    def _variants(s: str) -> set[str]:
        return {s} | {s[:i] + s[i + 1:] for i in range(len(s))}

    def near(self, word: str, exclude: Iterable[str] = ()) -> list[tuple[str, float]]:
        """[(word, sound distance)] for words this name may be heard as, closest first."""
        w = word.lower()
        k = sound_key(w)
        sk = skeleton(k)
        if len(sk) < config.SOUNDALIKE_MIN_KEY:
            return []
        skip = {w, *exclude}
        found: dict[str, float] = {}
        for d in self._variants(sk):
            for other in self.deletes.get(d, ()):
                if other in skip or other in found:
                    continue
                if other in w or (len(w) >= 5 and w in other):
                    continue       # a name built on a real word is not a mishearing of it
                dist = sound_distance(k, self.full[other])
                if dist <= config.SOUNDALIKE_MAX_DIST:
                    found[other] = round(dist, 2)
        return sorted(found.items(), key=lambda kv: (kv[1], kv[0]))

    def misspelling_of(self, word: str, exclude: Iterable[str] = ()) -> list[str]:
        """Common words one letter away from this name (insert, delete or substitute)."""
        w = word.lower()
        skip = {w, *exclude}
        out = set()
        for d in self._variants(w):
            for other in self.letters.get(d, ()):
                if other not in skip and other not in w and lev(w, other, cap=1) <= 1:
                    out.add(other)
        return sorted(out)


@functools.lru_cache(maxsize=1)
def sound_index() -> SoundIndex:
    """Index over English words at or above SOUNDALIKE_ZIPF (5+ letters) plus the collision terms."""
    words: list[str] = []
    spelled: list[str] = []
    try:
        from wordfreq import top_n_list, zipf_frequency
        for w in top_n_list("en", 120_000):
            if len(w) < 5 or not (w.isalpha() and w.isascii()):
                continue
            z = zipf_frequency(w, "en")
            if z >= config.SOUNDALIKE_ZIPF:
                words.append(w)
            if len(w) >= 6 and z >= config.MISSPELLING_ZIPF:
                spelled.append(w)
    except ImportError:  # pragma: no cover
        pass
    terms = load_terms()
    return SoundIndex(words + terms, spelled + [t for t in terms if len(t) >= 6])


def soundalikes(word: str, parents: Sequence[str] = ()) -> list[str]:
    """Common words or reference terms this name may be heard as or read as a misspelling of (closest first).

    The name's own parent words are ignored."""
    idx = sound_index()
    heard = [w for w, _ in idx.near(word, exclude=parents)]
    misread = [w for w in idx.misspelling_of(word, exclude=parents) if w not in heard]
    return heard + misread


# --- tournament pool screen --------------------------------------------------
def screen_pool(pool: Sequence[str], keep: Iterable[str] = (), cap: int | None = None) -> tuple[list[str], Counter]:
    """Pool names that pass the bans, the collision and sound-alike checks, and at most `cap` per family
    (in pool order, so the best-ranked members of a family stay). `keep` names (references) always pass.
    Returns (kept, dropped counts by reason)."""
    keepset = set(keep)
    cap = cap or max(4, int(round(config.FAMILY_POOL_SHARE * len(pool))))
    terms = load_terms()
    used: Counter = Counter()
    out: list[str] = []
    dropped: Counter = Counter()
    for w in pool:
        if w in keepset:
            out.append(w)
            continue
        if banned(w):
            dropped["banned"] += 1
            continue
        if config.COLLISION_GATE and collisions(w, terms):
            dropped["too close to a reference term"] += 1
            continue
        if config.SOUNDALIKE_GATE and soundalikes(w):
            dropped["sounds like or misspells a real word"] += 1
            continue
        if config.READABILITY_GATE and phonetics.bad_ending(w):
            dropped["unreadable ending"] += 1
            continue
        fam = keys(w)
        if any(used[k] >= cap for k in fam):
            dropped[f"over the family cap ({cap})"] += 1
            continue
        for k in set(fam):
            used[k] += 1
        out.append(w)
    return out, dropped


# --- one gate for the generation stages ------------------------------------
@dataclass
class Verdict:
    ok: bool
    reason: str = ""


class Gate:
    """Bans, saturation and collisions in one admission check, built once per batch."""

    def __init__(self, conn, use_saturation: bool | None = None, rng: random.Random | None = None):
        self.sat = saturation(conn) if (config.FAMILY_GATE if use_saturation is None else use_saturation) else None
        self.terms = load_terms()
        self.rng = rng or random.Random()
        self.dropped: Counter = Counter()
        self.last_category = ""

    def check(self, word: str, parents: Sequence[str] = ()) -> Verdict:
        b = banned(word)
        if b:
            return self._drop("banned", f"banned {b}")
        if self.sat is not None and not self.sat.admit(word, self.rng):
            return self._drop("overused family", "overused family " + "/".join(self.sat.hits(word)))
        if config.COLLISION_GATE:
            hit = collisions(word, self.terms)
            if hit:
                return self._drop("too close to a reference term", f"too close to {hit[0].term}")
        if config.SOUNDALIKE_GATE:
            near = soundalikes(word, parents)
            if near:
                return self._drop("sounds like a real word", f"sounds like {near[0]}")
        if config.READABILITY_GATE:
            tail = phonetics.bad_ending(word)
            if tail:
                return self._drop("unreadable ending", f"no vowel before final '{tail}'")
        return Verdict(True)

    def _drop(self, category: str, reason: str) -> Verdict:
        self.dropped[category] += 1
        self.last_category = category
        return Verdict(False, reason)

    def summary(self) -> str:
        return ", ".join(f"{n} {why}" for why, n in self.dropped.most_common())
