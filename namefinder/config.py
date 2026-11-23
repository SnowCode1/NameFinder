"""Central configuration: paths, weights, endpoints, question version.

Nothing here prints or logs secrets. Keys are read by name at call time via
:func:`secret`, which returns None when unset so stages can fail with a clear
message instead of a traceback.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- .env ------------------------------------------------------------------
# Load keys from .env if python-dotenv is available; never echo the file.
def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(ROOT / ".env")


_load_env()


# --- paths -----------------------------------------------------------------
DATA_DIR = ROOT / "data"
RESULTS_DIR = ROOT / "results"
IPGOD_DIR = DATA_DIR / "ipgod"
PROMPTS_DIR = ROOT / "prompts"
DB_PATH = Path(os.getenv("NAMEFINDER_DB") or (DATA_DIR / "namefinder.db"))
if not DB_PATH.is_absolute():
    DB_PATH = ROOT / DB_PATH


def load_prompt(filename: str, default: str) -> str:
    """Load prompt from prompts/<filename>, falling back to .example, then default."""
    p = PROMPTS_DIR / filename
    if p.exists():
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    p_ex = PROMPTS_DIR / (filename.replace(".md", ".example.md").replace(".txt", ".example.txt"))
    if p_ex.exists():
        try:
            return p_ex.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return default.strip()


def load_concept_roots(default: tuple[str, ...]) -> tuple[str, ...]:
    """Load concept roots from prompts/concept_roots.txt or fallback."""
    p = PROMPTS_DIR / "concept_roots.txt"
    if not p.exists():
        p = PROMPTS_DIR / "concept_roots.example.txt"
    if p.exists():
        try:
            lines = [line.strip().lower() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
            if lines:
                return tuple(lines)
        except OSError:
            pass
    return default


# --- secrets ---------------------------------------------------------------
TYPESAFE_KEY_ENV = "TYPESAFE_API_KEY"
OPENROUTER_KEY_ENV = os.getenv("NAMEFINDER_LLM_KEY_ENV") or "OPENROUTER_API_KEY"
MARKBASE_KEY_ENV = "MARKBASE_API_KEY"
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"

WRITE_KEY_HINT = "add it to .env (see .env.example); keys are never printed"


def secret(name: str) -> str | None:
    """Return an env var value, or None. Never logs the value."""
    val = os.getenv(name)
    if val is None:
        return None
    val = val.strip()
    if not val or val.lower().startswith(("<", "your", "paste", "***")):
        return None
    return val


# --- product context (shared Jev state, per the TypeSafe skill) ------------
PRODUCT_NAME = "the product"
# The real product brief is private: prompts/product.md (gitignored), else product.example.md.
DEFAULT_PRODUCT_BLURB = (
    "A software verification tool: a record of where an image first appeared and every copy and edit since. "
    "The voice is technical and plain, with no hype; names should sound like a sober technical term."
)
PRODUCT_BLURB = load_prompt("product.md", DEFAULT_PRODUCT_BLURB)


# --- stage 1: wordlist -----------------------------------------------------
WORD_MIN_LEN = 4
WORD_MAX_LEN = 8
WORD_TARGET = 12000          # aim for 10k-15k
ZIPF_MIN = 2.60              # drop ultra-obscure words nobody can spell
ZIPF_MAX = 5.55              # drop the ultra-common stop-word band
STOPWORD_DEPTH = 500         # always exclude the top-N most frequent words
POS_KEEP = ("n", "v")        # WordNet parts of speech we keep
# Endings that mark an inflection or a nominalisation rather than a nameable
# thing. Plurals are handled separately in words.py (stem-in-vocabulary test),
# so a good word like "canvas" or "atlas" survives while "sifts" does not.
SUFFIX_DROP = (
    "ings", "ing", "edly", "ed", "ness", "tions", "tion", "sions", "sion",
    "ments", "ment", "ance", "ence", "ities", "ity", "ative", "itive",
    "ical", "ious", "ous", "ism", "ist", "ish", "ify", "fy", "ia", "io",
    "ly", "ies",
)

# Extra base-word layers (namefinder/lexicon.py)
LEXICON_ZIPF_MIN = 1.5       # rarest word the jargon layers consider
LEXICON_MIN_PHONETIC = 0.55  # pronounceability floor; lower than COIN_MIN_PHONETIC so 4-syllable words (perplexity) pass
# Rare words with no subject-domain tag still join the jargon layer when WordNet files a noun sense under
# one of these abstract categories and they are at least this frequent (perplexity, antecedent).
LEXICON_ABSTRACT_LEXNAMES = ("noun.cognition", "noun.attribute", "noun.state", "noun.relation",
                             "noun.communication", "noun.process", "noun.event", "noun.phenomenon")
LEXICON_ABSTRACT_ZIPF_MIN = 2.0
# Foreign layer: OMW wordnet translations of abstract English concepts into these languages
# (OMW code -> (OMW archive folder, wordfreq code, display name)). A word must be common in its language
# (wordfreq zipf >= LEXICON_FOREIGN_ZIPF_MIN), not an English word, and found in at least
# LEXICON_FOREIGN_MIN_LANGUAGES of them (shared Latinate roots: fase, pausa, consulta; single-language
# words are mostly Dutch or Icelandic oddities).
LEXICON_LANGUAGES = {
    "ita": ("ita", "it", "Italian"), "fra": ("fra", "fr", "French"), "spa": ("mcr", "es", "Spanish"),
    "por": ("por", "pt", "Portuguese"), "ron": ("ron", "ro", "Romanian"), "cat": ("mcr", "ca", "Catalan"),
    "nld": ("nld", "nl", "Dutch"), "swe": ("swe", "sv", "Swedish"), "dan": ("dan", "da", "Danish"),
    "nob": ("nor", "nb", "Norwegian"), "isl": ("isl", "is", "Icelandic"),
}
LEXICON_FOREIGN_MAX_LEN = 8             # foreign roots stay short (the core wordlist's own limit)
LEXICON_FOREIGN_ZIPF_MIN = 3.0
LEXICON_FOREIGN_MAX_SIMILARITY = 0.8    # drop words this close in spelling to their English meaning (kontroll, strategi)
LEXICON_FOREIGN_MIN_LANGUAGES = 2
# English-side WordNet categories whose translations are worth considering (concepts, not animals or food)
LEXICON_FOREIGN_LEXNAMES = (
    "noun.cognition", "noun.communication", "noun.relation", "noun.attribute", "noun.state", "noun.process",
    "noun.event", "noun.phenomenon", "noun.act", "noun.time", "noun.shape", "noun.group", "noun.possession",
    "verb.cognition", "verb.communication", "verb.change", "verb.social", "verb.stative", "verb.creation",
    "verb.possession",
)
LEXICON_MIN_LEN = 4
LEXICON_MAX_LEN = 11         # core wordlist stops at 8; abstract nouns (perplexity, provenance) run longer
# WordNet subject domains whose words never suit a software name. A word is dropped only when
# every domain it carries is in this list ("law" or "logic" senses keep it).
LEXICON_SKIP_DOMAINS = (
    "sport", "baseball", "american_football", "football", "basketball", "golf", "tennis", "cricket",
    "boxing", "fencing", "wrestling", "racing", "hunting", "fishing", "yiddish", "cooking", "music",
    "botany", "plant", "zoology", "animal", "anatomy", "physiology", "pathology", "medicine",
    "christianity", "heraldry", "farming", "fashion", "dance", "theater", "card_games",
)

# Every lexicon layer skips endings that make poor bases: agent nouns (-ist), qualities (-ness, -ful, -less),
# adjective forms. The `reinstated` layer also takes nouns and verbs only (adjectives like "stylish" add nothing).
LEXICON_DROP_ENDINGS = ("ness", "ist", "ful", "less", "able", "ible", "ish", "ious", "ally", "ize", "ise", "ian")

# Hard blocklist: AI/SaaS cliches, abstract corporate filler, and words whose
# everyday meaning is too crude or too strongly attached to another product.
WORD_BLOCKLIST = frozenset("""
aiify appify automateify botify cloudify datafy digitizeify
synergy synergetic disruptivify ideation leverageify optimizify
chatgpt copilot gemini claude cortex neural nexus paradigm qubit
porn crude sexist racial slur
""".split())


# --- stage 2: brand scoring ------------------------------------------------
DEFAULT_MODEL = "z-ai/glm-5.3-flash"
SCORE_MODEL = os.getenv("OPENROUTER_MODEL") or DEFAULT_MODEL
JEV_MODEL = os.getenv("TYPESAFE_MODEL") or "jev-latest"
# Bump this whenever questions/weights change: cached scores stop matching.
QUESTION_VERSION = "score-2026-11-16.3"
SCORE_CONCURRENCY = 6
SCORE_TOP_N = 500
JEV_CONCURRENCY = SCORE_CONCURRENCY
JEV_TOP_N = SCORE_TOP_N

# Composite = weighted sum of Noul probabilities, then multiplied by the
# cleanliness gate. Retune freely - it is pure code, no re-scoring needed.
WEIGHTS = {
    "fit": 0.30,        # evokes discovery / tracing / quiet utility
    "natural": 0.20,    # reads as a real, serious word
    "spellable": 0.15,  # written correctly after hearing it once
    "memorable": 0.15,  # Score, normalised to 0..1
    "novelty": 0.20,    # not already a known product / place / person name
}
# "Verbability": does it work as a verb in "I just ___ it and found what I needed"
# (google it, ping it, grep it)? Applied as a slight multiplicative bonus on brand
# composites: composite *= (1 - VERB_BONUS) + VERB_BONUS * verbable (so 0.92..1.0).
# Unrated words count as 0.5. 0 disables it.
VERB_BONUS = 0.08
TAKEN_DAMP = 0.75     # weight of "already a known software brand"
COMMON_NAME_DAMP = 0.50


# Pronounceability and distinctiveness (applied to coined words only)
PHONETIC_WEIGHT = 0.30       # how hard a perfectly unpronounceable word is penalised
GENERIC_PREFIX_PENALTY = {   # prefix -> composite multiplier reduction
    "meta": 0.08, "pan": 0.08, "ultra": 0.08, "omni": 0.06, "pro": 0.06,
    "inter": 0.05, "syn": 0.04, "neo": 0.05, "sub": 0.04, "co": 0.04,
    "con": 0.03, "re": 0.03, "in": 0.03, "per": 0.03, "epi": 0.03,
}
# Brevity: spoken brand names win at one or two syllables. Composite multiplier
# reduction by syllable count (the largest key <= the count applies). {} disables it.
SYLLABLE_PENALTY = {3: 0.04, 4: 0.10}
# Refine pass (second pass over the top coined words)
REFINE_MODEL = os.getenv("OPENROUTER_REFINE_MODEL") or SCORE_MODEL
TOURNAMENT_MODEL = os.getenv("OPENROUTER_TOURNAMENT_MODEL") or SCORE_MODEL   # judge for `tournament`; ratings are kept per model
IDEAS_MODEL = os.getenv("OPENROUTER_IDEAS_MODEL") or SCORE_MODEL            # idea generation (`ideas generate`)
NAMEGEN_MODEL = os.getenv("OPENROUTER_NAMEGEN_MODEL") or SCORE_MODEL        # names from ideas and variants (`generate`, `evolve`)
TOP_SINCE = "10m"       # `top --since` default: window for NEW/REF marks
REFINE_TOP_N = 100
RESCORE_TOP_N = 10000   # `rescore`: top coined names brought up to the current brand prompt
REFINE_SAMPLES = 3


# --- stage 3: domains ------------------------------------------------------
TLDS = ("io", "dev")        # checked as <word>.<tld>
USE_PREFIX = ""             # set to "use" to also check use<word>.com
CHECK_PLAIN_COM = True      # <word>.com


# Domains `generate` and `evolve` check for new names. .com only by default: it is what the tournament
# pool needs, and it is the fast registry (.dev goes through Google's, capped at 2 requests/s).
# `--all-tlds` on those commands checks every active role instead.
GENERATION_DOMAIN_ROLES = ("com",)


def active_domain_roles() -> tuple[str, ...]:
    """Domain roles currently being checked; older cached rows for other roles are ignored."""
    roles = list(TLDS)
    if USE_PREFIX:
        roles.append("use-com")
    if CHECK_PLAIN_COM:
        roles.append("com")
    return tuple(roles)


RDAP_ENDPOINTS = {
    "com": "https://rdap.verisign.com/com/v1/domain/{domain}",
    "dev": "https://pubapi.registry.google/rdap/domain/{domain}",
    "app": "https://pubapi.registry.google/rdap/domain/{domain}",
    "io": "https://rdap.identitydigital.services/rdap/domain/{domain}",
}
REGISTRY_BY_TLD = {"com": "verisign", "dev": "google", "app": "google", "io": "identitydigital"}
RDAP_RATE_PER_SEC = 2.0     # default cap per registry
# Measured: Verisign (.com) handled 10 req/s cleanly; Google's registry (.dev/.app)
# returned 429s at 3 req/s, so it stays at 2.
# Identity Digital (.io) measured clean at 8 req/s.
RDAP_RATE_BY_REGISTRY = {"verisign": 8.0, "google": 2.0, "identitydigital": 8.0}
# Coining quality gates and learning
COIN_MIN_PHONETIC = 0.70     # reject coined candidates below this pronounceability score
# Unambiguous crude/embarrassing fragments (checked as substrings). Deliberately
# short: fragments like "ass" or "cum" would reject ordinary words.
COIN_BLOCK_SUBSTRINGS = (
    "rectum", "anus", "porn", "fuck", "shit", "piss", "cunt", "slut", "whore",
    "nazi", "rape", "dildo", "penis", "vagina", "sexy", "nigg",
)
COIN_BLOCK_EDGES = ("sex",)  # blocked only at the start or end of a word
# Wordlikeness floor (namefinder/wordlike.py): reject names less wordlike than this percentile of held-out
# real words. A safety net against gibberish only. Measured on 67k scored names, wordlikeness does not
# predict a top score (the most wordlike decile has about half the tail rate of the least), so it is not
# used to bias sampling. 0 disables it.
COIN_MIN_WORDLIKE_PCT = 0.02
# Endings for the suffix and trunc engines, chosen by measured tail-rate lift (`namefinder engines --lift ending`):
# -ex 3.5x, -um 2.6x, -os 2.1x, -ia 2.0x, -is 1.7x, -ix 1.6x, -us 1.3x. Dropped for lift under 1x:
# -or, -ar, -el, -en, -ic, -an, -ax, -ox. Re-run the report and revise this list as scoring data grows.
COIN_TWIST_PHONETIC_SLACK = 0.03        # twist: the edit may score at most this far below its winner's phonetic score
COIN_TWIST_POOL = 300                   # twist engine: it edits names from the best N coined composites
COIN_TRUNC_MIN_BASE = 7      # trunc only clips bases at least this long (short bases lose their meaning)
COIN_TRUNC_MIN_CLIP = 3      # ...and keeps at least this many letters, in whole syllables (casix = cas + ix);
                             # clips are drawn with weight length^2, so 3-letter clips are the rarest
COIN_COMPOUND_MAX_PART = 6   # compound engine: each whole word is at most this long
COIN_SUFFIXES = ("ex", "um", "os", "ia", "is", "ix", "us", "on", "am", "im", "x")
# Verb-likeness steering for the coining engines
COIN_VERB_PARENT_BOOST = 1.0   # verb-like parents are up to (1 + boost)x as likely to be picked
COIN_VERB_FLOOR = 0.35         # a candidate with verb_score 0 is kept with this probability (1.0 = no bias)
COIN_VERB_SPLICE_WEIGHT = 0.30  # weight of verb_score when choosing among splice options
COIN_LEARN_VERB = 1.0          # engine learning scales a composite by 1 + this * (verbable - 0.5)
COIN_LEARN_PRIOR = 10        # pseudo-observations pulling per-engine/partner means toward the overall mean
COIN_EXPLORE = 0.30          # share of engine sampling kept uniform so no engine is starved
# LLM coining engine (namefinder/llmcoin.py)
COIN_MODEL = os.getenv("OPENROUTER_COIN_MODEL") or SCORE_MODEL
COIN_LLM_SHARE = 0.30          # fraction of each continuous cycle that comes from the LLM engine (0 = off)
COIN_LLM_PER_NAME_COUNT = 8    # names requested per parent word
COIN_LLM_PARENTS_PER_REQUEST = 6
COIN_LLM_FOCUS_PER_NAME_COUNT = 16  # names requested per parent when the loop is focused on a few roots
COIN_LLM_TEMPERATURE = 0.9
COIN_LLM_FEEDBACK = 6          # best/worst earlier LLM names shown to the model as style feedback
COIN_NEAR_DUP = True                 # reject coined names one letter from an existing coined name...
COIN_NEAR_DUP_KEEP_QUANTILE = 0.95   # ...unless that name scored in the top 5% (variations on winners stay)
COIN_BATCH = 120            # coined words per continuous cycle

# Base-word bandit (namefinder/bases.py): which bases tend to produce top-tier names
BASE_BANDIT = True           # weight base sampling by each base's track record (False = rank prior only)
BASE_TAIL_QUANTILE = 0.95    # a "hit" is a coined name in the top 5% of all coined composites
BASE_PRIOR = (1, 19)         # Beta prior pseudo-counts (hits, misses): prior hit rate 5%, worth 20 names
BASE_WINDOW = 150            # bandit judges each base on its most recent N names, so exhausted bases fade
BASE_MULT_CAP = 20.0         # largest weight multiplier a single base can draw
BASE_SPOKEN_ONLY = True      # judge bases only on names the LLM spelled out (pass-1 spoken review or refine)
BASE_SPOKEN_MIN_NAMES = 500  # ...but use all names until this many spoken-reviewed names with a base exist
BASE_RANK_FADE = 50         # names after which the rank prior keeps half its (log) influence on a base
COIN_CONCURRENCY = 20        # parallel scoring / LLM-coining requests per cycle (429s back off automatically)
COIN_DOMAIN_TOP = 0.25       # fraction of each cycle (best scores) that gets a domain check
DOMAIN_STALE_DAYS = 7


# --- stage 4: trademarks ---------------------------------------------------
NICE_CLASSES = ("9", "42", "35", "41")   # software, SaaS/AI/research, data/search, education
NICE_CLASSES_PADDED = tuple(c.zfill(3) for c in NICE_CLASSES)
MARKBASE_URL = "https://api.markbase.co"
MARKBASE_RATE_PER_MIN = 24          # stay under the documented 25/min
MARKBASE_MONTHLY_BUDGET = 1000      # free tier; each name consumes one
MARKBASE_RISK_BLOCK = ("medium", "high")
MARKBASE_MATCH_LIMIT = 30

# USPTO/TSDR status codes: 6xx abandoned, 78x/79x cancelled, 9xx expired,
# 7xx registered/renewed, 8xx registered, 1xx-5xx pending/in prosecution.
DEAD_STATUS_PREFIXES = ("6", "9")
DEAD_STATUS_CODES = frozenset({"780", "781", "782", "783", "784", "790", "791"})

GITHUB_MIN_STARS = 2
PYPI_TIMEOUT = 15

TRADEMARK_STALE_DAYS = 30


# --- stage 5: final tagging ------------------------------------------------
# Any OpenAI-compatible endpoint can replace OpenRouter for every LLM stage: set
# NAMEFINDER_LLM_URL (e.g. https://inference-api.nousresearch.com/v1) and
# NAMEFINDER_LLM_KEY_ENV (name of the .env variable holding that provider's key).
# OpenRouter-only request fields (reasoning, provider routing) are sent only to openrouter.ai.
OPENROUTER_URL = (os.getenv("NAMEFINDER_LLM_URL") or "https://openrouter.ai/api/v1").rstrip("/")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL") or DEFAULT_MODEL
# Generation stages (`ideas`, `generate`, `evolve`, LLM coining in `coin`) can use their own
# endpoint while scoring, refine and the tournament stay on the one above:
# NAMEFINDER_GEN_URL and NAMEFINDER_GEN_KEY_ENV (same meaning as the two settings above).
GEN_URL = (os.getenv("NAMEFINDER_GEN_URL") or OPENROUTER_URL).rstrip("/")
GEN_KEY_ENV = os.getenv("NAMEFINDER_GEN_KEY_ENV") or OPENROUTER_KEY_ENV
# Optional OpenRouter routing. OPENROUTER_REASONING: "low" (default; also minimal,
# medium, high) sets reasoning effort, "off" sends enabled=false (some endpoints
# reject this), "on" leaves the model default. Pin providers with OPENROUTER_PROVIDER
# (comma-separated names, no fallbacks).
OPENROUTER_PROVIDER = os.getenv("OPENROUTER_PROVIDER", "").strip()
OPENROUTER_REASONING = os.getenv("OPENROUTER_REASONING", "low").strip().lower()
# Without a pinned provider, route each request to the cheapest provider that serves the model
# (OpenRouter `provider.sort`). Measured on the tournament judge: about 3x cheaper per call with
# no measurable loss in consistency. Set OPENROUTER_SORT=off for OpenRouter's default balancing.
OPENROUTER_SORT = os.getenv("OPENROUTER_SORT", "price").strip().lower()


def openrouter_extras(url: str) -> dict:
    """Extra request-body fields for OpenRouter; empty for local servers."""
    if "openrouter.ai" not in url:
        # Other hosted providers (e.g. Nous): thinking models burn max_tokens on reasoning and
        # return an empty answer, so switch it off. NAMEFINDER_REASONING=on keeps the default.
        if "localhost" in url or "127.0.0.1" in url or os.getenv("NAMEFINDER_REASONING", "off").strip().lower() == "on":
            return {}
        return {"reasoning": {"enabled": False}}
    extras: dict = {}
    if OPENROUTER_REASONING == "off":
        extras["reasoning"] = {"enabled": False}
    elif OPENROUTER_REASONING != "on":
        extras["reasoning"] = {"effort": OPENROUTER_REASONING}
    if OPENROUTER_PROVIDER:
        names = [n.strip() for n in OPENROUTER_PROVIDER.split(",") if n.strip()]
        extras["provider"] = {"order": names, "allow_fallbacks": False}
    elif OPENROUTER_SORT and OPENROUTER_SORT not in ("off", "none", "default"):
        extras["provider"] = {"sort": OPENROUTER_SORT}
    return extras


# Per-command spend cap in USD for paid API calls (0 disables). Override with --max-cost.
MAX_COST = float(os.getenv("NAMEFINDER_MAX_COST", "1.0"))
OPENROUTER_BATCH = 25        # words per call; keeps us at 1-2 calls total
MAX_FINAL = 50
