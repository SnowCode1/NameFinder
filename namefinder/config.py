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
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
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
TOP_SINCE = "10m"       # `top --since` default: window for NEW/REF marks
REFINE_TOP_N = 100
RESCORE_TOP_N = 10000   # `rescore`: top coined names brought up to the current brand prompt
REFINE_SAMPLES = 3


# --- stage 3: domains ------------------------------------------------------
TLDS = ("io", "dev")        # checked as <word>.<tld>
USE_PREFIX = ""             # set to "use" to also check use<word>.com
CHECK_PLAIN_COM = True      # <word>.com


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
OPENROUTER_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL") or DEFAULT_MODEL
# Optional OpenRouter routing. OPENROUTER_REASONING: "low" (default; also minimal,
# medium, high) sets reasoning effort, "off" sends enabled=false (some endpoints
# reject this), "on" leaves the model default. Pin providers with OPENROUTER_PROVIDER
# (comma-separated names, no fallbacks).
OPENROUTER_PROVIDER = os.getenv("OPENROUTER_PROVIDER", "").strip()
OPENROUTER_REASONING = os.getenv("OPENROUTER_REASONING", "low").strip().lower()


def openrouter_extras(url: str) -> dict:
    """Extra request-body fields for OpenRouter; empty for local servers."""
    if "openrouter.ai" not in url:
        return {}
    extras: dict = {}
    if OPENROUTER_REASONING == "off":
        extras["reasoning"] = {"enabled": False}
    elif OPENROUTER_REASONING != "on":
        extras["reasoning"] = {"effort": OPENROUTER_REASONING}
    if OPENROUTER_PROVIDER:
        names = [n.strip() for n in OPENROUTER_PROVIDER.split(",") if n.strip()]
        extras["provider"] = {"order": names, "allow_fallbacks": False}
    return extras


# Per-command spend cap in USD for paid API calls (0 disables). Override with --max-cost.
MAX_COST = float(os.getenv("NAMEFINDER_MAX_COST", "1.0"))
OPENROUTER_BATCH = 25        # words per call; keeps us at 1-2 calls total
MAX_FINAL = 50
