"""Command line interface: one subcommand per stage so costs stay inspectable."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import budget, config, console, db


_MAX_COST_HELP = (
    f"stop issuing paid API requests once this many USD are spent in this command "
    f"(default: ${config.MAX_COST:.2f}; 0 = no limit)"
)


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", type=Path, default=None, help="override the SQLite path")
    p.add_argument("--limit", type=int, default=None,
                   help="cap how many words this stage processes (use it for dry runs)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="namefinder",
        description="Resumable brand discovery, domain screening, trademark clearance, and neologism pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Standard Workflows:

  1. Score Dictionary Base Words:
     namefinder prepare                         # stage 1: ingest & filter English dictionary (~11k words)
     namefinder score --backend local --limit 100 # score top roots locally (LM Studio on port 1234)
     namefinder score --backend openrouter      # or score via OpenRouter cloud

  2. Continuous Neologism Coining & Discovery:
     namefinder coin -c --backend local         # autonomous stream: rank-weighted sampling -> coin -> RDAP -> score
     namefinder coin --limit 30 --backend local # single batch of 30 independently sampled variations
     namefinder coin --bases fathom,spectral    # coin variations from specific root words

  3. Screen Domains & Trademarks Directly:
     namefinder check-domains --top-n 500       # check .com, .io, .dev via keyless RDAP
     namefinder check-trademarks --top-n 100    # check USPTO (Markbase), PyPI, GitHub

  4. Inspect Rankings & Export:
     namefinder top --free-domains              # view top brands with verified available domains
     namefinder top --clean                     # view top brands clear of trademark conflicts
     namefinder bases                           # which base words produce top-tier coined names
     namefinder rank-final --skip-llm           # export shortlist.md / shortlist.csv to results/
     namefinder status                          # view pipeline cache & database status
     namefinder score --clear                   # wipe cached scores to start fresh
""",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("prepare", help="stage 1: build the clean wordlist")
    _add_common(p1)
    p1.add_argument("--refresh", action="store_true", help="clear cached words and rebuild the wordlist")
    p1.add_argument("--target", type=int, default=None, help="target number of words (default: 12000)")
    p1.add_argument("--scan", type=int, default=90000, help="how many frequent words to scan")
    p1.add_argument("--no-pos", action="store_true", help="skip the WordNet noun/verb filter")

    # Primary Stage 2 command: score
    p2 = sub.add_parser(
        "score",
        help="stage 2: brand resonance scoring (local LLM / OpenRouter)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Scoring Examples:
  # Score locally using LM Studio (runs at ~2s/word with early-out):
  namefinder score --backend local --limit 100

  # Score using OpenRouter cloud:
  namefinder score --backend openrouter --limit 100

  # Score ONLY candidates that already have a free domain:
  namefinder score --backend local --free-domains-only

  # Clear cached scores without re-running:
  namefinder score --clear
""",
    )
    _add_common(p2)
    p2.add_argument("--backend", choices=["openrouter", "local", "lmstudio", "typesafe"], default=None,
                    help="scoring backend (default: openrouter; 'local' for LM Studio)")
    p2.add_argument("--mode", choices=["auto", "root", "brand"], default="auto",
                    help="scoring mode: 'root' for base conceptual potency, 'brand' for standalone names (default: auto)")
    p2.add_argument("--max-cost", type=float, default=None, help=_MAX_COST_HELP)
    p2.add_argument("--clear", action="store_true",
                    help="back up the database, then delete all scores and coined words (no re-scoring)")
    p2.add_argument("--free-domains-only", action="store_true",
                    help="score ONLY words that already have a free domain")
    p2.add_argument("--top-n", type=int, default=config.SCORE_TOP_N,
                    help="how many top words to carry into stage 3")
    p2.add_argument("--refresh", action="store_true", help="re-score words already cached")
    p2.add_argument("--concurrency", type=int, default=config.SCORE_CONCURRENCY)
    p2.add_argument("--model", default=None, help="model id override")
    p2.add_argument("--sample", type=int, default=25, help="how many top rows to print")

    # Alias for backwards compatibility
    p2_alias = sub.add_parser("score-jev", help=argparse.SUPPRESS)
    _add_common(p2_alias)
    p2_alias.add_argument("--backend", choices=["openrouter", "typesafe"], default=None)
    p2_alias.add_argument("--top-n", type=int, default=config.SCORE_TOP_N)
    p2_alias.add_argument("--refresh", action="store_true")
    p2_alias.add_argument("--concurrency", type=int, default=config.SCORE_CONCURRENCY)
    p2_alias.add_argument("--model", default=None)
    p2_alias.add_argument("--sample", type=int, default=25)

    p3 = sub.add_parser("check-domains", help="stage 3: RDAP domain availability")
    _add_common(p3)
    p3.add_argument("--top-n", type=int, default=config.SCORE_TOP_N)
    p3.add_argument("--refresh", action="store_true", help="ignore cached RDAP answers")

    p4 = sub.add_parser("check-trademarks", help="stage 4: USPTO clearance + repo collision screening")
    _add_common(p4)
    p4.add_argument("--top-n", type=int, default=None, help="max candidates to check")
    p4.add_argument("--refresh", action="store_true")
    p4.add_argument("--no-markbase", action="store_true", help="skip USPTO")
    p4.add_argument("--no-ipgod", action="store_true", help="skip IP Australia local CSVs")
    p4.add_argument("--no-pypi", action="store_true")
    p4.add_argument("--no-github", action="store_true")
    p4.add_argument("--word", action="append", default=None, metavar="WORD",
                    help="screen this name instead of the old pipeline's survivors (repeatable)")
    p4.add_argument("--tournament", type=int, default=None, metavar="N",
                    help="screen the top N names by tournament rating instead of the old pipeline's survivors")

    p5 = sub.add_parser("rank-final", help="stage 5: tag and write the shortlist")
    _add_common(p5)
    p5.add_argument("--refresh", action="store_true")
    p5.add_argument("--model", default=None, help="OpenRouter model id")
    p5.add_argument("--skip-llm", action="store_true",
                    help="write the report from cached data without calling OpenRouter")

    p_ref = sub.add_parser(
        "refine",
        help="pass 2: stabilise the top coined words by re-sampling the spoken-name review and averaging",
    )
    _add_common(p_ref)
    p_ref.add_argument("--top-n", type=int, default=config.REFINE_TOP_N,
                       help=f"how many top coined words to review (default: {config.REFINE_TOP_N})")
    p_ref.add_argument("--samples", type=int, default=config.REFINE_SAMPLES,
                       help="independent reviews per word, averaged (default: %(default)s)")
    p_ref.add_argument("--backend", choices=["openrouter", "local", "lmstudio"], default=None)
    p_ref.add_argument("--model", default=None, help="model id override (default: OPENROUTER_REFINE_MODEL)")
    p_ref.add_argument("--concurrency", type=int, default=config.SCORE_CONCURRENCY,
                       help="parallel requests for cloud backends (default: %(default)s)")
    p_ref.add_argument("--max-cost", type=float, default=None, help=_MAX_COST_HELP)
    p_ref.add_argument("--refresh", action="store_true", help="re-review words already refined")
    p_ref.add_argument("--free-domains", "--free-domains-only", dest="free_domains_only", action="store_true",
                       help="review only coined words that have at least one free domain (same as `top --free-domains`)")
    p_ref.add_argument("--all-free", action="store_true",
                       help="review only coined words whose every checked domain is free (same as `top --all-free`)")
    p_ref.add_argument("--clean", action="store_true",
                       help="review only coined words clear of trademark conflicts (same as `top --clean`)")

    p_tour = sub.add_parser(
        "tournament",
        help="rank the best names against each other in groups of 5 (Elo), instead of scoring each alone",
    )
    _add_common(p_tour)
    p_tour.add_argument("--pool", type=int, default=300,
                        help="how many top coined names with a free domain to enter (default: %(default)s)")
    p_tour.add_argument("--rounds", type=int, default=6,
                        help="rounds to play now; each shows every name in two groups of 5 (default: %(default)s)")
    p_tour.add_argument("--ref", action="append", default=[], metavar="WORD",
                        help="reference name that always enters (repeatable; also read from prompts/tournament_refs.txt)")
    p_tour.add_argument("--tld", default="com", help="domain that must be free for pool names (default: %(default)s)")
    p_tour.add_argument("--model", default=None,
                        help="judge model (default: OPENROUTER_TOURNAMENT_MODEL, else the scoring model). "
                             "Ratings are kept per model and judge prompt.")
    p_tour.add_argument("--backend", choices=["openrouter", "local", "lmstudio"], default=None)
    p_tour.add_argument("--concurrency", type=int, default=config.SCORE_CONCURRENCY,
                        help="parallel requests for cloud backends (default: %(default)s)")
    p_tour.add_argument("--max-cost", type=float, default=None, help=_MAX_COST_HELP)
    p_tour.add_argument("--show", type=int, default=30, help="rows of the final ranking to print (default: %(default)s)")
    p_tour.add_argument("--show-only", action="store_true", help="print the stored ranking without calling the judge")
    p_tour.add_argument("--seed", type=int, default=None, help="seed for grouping, for repeatable runs")
    p_tour.add_argument("--source", choices=["score", "ideas", "both", "rated"], default="score",
                        help="where the pool comes from: top old-composite coined names (default); names made by "
                             "`generate`/`evolve` plus the best-rated names carried over as competition (ideas); "
                             "or everything already rated + the idea names + the top --pool old names, with the "
                             "old names tagged elite/bulk so the groups can be compared (both); or the top --pool "
                             "names already rated plus any unrated idea/variant names, to confirm the leaders (rated)")
    p_tour.add_argument("--prune-below", default=None, metavar="WORD",
                        help="stop playing names rated more than --margin below this name (e.g. origalia), once they "
                             "have --min-games games; reference names are never pruned")
    p_tour.add_argument("--prune-rank", type=int, default=100, metavar="N",
                        help="stop playing names that are clearly below the Nth best name: rating + --z standard "
                             "errors still under it (default: %(default)s; combined with --prune-below, the stricter "
                             "cut applies)")
    p_tour.add_argument("--no-prune", action="store_true", help="play every name in the pool every round")
    p_tour.add_argument("--z", type=float, default=2.0,
                        help="standard errors of benefit of the doubt before a name is pruned (default: %(default)s)")
    p_tour.add_argument("--margin", type=float, default=0.0,
                        help="extra rating points of slack below the pruning cut (default: %(default)s)")
    p_tour.add_argument("--min-games", type=int, default=4,
                        help="games a name needs before it can be pruned (default: %(default)s)")
    p_tour.add_argument("--group-size", type=int, default=8,
                        help="names per judged group (default: %(default)s; measured cheaper per comparison than 5)")
    p_tour.add_argument("--refit", action="store_true",
                        help="refit the ratings from the stored matches (no API calls), then show the ranking")
    p_tour.add_argument("--carry", type=int, default=60,
                        help="with --source ideas: how many of the best-rated names to carry in (default: %(default)s)")

    p_ideas = sub.add_parser("ideas", help="idea-first naming, step 1: generate ideas, review them, list them")
    ideas_sub = p_ideas.add_subparsers(dest="ideas_cmd", required=True)
    pi_gen = ideas_sub.add_parser("generate", help="have the model propose ideas (true statements + roots) per angle")
    _add_common(pi_gen)
    pi_gen.add_argument("--per-angle", type=int, default=5,
                        help="base ideas per angle, scaled by each angle's weight (default: %(default)s)")
    pi_gen.add_argument("--angle", action="append", default=None, metavar="NAME",
                        help="only this angle (repeatable): origin, spread, evidence, discovery, gaps, "
                             "synthesis, inquiry, time, alteration, exploration")
    pi_gen.add_argument("--no-merge", action="store_true",
                        help="do not merge near-duplicate ideas after generating")
    pi_gen.add_argument("--model", default=None, help="model id (default: OPENROUTER_IDEAS_MODEL, else the scoring model)")
    pi_gen.add_argument("--backend", choices=["openrouter", "local", "lmstudio"], default=None)
    pi_gen.add_argument("--concurrency", type=int, default=config.SCORE_CONCURRENCY)
    pi_gen.add_argument("--max-cost", type=float, default=None, help=_MAX_COST_HELP)
    pi_mrg = ideas_sub.add_parser("merge", help="merge near-duplicate pending ideas, combining their roots")
    _add_common(pi_mrg)
    pi_mrg.add_argument("--model", default=None, help="model id (default: OPENROUTER_IDEAS_MODEL)")
    pi_mrg.add_argument("--backend", choices=["openrouter", "local", "lmstudio"], default=None)
    pi_mrg.add_argument("--max-cost", type=float, default=None, help=_MAX_COST_HELP)
    pi_rev = ideas_sub.add_parser("review", help="walk through pending ideas: approve, reject or edit each")
    _add_common(pi_rev)
    pi_rev.add_argument("--status", default="pending", choices=["pending", "approved", "rejected"],
                        help="which ideas to walk through (default: %(default)s)")
    pi_list = ideas_sub.add_parser("list", help="show ideas with their roots and status")
    _add_common(pi_list)
    pi_list.add_argument("--status", default=None, choices=["pending", "approved", "rejected", "merged"])
    pi_mark = ideas_sub.add_parser("mark", help="approve or reject ideas by id without the interactive review")
    _add_common(pi_mark)
    pi_mark.add_argument("--approve", type=int, nargs="*", default=[], metavar="ID")
    pi_mark.add_argument("--reject", type=int, nargs="*", default=[], metavar="ID")

    p_gen = sub.add_parser("generate", help="idea-first naming, step 2: invent names from approved ideas")
    _add_common(p_gen)
    p_gen.add_argument("--per-idea", type=int, default=20, help="names requested per idea (default: %(default)s)")
    p_gen.add_argument("--no-feedback", action="store_true",
                       help="do not show the model the best-rated names and the judge's flaws (for A/B tests)")
    p_gen.add_argument("--idea", type=int, action="append", default=None, metavar="ID",
                       help="only this approved idea (repeatable)")
    p_gen.add_argument("--model", default=None, help="model id (default: OPENROUTER_NAMEGEN_MODEL, else the scoring model)")
    p_gen.add_argument("--backend", choices=["openrouter", "local", "lmstudio"], default=None)
    p_gen.add_argument("--concurrency", type=int, default=config.SCORE_CONCURRENCY)
    p_gen.add_argument("--min-len", type=int, default=5)
    p_gen.add_argument("--max-len", type=int, default=10)
    p_gen.add_argument("--no-domains", action="store_true", help="skip the domain check")
    p_gen.add_argument("--all-tlds", action="store_true",
                       help="check every configured domain, not just .com (much slower: .dev is rate-limited)")
    p_gen.add_argument("--max-cost", type=float, default=None, help=_MAX_COST_HELP)

    p_evo = sub.add_parser(
        "evolve",
        help="idea-first naming, step 3: improve the best-rated names using the tournament judge's critique",
    )
    _add_common(p_evo)
    p_evo.add_argument("--top", type=int, default=20, help="how many of the best-rated names to improve (default: %(default)s)")
    p_evo.add_argument("--variants", type=int, default=4, help="variants requested per name (default: %(default)s)")
    p_evo.add_argument("--no-refs", action="store_true", help="do not make variants of your reference names")
    p_evo.add_argument("--model", default=None, help="model id (default: OPENROUTER_NAMEGEN_MODEL, else the scoring model)")
    p_evo.add_argument("--judge", default=None, metavar="MODEL", help="whose tournament ratings to read (default: configured judge)")
    p_evo.add_argument("--backend", choices=["openrouter", "local", "lmstudio"], default=None)
    p_evo.add_argument("--concurrency", type=int, default=config.SCORE_CONCURRENCY)
    p_evo.add_argument("--min-len", type=int, default=5)
    p_evo.add_argument("--max-len", type=int, default=10)
    p_evo.add_argument("--no-domains", action="store_true", help="skip the domain check")
    p_evo.add_argument("--all-tlds", action="store_true",
                       help="check every configured domain, not just .com (much slower: .dev is rate-limited)")
    p_evo.add_argument("--max-cost", type=float, default=None, help=_MAX_COST_HELP)

    p_top = sub.add_parser("top", help="inspect candidate rankings directly in the terminal")
    _add_common(p_top)
    p_top.add_argument("--top", "-n", dest="top_count", type=int, default=25,
                       help="how many candidates to show (default: 25)")
    p_top.add_argument("--free-domains", action="store_true",
                       help="show only candidates with at least one free domain")
    p_top.add_argument("--all-free", action="store_true",
                       help="show only candidates whose every checked domain is free (unchecked words excluded)")
    p_top.add_argument("--clean", action="store_true",
                       help="show only candidates clear of trademark conflicts")
    p_top.add_argument("--spoken", action="store_true",
                       help="show only names with an LLM pronunciation review (spelled out in scoring or refine)")
    p_top.add_argument("--bases-only", action="store_true",
                       help="show only dictionary base words (exclude coined names)")
    p_top.add_argument("--by", choices=["score", "tournament"], default="score",
                       help="rank by the composite score (default) or by tournament rating (rated names only). "
                            "Either way, rated names show an Elo column")
    p_top.add_argument("--judge", default=None, metavar="MODEL",
                       help="whose tournament ratings to use (default: the configured tournament model, "
                            "else the latest rating set)")
    p_top.add_argument("--since", default=config.TOP_SINCE, metavar="AGE",
                       help="mark names scored (NEW) or refined (REF) within this window: s, m, h, d or w units, "
                            "combinable and spellable in full (30s, 10m, 1h30m, 6h, 2d, 1w, '2 days'); a bare "
                            "number is minutes; 'all' marks everything, 0/off turns marking off "
                            "(default: %(default)s)")
    p_top.add_argument("--new", dest="new_only", action="store_true",
                       help="show only names marked NEW or REF within the --since window")

    p_bases = sub.add_parser(
        "bases",
        help="rank base words by how often their coined names reach the top tier (what the coin bandit uses)",
    )
    _add_common(p_bases)
    p_bases.add_argument("--top", "-n", dest="top_count", type=int, default=30,
                         help="how many bases to show (default: %(default)s)")
    p_bases.add_argument("--sort", choices=["rate", "recent", "best", "names", "hits"], default="rate",
                         help="sort key: shrunk tail rate (default), recent-window rate, best score, "
                              "name count or hit count")
    p_bases.add_argument("--min-names", type=int, default=0,
                         help="hide bases with fewer scored coined names than this")
    p_bases.add_argument("--spoken", action="store_true",
                         help="count only names with an LLM pronunciation review (what the coin loop uses "
                              "when BASE_SPOKEN_ONLY is on)")

    p_coin = sub.add_parser(
        "coin",
        help="programmatically generate and clear novel brand variations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Coining Examples:
  # Continuous autonomous discovery loop (press Ctrl+C anytime to pause):
  namefinder coin --continuous --backend local

  # Continuous loop focused on one root (blends may still pair it with any other word):
  namefinder coin --continuous --bases root

  # Draw 30 independent variations across full ranking (exponential decay half-life 25):
  namefinder coin --limit 30 --backend local

  # Coin from specific base words:
  namefinder coin --bases spectral,fathom,lattice --limit 20

  # Fast dry-run without RDAP domain check or LLM scoring:
  namefinder coin --no-domains --no-score --limit 50
""",
    )
    _add_common(p_coin)
    p_coin.add_argument("--bases", type=str, default=None,
                        help="comma-separated base words (e.g. spectral,quartz,atlas,fathom); with --continuous, "
                             "every name is built on these while blends may pair them with any other word")
    p_coin.add_argument("--top-bases", type=int, default=25,
                        help="how many base words to sample across vocabulary (default: 25)")
    p_coin.add_argument("--half-life", type=float, default=25.0,
                        help="rank-based exponential falloff half-life (default: 25.0)")
    p_coin.add_argument("--continuous", "-c", action="store_true",
                        help="run continuous autonomous discovery loop until Ctrl+C")
    p_coin.add_argument("--backend", choices=["openrouter", "local", "lmstudio"], default=None,
                        help="scoring backend (default: openrouter; 'local' for LM Studio)")
    p_coin.add_argument("--min-len", type=int, default=5, help="minimum candidate length (default: 5)")
    p_coin.add_argument("--max-len", type=int, default=10, help="maximum candidate length (default: 10)")
    p_coin.add_argument("--batch", type=int, default=config.COIN_BATCH,
                        help="coined words per continuous cycle (default: %(default)s)")
    p_coin.add_argument("--domain-top", type=float, default=config.COIN_DOMAIN_TOP,
                        help="fraction of each cycle (best scores) to domain-check; 1.0 = all (default: %(default)s)")
    p_coin.add_argument("--concurrency", type=int, default=config.COIN_CONCURRENCY,
                        help="parallel scoring requests per cycle (default: %(default)s)")
    p_coin.add_argument("--model", default=None, help="model id override for scoring coined words")
    p_coin.add_argument("--llm-share", type=float, default=config.COIN_LLM_SHARE,
                        help="fraction of each batch invented by the LLM engine; 0 disables (default: %(default)s)")
    p_coin.add_argument("--coin-model", default=None,
                        help="model id for LLM coining (default: OPENROUTER_COIN_MODEL, else the scoring model)")
    p_coin.add_argument("--max-cost", type=float, default=None, help=_MAX_COST_HELP)
    p_coin.add_argument("--no-domains", action="store_true", help="skip automatic RDAP domain checks")
    p_coin.add_argument("--no-score", action="store_true", help="skip LLM brand scoring")

    p_rc = sub.add_parser(
        "recompute",
        help="re-derive composites of coined words from stored sub-scores after changing config weights (no LLM calls)",
    )
    _add_common(p_rc)
    p_rc.add_argument("--no-backup", action="store_true", help="skip the database backup before updating")

    p_rs = sub.add_parser(
        "rescore",
        help="re-score the top coined names with the current brand prompt (after editing prompts; resumable)",
    )
    _add_common(p_rs)
    p_rs.add_argument("--top-n", type=int, default=config.RESCORE_TOP_N,
                      help="re-score this many of the best names still on an older prompt; rerun to continue "
                           "down the list (default: %(default)s)")
    p_rs.add_argument("--backend", choices=["openrouter", "local", "lmstudio"], default=None)
    p_rs.add_argument("--model", default=None, help="model id override (default: the scoring model)")
    p_rs.add_argument("--concurrency", type=int, default=config.COIN_CONCURRENCY,
                      help="parallel requests (default: %(default)s)")
    p_rs.add_argument("--max-cost", type=float, default=None, help=_MAX_COST_HELP)
    p_rs.add_argument("--no-backup", action="store_true", help="skip the database backup before re-scoring")

    p6 = sub.add_parser("status", help="show pipeline cache counts and funnel summary")
    _add_common(p6)

    return ap


_AGE_UNITS = {
    "s": "seconds", "sec": "seconds", "secs": "seconds", "second": "seconds", "seconds": "seconds",
    "m": "minutes", "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes",
    "h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours",
    "d": "days", "day": "days", "days": "days",
    "w": "weeks", "wk": "weeks", "wks": "weeks", "week": "weeks", "weeks": "weeks",
}
_AGE_PART = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]*)")


def _parse_age(text: str) -> timedelta | None:
    """'30s', '10m', '6h', '2d', '1w', '1h30m', '2 days', 'all' -> timedelta; '0' / 'off' -> None.

    A bare number means minutes. 'all' covers every row (marks everything scored).
    """
    raw = str(text or "0").strip().lower()
    if raw in ("0", "off", "none", "no"):
        return None
    if raw in ("all", "ever", "forever"):
        return timedelta(days=365 * 100)
    compact = raw.replace(" ", "")
    parts = _AGE_PART.findall(compact)
    if not parts or "".join(n + u for n, u in parts) != compact or any(u and u not in _AGE_UNITS for _, u in parts):
        raise SystemExit(
            f"--since: expected an age like 30s, 10m, 6h, 2d, 1w or 1h30m (or 0/off, all), got {text!r}"
        )
    total = timedelta()
    for num, unit in parts:
        total += timedelta(**{_AGE_UNITS[unit or "m"]: float(num)})
    return total or None


def _mark_fresh(items: list[dict], window: timedelta | None) -> None:
    """Set item['fresh'] to 'new' (scored in window), 'refined' (refined in window) or None."""
    cutoff = (datetime.now(timezone.utc) - window).isoformat(timespec="seconds") if window else None
    for it in items:
        it["fresh"] = None
        if not cutoff:
            continue
        if (it.get("scored_at") or "") >= cutoff:
            it["fresh"] = "new"
        elif (it.get("refined_at") or "") >= cutoff:
            it["fresh"] = "refined"


def cmd_top(db_path, limit: int = 25, only_free: bool = False, only_clean: bool = False,
            bases_only: bool = False, all_free: bool = False,
            since: str = config.TOP_SINCE, new_only: bool = False, spoken_only: bool = False,
            by: str = "score", judge_model: str | None = None) -> None:
    from . import tournament
    window = _parse_age(since)
    if new_only and not window:
        raise SystemExit("--new needs a non-zero --since window")
    with db.session(db_path) as conn:
        qversion = config.QUESTION_VERSION
        judge, judge_note = tournament.resolve_judge(conn, judge_model)
        rated = db.tournament_ratings(conn, judge) if judge else {}
        if by == "tournament" and not rated:
            raise SystemExit(judge_note or "No tournament ratings yet. Run `namefinder tournament` first.")
        order = sorted(rated, key=lambda w: rated[w]["rating"], reverse=True)
        elo_rank = {w: i for i, w in enumerate(order, 1)}
        items = db.get_candidates_summary(
            conn, qversion, limit=10**9 if (new_only or by == "tournament") else limit,
            only_free_domains=only_free,
            only_clean_trademarks=only_clean,
            bases_only=bases_only,
            all_domains_free=all_free,
            spoken_only=spoken_only,
            only_words=set(rated) if by == "tournament" else None,
        )
        for it in items:
            r = rated.get(it["word"])
            it["elo"] = r["rating"] if r else None
            it["elo_rank"] = elo_rank.get(it["word"])
        if by == "tournament":
            items.sort(key=lambda it: it["elo"], reverse=True)
            items = items[:10**9 if new_only else limit]
        _mark_fresh(items, window)
        if new_only:
            items = [it for it in items if it["fresh"]][:limit]
        n_new = sum(1 for it in items[:limit] if it["fresh"] == "new")
        n_ref = sum(1 for it in items[:limit] if it["fresh"] == "refined")

        filter_desc = []
        if bases_only:
            filter_desc.append("base words only")
        if all_free:
            filter_desc.append("all checked domains free")
        if only_free:
            filter_desc.append("free domains only")
        if only_clean:
            filter_desc.append("clean trademarks only")
        if spoken_only:
            filter_desc.append("pronunciation-reviewed only")
        if by == "tournament":
            filter_desc.insert(0, f"by tournament rating, judge {judge}")
        filters_str = f" ({', '.join(filter_desc)})" if filter_desc else ""

        legend = "✓ = refined"
        if window:
            legend += f", NEW/REF = scored/refined in last {since}: {n_new} new, {n_ref} refined"
        title = f"NameFinder Rankings: Top {len(items)}{filters_str}  ({legend})"
        if not items:
            print(f"\n{console.yellow('No candidates match the specified criteria.')}")
            print(f"Run {console.b_cyan('namefinder score')} or {console.b_cyan('namefinder coin')} first.\n")
            return

        console.print_leaderboard(
            items,
            title=title,
            limit=limit,
            show_domains=True,
            show_reasoning=True,
        )
        if judge_note and (by == "tournament" or any(it["elo"] is not None for it in items[:limit])):
            print(console.dim(f"  note: {judge_note}"))


def cmd_bases(db_path, limit: int = 30, sort: str = "rate", min_names: int = 0, spoken_only: bool = False) -> None:
    from . import bases
    with db.session(db_path) as conn:
        stats, thr = bases.base_stats(conn, config.QUESTION_VERSION, spoken_only=spoken_only)
    rows = [s for s in stats.values() if s.n >= min_names]
    if not rows:
        print(f"\n{console.yellow('No scored coined names with a recorded base yet.')}\n")
        return
    key = {
        "rate": lambda s: s.rate, "recent": lambda s: s.recent_rate, "best": lambda s: s.best,
        "names": lambda s: s.n, "hits": lambda s: s.hits,
    }[sort]
    rows.sort(key=key, reverse=True)
    a, b = config.BASE_PRIOR
    top_pct = round((1 - config.BASE_TAIL_QUANTILE) * 100)
    console.print_base_table(
        rows[:limit], thr,
        title=f"Base value{' (pronunciation-reviewed names only)' if spoken_only else ''}: "
              f"{len(rows)} bases, a hit = top {top_pct}% of coined names "
              f"(rate = (hits+{a})/(names+{a + b}); recent = last {config.BASE_WINDOW} names, red = running dry)",
    )


def cmd_status(db_path) -> None:
    with db.session(db_path) as conn:
        def one(sql, *args):
            return conn.execute(sql, args).fetchone()[0]

        qversion = config.QUESTION_VERSION
        db_file = db_path or config.DB_PATH

        print(f"\n{console.bold(console.b_cyan('=== NameFinder Pipeline Dashboard ==='))}")
        print(f"Database : {console.dim(str(db_file))}")
        print(f"Version  : {console.b_cyan(qversion)}")

        n_words = one('SELECT COUNT(*) FROM words')
        n_scores = one('SELECT COUNT(*) FROM scores WHERE qversion = ?', qversion)
        n_domains = one('SELECT COUNT(*) FROM domains')
        n_free = one('SELECT COUNT(*) FROM domains WHERE status = ?', 'available')
        n_marks = one('SELECT COUNT(*) FROM mark_checks')
        n_hits = one('SELECT COUNT(*) FROM mark_hits')
        n_repos = one('SELECT COUNT(*) FROM repo_checks')
        n_final = one('SELECT COUNT(*) FROM final_tags')

        print(f"Words    : {console.b_white(f'{n_words:,}')} vocabulary words prepared")
        print(f"Scores   : {console.b_green(f'{n_scores:,}')} scored candidates")
        print(f"Domains  : {console.b_white(f'{n_domains:,}')} checked, {console.b_green(f'{n_free:,}')} free")
        print(f"Marks    : {console.b_white(f'{n_marks:,}')} screened, {console.yellow(f'{n_hits:,}')} hits")
        print(f"Repos    : {console.b_white(f'{n_repos:,}')} PyPI / GitHub screens")
        print(f"Final    : {console.b_cyan(f'{n_final:,}')} tagged shortlist candidates")

        passing_doms = len(db.words_passing_domains(conn, qversion))
        passing_marks = len(db.words_passing_trademarks(conn, qversion))

        print(f"\n{console.bold('Pipeline Funnel:')}")
        print(f"  1. Prepared words      : {console.b_white(f'{n_words:,}')}")
        print(f"  2. Scored words        : {console.b_cyan(f'{n_scores:,}')}")
        print(f"  3. With a free domain  : {console.b_green(f'{passing_doms:,}')}")
        print(f"  4. Clean trademarks    : {console.b_green(f'{passing_marks:,}')}")

        top5 = db.get_candidates_summary(conn, qversion, limit=5)
        if top5:
            console.print_leaderboard(top5, title="Top 5 Candidates Preview", limit=5)


def _screen_words(args) -> list[str] | None:
    """Names for `check-trademarks`: --word entries and/or the top --tournament N, else None (old behaviour)."""
    words = [w.strip().lower() for w in (args.word or []) if w.strip()]
    if args.tournament:
        from . import tournament
        with db.session(args.db) as conn:
            judge, note = tournament.resolve_judge(conn)
            if not judge:
                raise SystemExit(note or "No tournament ratings yet. Run `namefinder tournament` first.")
            rows = conn.execute(
                "SELECT word FROM tournament_ratings WHERE judge = ? ORDER BY rating DESC LIMIT ?",
                (judge, args.tournament)).fetchall()
        words += [r["word"] for r in rows]
    return list(dict.fromkeys(words)) or None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd in ("score", "score-jev", "refine", "coin", "rescore", "tournament", "ideas", "generate", "evolve"):
        budget.reset(getattr(args, "max_cost", None))

    try:
        if args.cmd == "prepare":
            from . import words
            target = args.limit or args.target or config.WORD_TARGET
            words.run(target=target, scan=args.scan, use_pos=not args.no_pos, db_path=args.db, refresh=args.refresh)
        elif args.cmd in ("score", "score-jev"):
            from . import score
            free_doms = getattr(args, "free_domains_only", False)
            clear_flag = getattr(args, "clear", False)
            score.run(limit=args.limit, top_n=args.top_n, refresh=args.refresh,
                      concurrency=args.concurrency, model=args.model, db_path=args.db,
                      sample=args.sample, backend=args.backend, free_domains_only=free_doms,
                      clear_only=clear_flag, mode=getattr(args, "mode", "auto"))
        elif args.cmd == "check-domains":
            from . import domains
            domains.run(limit=args.limit, refresh=args.refresh, db_path=args.db, top_n=args.top_n)
        elif args.cmd == "check-trademarks":
            from . import trademarks
            trademarks.run(limit=args.limit, refresh=args.refresh, db_path=args.db,
                           do_markbase=not args.no_markbase, do_ipgod=not args.no_ipgod,
                           do_pypi=not args.no_pypi, do_github=not args.no_github,
                           top_n=args.top_n, words=_screen_words(args))
        elif args.cmd == "rank-final":
            from . import final
            final.run(limit=args.limit, refresh=args.refresh, model=args.model,
                      db_path=args.db, skip_llm=args.skip_llm)
        elif args.cmd == "refine":
            from . import refine
            refine.run(top_n=args.top_n, samples=args.samples, model=args.model, backend=args.backend,
                       refresh=args.refresh, free_domains_only=args.free_domains_only,
                       all_free=args.all_free, clean_only=args.clean, db_path=args.db,
                       concurrency=args.concurrency)
        elif args.cmd == "tournament":
            from . import tournament
            tournament.run(pool_size=args.pool, rounds=args.rounds, model=args.model, refs=args.ref,
                           tld=args.tld, concurrency=args.concurrency, backend=args.backend,
                           show=args.show, show_only=args.show_only, db_path=args.db, seed=args.seed,
                           source=args.source, carry=args.carry,
                           prune_below=None if args.no_prune else args.prune_below,
                           prune_rank=None if args.no_prune else args.prune_rank,
                           margin=args.margin, min_games=args.min_games, z=args.z,
                           group_size=args.group_size, refit_only=args.refit)
        elif args.cmd == "ideas":
            from . import ideas
            if args.ideas_cmd == "generate":
                ideas.generate(per_angle=args.per_angle, angles=args.angle, model=args.model,
                               backend=args.backend, concurrency=args.concurrency,
                               merge_after=not args.no_merge, db_path=args.db)
            elif args.ideas_cmd == "merge":
                ideas.merge(model=args.model, backend=args.backend, db_path=args.db)
            elif args.ideas_cmd == "review":
                ideas.review(status=args.status, db_path=args.db)
            elif args.ideas_cmd == "list":
                ideas.show_list(status=args.status, db_path=args.db)
            elif args.ideas_cmd == "mark":
                ideas.mark(args.approve, args.reject, db_path=args.db)
        elif args.cmd == "generate":
            from . import ideanames
            ideanames.run(per_idea=args.per_idea, idea_ids=args.idea, model=args.model, backend=args.backend,
                          concurrency=args.concurrency, min_len=args.min_len, max_len=args.max_len,
                          check=not args.no_domains, use_feedback=not args.no_feedback, all_tlds=args.all_tlds, db_path=args.db)
        elif args.cmd == "evolve":
            from . import evolve
            evolve.run(top=args.top, variants=args.variants, include_refs=not args.no_refs, model=args.model,
                       backend=args.backend, concurrency=args.concurrency, min_len=args.min_len,
                       max_len=args.max_len, check=not args.no_domains, judge_model=args.judge,
                       all_tlds=args.all_tlds, db_path=args.db)
        elif args.cmd == "top":
            cmd_top(args.db, limit=args.top_count, only_free=args.free_domains, only_clean=args.clean,
                    bases_only=args.bases_only, all_free=args.all_free,
                    since=args.since, new_only=args.new_only, spoken_only=args.spoken,
                    by=args.by, judge_model=args.judge)
        elif args.cmd == "bases":
            cmd_bases(args.db, limit=args.top_count, sort=args.sort, min_names=args.min_names,
                      spoken_only=args.spoken)
        elif args.cmd == "coin":
            from . import coin
            bases_list = [b.strip() for b in args.bases.split(",")] if args.bases else None
            coin.run(
                bases=bases_list,
                top_base_count=args.top_bases,
                limit=args.limit,
                min_len=args.min_len,
                max_len=args.max_len,
                check_domains_flag=not args.no_domains,
                score_flag=not args.no_score,
                continuous=getattr(args, "continuous", False),
                half_life=getattr(args, "half_life", 25.0),
                backend=getattr(args, "backend", None),
                model_override=args.model,
                batch=args.batch,
                domain_top=args.domain_top,
                concurrency=args.concurrency,
                llm_share=args.llm_share,
                coin_model=args.coin_model,
                db_path=args.db,
            )
        elif args.cmd == "recompute":
            from . import score as score_mod
            total, changed, path = score_mod.recompute_brand(args.db, backup=not args.no_backup)
            print(f"[recompute] {total} coined words checked, {changed} composites updated")
            if path:
                print(f"[recompute] backup: {path}")
        elif args.cmd == "rescore":
            from . import score as score_mod
            score_mod.rescore_run(top_n=args.top_n, model=args.model, backend=args.backend,
                                  concurrency=args.concurrency, db_path=args.db, backup=not args.no_backup)
        elif args.cmd == "status":
            cmd_status(args.db)
        return 0
    except KeyboardInterrupt:
        print("\n[namefinder] Process interrupted by user (Ctrl+C). Exiting cleanly.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
