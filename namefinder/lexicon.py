"""Extra base-word layers beyond the core `prepare` wordlist (`namefinder lexicon`).

`prepare` keeps WordNet nouns and verbs in a mid-frequency band and drops many endings (-ity,
-ance, -ence, -ism, -ist, -ical, -ous, -tion ...). That is right for bases that get suffixed, but
it also removes whole classes of good names: abstract nouns ("perplexity", "similarity",
"provenance") and rare technical terms ("heuristic", "stemmatics").

Layers (stored in `words.lexicon`, with `source = 'lexicon'` and a short WordNet gloss in `words.gloss`):

- `reinstated`: ordinary-frequency words (core zipf band) that `prepare` dropped for their ending,
  length or part of speech, minus inflections.
- `jargon`: rarer words (zipf 1.5 to the core floor) that WordNet tags with a subject domain
  (law, mathematics, logic, linguistics ...), minus domains that never suit a software name, plus
  rare abstract nouns (cognition, attribute, state, relation ...: "perplexity", "antecedent").
- `foreign`: common words of Romance and Scandinavian languages, from the OMW wordnet translations of
  abstract English concepts, shared by at least two languages (fase, pausa, consulta). `words.gloss`
  says what they mean ("Spanish/Italian for 'phase'"). Needs `nltk.download("omw-1.4")`, done on first use.
- `rare`: the same rare band without a domain tag. Large and mostly obscure, so off by default.

Everything is deterministic and local. The new words are unscored; root-score them with
`namefinder score --mode root --limit N` (the gloss is shown to the model for unfamiliar words).
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher

from . import config, db, phonetics, words as core_words

LAYERS = ("reinstated", "jargon", "foreign", "rare")
DEFAULT_LAYERS = ("reinstated", "jargon", "foreign")

_ALPHA = re.compile(r"^[a-z]+$")
GLOSS_MAX = 90


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _gloss(syn) -> str:
    text = (syn.definition() or "").split(";")[0].strip()
    return text if len(text) <= GLOSS_MAX else text[: GLOSS_MAX - 1].rstrip() + "…"


def _inflected(wn, w: str) -> bool:
    """True for plurals, participles and other inflections of a base form (twins, plucked, changed)."""
    for p in ("n", "v", "a"):
        base = wn.morphy(w, p)
        if base and base != w:
            return True
    return False


def collect(wn, layers: tuple[str, ...], have: set[str]) -> list[tuple[str, str, float, str, str]]:
    """Candidate rows (word, pos, zipf, layer, gloss) from WordNet, best-first within each layer."""
    from wordfreq import top_n_list, zipf_frequency

    stop = set(top_n_list("en", config.STOPWORD_DEPTH))
    skip_domains = set(config.LEXICON_SKIP_DOMAINS)
    block = config.WORD_BLOCKLIST
    min_len, max_len = config.LEXICON_MIN_LEN, config.LEXICON_MAX_LEN

    pos_of: dict[str, set[str]] = {}
    domains: dict[str, set[str]] = {}
    abstract: set[str] = set()
    gloss: dict[str, str] = {}
    domain_gloss: dict[str, str] = {}
    for syn in wn.all_synsets():
        if syn.instance_hypernyms():                      # people, places, titles
            continue
        pos = "a" if syn.pos() in ("a", "s") else syn.pos()
        if pos == "r":                                    # adverbs
            continue
        doms = {d.name().split(".")[0] for d in list(syn.topic_domains()) + list(syn.usage_domains())}
        for lem in syn.lemmas():
            w = lem.name()
            if not _ALPHA.match(w) or not (min_len <= len(w) <= max_len):
                continue
            pos_of.setdefault(w, set()).add(pos)
            if syn.lexname() in config.LEXICON_ABSTRACT_LEXNAMES:
                abstract.add(w)
            gloss.setdefault(w, _gloss(syn))
            if doms:
                domains.setdefault(w, set()).update(doms)
                domain_gloss.setdefault(w, _gloss(syn))   # prefer the sense that carries the domain

    out: list[tuple[str, str, float, str, str]] = []
    for w, poses in pos_of.items():
        if w in have or w in stop or w in block or any(b in w for b in config.COIN_BLOCK_SUBSTRINGS):
            continue
        z = zipf_frequency(w, "en")
        if z < config.LEXICON_ZIPF_MIN:
            continue
        if w.endswith(config.LEXICON_DROP_ENDINGS) or _inflected(wn, w):
            continue
        if phonetics.analyze(w).score < config.LEXICON_MIN_PHONETIC:
            continue
        doms = domains.get(w, set())
        if doms and doms <= skip_domains:                 # only ever used in sport, cooking, anatomy ...
            continue
        if z >= config.ZIPF_MIN:
            layer = "reinstated" if z <= config.ZIPF_MAX else None
            if layer and not ({"n", "v"} & poses):
                layer = None
        else:
            if doms or (w in abstract and z >= config.LEXICON_ABSTRACT_ZIPF_MIN):
                layer = "jargon"
            else:
                layer = "rare"
        if layer is None or layer not in layers:
            continue
        pos = "".join(sorted(poses, key=lambda p: "nva".index(p)))
        out.append((w, pos, z, layer, domain_gloss.get(w) or gloss[w]))
    out.sort(key=lambda r: (LAYERS.index(r[3]), -r[2], r[0]))
    return out


def _omw_zip():
    """Open the OMW archive (downloading it once if missing)."""
    import zipfile
    import nltk
    try:
        path = nltk.data.find("corpora/omw-1.4.zip")
    except LookupError:
        print("[lexicon] downloading NLTK omw-1.4 (translations, one time)...")
        nltk.download("omw-1.4", quiet=True)
        try:
            path = nltk.data.find("corpora/omw-1.4.zip")
        except LookupError as exc:
            raise SystemExit("[lexicon] omw-1.4 is unavailable; run: python -m nltk.downloader omw-1.4") from exc
    return zipfile.ZipFile(str(path))


def collect_foreign(wn, have: set[str], languages: tuple[str, ...] | None = None,
                    min_languages: int | None = None) -> list[tuple[str, str, float, str, str]]:
    """Foreign-language rows (word, pos, zipf, 'foreign', gloss), most widely shared words first."""
    from wordfreq import zipf_frequency

    langs = languages or tuple(config.LEXICON_LANGUAGES)
    need = config.LEXICON_FOREIGN_MIN_LANGUAGES if min_languages is None else min_languages
    keep_lex = set(config.LEXICON_FOREIGN_LEXNAMES)
    block = config.WORD_BLOCKLIST
    min_len, max_len = config.LEXICON_MIN_LEN, config.LEXICON_FOREIGN_MAX_LEN

    english: dict[str, object] = {}                     # "00001740-n" -> synset, concept categories only
    for syn in wn.all_synsets():
        if syn.lexname() in keep_lex:
            english[f"{syn.offset():08d}-{'a' if syn.pos() == 's' else syn.pos()}"] = syn

    z = _omw_zip()
    hits: dict[str, dict[str, list]] = {}               # word -> language -> synsets
    for code in langs:
        folder = config.LEXICON_LANGUAGES[code][0]
        member = f"omw-1.4/{folder}/wn-data-{code}.tab"
        try:
            text = z.read(member).decode("utf-8")
        except KeyError:
            print(f"[lexicon] no OMW data for {code}; skipped")
            continue
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) < 3 or parts[1] not in ("lemma", f"{code}:lemma"):
                continue
            w = parts[2]
            if not _ALPHA.match(w) or not (min_len <= len(w) <= max_len) or w in have or w in block:
                continue
            syn = english.get(parts[0])
            if syn is not None:
                hits.setdefault(w, {}).setdefault(code, []).append(syn)

    rows = []
    for w, by_lang in hits.items():
        if any(b in w for b in config.COIN_BLOCK_SUBSTRINGS):
            continue
        ok = [c for c in by_lang if zipf_frequency(w, config.LEXICON_LANGUAGES[c][1]) >= config.LEXICON_FOREIGN_ZIPF_MIN]
        if len(ok) < need or zipf_frequency(w, "en") >= 2.0:
            continue
        if phonetics.analyze(w).score < config.COIN_MIN_PHONETIC:
            continue
        # the sense most languages agree on names the meaning
        votes = Counter(s for c in ok for s in set(by_lang[c]))
        syn = votes.most_common(1)[0][0]
        eng = sorted((n for n in syn.lemma_names() if _ALPHA.match(n)),
                     key=lambda n: -zipf_frequency(n, "en"))[:2] or syn.lemma_names()[:1]
        if any(SequenceMatcher(None, w, n.lower()).ratio() >= config.LEXICON_FOREIGN_MAX_SIMILARITY
               for n in syn.lemma_names()):
            continue                                    # a respelling of the English word, not a new root
        names = [config.LEXICON_LANGUAGES[c][2]
                 for c in sorted(ok, key=lambda c: -zipf_frequency(w, config.LEXICON_LANGUAGES[c][1]))]
        who = "/".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
        meaning = f"{who}: '{', '.join(eng)}'"
        pos = "n" if syn.pos() == "n" else "v" if syn.pos() == "v" else "a"
        top_z = max(zipf_frequency(w, config.LEXICON_LANGUAGES[c][1]) for c in ok)
        rows.append((len(ok), top_z, (w, pos, round(top_z, 2), "foreign", meaning[:GLOSS_MAX])))
    rows.sort(key=lambda t: (-t[0], -t[1], t[2][0]))
    return [r[2] for r in rows]


def describe(rows: list[tuple[str, str, float, str, str]], sample: int = 30) -> str:
    import random
    lines = []
    by_layer = Counter(r[3] for r in rows)
    lines.append("  " + " | ".join(f"{k}: {by_layer[k]}" for k in LAYERS if by_layer[k]))
    rnd = random.Random(1)
    for layer in LAYERS:
        pool = [r for r in rows if r[3] == layer]
        if not pool:
            continue
        pick = rnd.sample(pool, min(sample, len(pool)))
        lines.append(f"\n  {layer} sample:")
        if layer == "foreign":
            lines.append("    " + "; ".join(f"{r[0]} ({r[4]})" for r in pick[:12]))
        else:
            lines.append("    " + ", ".join(f"{r[0]}" for r in pick))
    return "\n".join(lines)


def run(layers: tuple[str, ...] = DEFAULT_LAYERS, dry_run: bool = False, limit: int | None = None,
        db_path=None, languages: tuple[str, ...] | None = None, min_languages: int | None = None) -> int:
    wn = core_words._load_wordnet()
    if wn is None:
        raise SystemExit("[lexicon] WordNet is required (pip install nltk; it downloads once)")
    with db.session(db_path) as conn:
        have = {r[0] for r in conn.execute("SELECT word FROM words")}
    rows = collect(wn, layers, have)
    if "foreign" in layers:
        rows += collect_foreign(wn, have | {r[0] for r in rows}, languages, min_languages)
    if limit:
        rows = rows[:limit]
    print(f"[lexicon] {len(rows)} new candidate words not already in the database")
    print(describe(rows))
    if dry_run:
        calls = -(-len(rows) // 20)
        print(f"\n[lexicon] dry run: nothing written. Scoring them takes about {calls} root-scoring calls "
              f"(20 words each).")
        return 0
    with db.session(db_path) as conn:
        added = db.upsert_lexicon(conn, rows, _now())
    print(f"\n[lexicon] added {added} words. Next: namefinder score --mode root --limit {added}")
    return added
