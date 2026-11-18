"""Stage 1 - clean local wordlist from wordfreq (+ WordNet for parts of speech)."""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from typing import Iterable

from . import config, db

ALPHA_RE = re.compile(r"^[a-z]+$")

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_wordnet():
    """Return the WordNet corpus, downloading it once if missing."""
    try:
        from nltk.corpus import wordnet as wn
    except ImportError:
        print("[prepare] nltk not installed - skipping part-of-speech filter", file=sys.stderr)
        return None
    try:
        wn.ensure_loaded()
        return wn
    except LookupError:
        try:
            import nltk

            print("[prepare] downloading NLTK WordNet corpus (one time)...", file=sys.stderr)
            nltk.download("wordnet", quiet=True)
            nltk.download("omw-1.4", quiet=True)
            wn.ensure_loaded()
            return wn
        except Exception as exc:  # pragma: no cover - offline machines
            print(f"[prepare] WordNet unavailable ({exc.__class__.__name__}) - continuing without POS filter",
                  file=sys.stderr)
            return None


def _pos_of(wn, word: str, cache: dict[str, str]) -> str:
    """Return a compact 'n', 'v', 'nv' or '' tag from WordNet."""
    if word in cache:
        return cache[word]
    tag = ""
    if wn is not None:
        try:
            has_n = bool(wn.synsets(word, pos=wn.NOUN))
            has_v = bool(wn.synsets(word, pos=wn.VERB))
            tag = ("n" if has_n else "") + ("v" if has_v else "")
        except Exception:
            tag = ""
    cache[word] = tag
    return tag


def _is_inflection(word: str, freq: dict[str, float]) -> bool:
    """True when `word` looks like a plural/gerund of a more common base word."""
    candidates: list[str] = []
    if word.endswith("ies") and len(word) > 4:
        candidates.append(word[:-3] + "y")
    if word.endswith("es"):
        candidates += [word[:-2], word[:-1]]
    if word.endswith("s") and not word.endswith("ss"):
        candidates.append(word[:-1])
    base_zipf = freq.get(word, 0.0)
    for stem in candidates:
        if len(stem) >= 3 and stem in freq and freq[stem] >= base_zipf - 0.4:
            return True
    return False


def build_wordlist(
    target: int = config.WORD_TARGET,
    scan: int = 90000,
    use_pos: bool = True,
) -> list[tuple[str, str, float]]:
    """Return [(word, pos, zipf)] ordered by descending frequency."""
    try:
        from wordfreq import top_n_list, zipf_frequency
    except ImportError as exc:
        raise SystemExit("wordfreq is required for `prepare`: pip install wordfreq") from exc

    vocabulary = top_n_list("en", scan)
    freq: dict[str, float] = {}
    for w in vocabulary:
        freq[w] = zipf_frequency(w, "en")

    stop = set(top_n_list("en", config.STOPWORD_DEPTH))
    wn = _load_wordnet() if use_pos else None
    pos_cache: dict[str, str] = {}

    kept: list[tuple[str, str, float]] = []
    seen: set[str] = set()
    dropped = {"shape": 0, "stop": 0, "zipf": 0, "suffix": 0, "inflection": 0, "pos": 0, "block": 0}

    for word in vocabulary:
        if not (config.WORD_MIN_LEN <= len(word) <= config.WORD_MAX_LEN) or not ALPHA_RE.match(word):
            dropped["shape"] += 1
            continue
        if word in stop:
            dropped["stop"] += 1
            continue
        z = freq.get(word, 0.0)
        if z < config.ZIPF_MIN or z > config.ZIPF_MAX:
            dropped["zipf"] += 1
            continue
        if word in config.WORD_BLOCKLIST:
            dropped["block"] += 1
            continue
        if word.endswith(config.SUFFIX_DROP):
            dropped["suffix"] += 1
            continue
        if _is_inflection(word, freq):
            dropped["inflection"] += 1
            continue

        tag = _pos_of(wn, word, pos_cache) if wn is not None else "n"
        if wn is not None and not tag:
            dropped["pos"] += 1
            continue

        if word in seen:
            continue
        seen.add(word)
        kept.append((word, tag, z))
        if len(kept) >= target:
            break

    print(
        "[prepare] scanned {scan}: kept {kept} | dropped {d}".format(
            scan=scan, kept=len(kept), d=", ".join(f"{k}={v}" for k, v in dropped.items() if v)
        ),
        file=sys.stderr,
    )
    return kept


def describe(words: Iterable[tuple[str, str, float]]) -> str:
    rows = list(words)
    if not rows:
        return "no words"
    by_len: dict[int, int] = {}
    for w, _pos, _z in rows:
        by_len[len(w)] = by_len.get(len(w), 0) + 1
    zs = [z for _w, _p, z in rows]
    lengths = ", ".join(f"{k}:{by_len[k]}" for k in sorted(by_len))
    return (f"{len(rows)} words | by length {lengths} | "
            f"zipf {min(zs):.2f}-{max(zs):.2f} (mean {sum(zs)/len(zs):.2f})")


def run(target: int | None = None, scan: int = 90000, use_pos: bool = True, db_path=None, refresh: bool = False) -> int:
    words = build_wordlist(target=target or config.WORD_TARGET, scan=scan, use_pos=use_pos)
    with db.session(db_path) as conn:
        if refresh:
            conn.execute("DELETE FROM words")
        added = db.upsert_words(conn, words, _now())
        db.set_meta(conn, "words_built_at", _now())
        total = db.count_words(conn)
    print(f"[prepare] inserted {added} new words (total cached: {total})")
    print(f"[prepare] {describe(words)}")
    return total
