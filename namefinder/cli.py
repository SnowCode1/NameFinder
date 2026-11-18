"""Command line interface: one subcommand per stage so costs stay inspectable."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config, console, db


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
     namefinder check-domains --top-n 500       # check .com, .dev, .app via keyless RDAP
     namefinder check-trademarks --top-n 100    # check USPTO (Markbase), PyPI, GitHub

  4. Inspect Rankings & Export:
     namefinder top --free-domains              # view top brands with verified available domains
     namefinder top --clean                     # view top brands clear of trademark conflicts
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
    p2.add_argument("--clear", action="store_true",
                    help="wipe all cached scores from the database without re-scoring")
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

    p5 = sub.add_parser("rank-final", help="stage 5: tag and write the shortlist")
    _add_common(p5)
    p5.add_argument("--refresh", action="store_true")
    p5.add_argument("--model", default=None, help="OpenRouter model id")
    p5.add_argument("--skip-llm", action="store_true",
                    help="write the report from cached data without calling OpenRouter")

    p_top = sub.add_parser("top", help="inspect candidate rankings directly in the terminal")
    _add_common(p_top)
    p_top.add_argument("--top", "-n", dest="top_count", type=int, default=25,
                       help="how many candidates to show (default: 25)")
    p_top.add_argument("--free-domains", action="store_true",
                       help="show only candidates with at least one free domain")
    p_top.add_argument("--clean", action="store_true",
                       help="show only candidates clear of trademark conflicts")

    p_coin = sub.add_parser(
        "coin",
        help="programmatically generate and clear novel brand variations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Coining Examples:
  # Continuous autonomous discovery loop (press Ctrl+C anytime to pause):
  namefinder coin --continuous --backend local

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
                        help="comma-separated base words (e.g. spectral,quartz,atlas,fathom)")
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
    p_coin.add_argument("--no-domains", action="store_true", help="skip automatic RDAP domain checks")
    p_coin.add_argument("--no-score", action="store_true", help="skip LLM brand scoring")

    p6 = sub.add_parser("status", help="show pipeline cache counts and funnel summary")
    _add_common(p6)

    return ap


def cmd_top(db_path, limit: int = 25, only_free: bool = False, only_clean: bool = False) -> None:
    with db.session(db_path) as conn:
        qversion = config.QUESTION_VERSION
        items = db.get_candidates_summary(
            conn, qversion, limit=limit,
            only_free_domains=only_free,
            only_clean_trademarks=only_clean,
        )

        filter_desc = []
        if only_free:
            filter_desc.append("free domains only")
        if only_clean:
            filter_desc.append("clean trademarks only")
        filters_str = f" ({', '.join(filter_desc)})" if filter_desc else ""

        title = f"NameFinder Rankings: Top {len(items)}{filters_str}"
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

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
                      clear_only=clear_flag)
        elif args.cmd == "check-domains":
            from . import domains
            domains.run(limit=args.limit, refresh=args.refresh, db_path=args.db, top_n=args.top_n)
        elif args.cmd == "check-trademarks":
            from . import trademarks
            trademarks.run(limit=args.limit, refresh=args.refresh, db_path=args.db,
                           do_markbase=not args.no_markbase, do_ipgod=not args.no_ipgod,
                           do_pypi=not args.no_pypi, do_github=not args.no_github,
                           top_n=args.top_n)
        elif args.cmd == "rank-final":
            from . import final
            final.run(limit=args.limit, refresh=args.refresh, model=args.model,
                      db_path=args.db, skip_llm=args.skip_llm)
        elif args.cmd == "top":
            cmd_top(args.db, limit=args.top_count, only_free=args.free_domains, only_clean=args.clean)
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
                db_path=args.db,
            )
        elif args.cmd == "status":
            cmd_status(args.db)
        return 0
    except KeyboardInterrupt:
        print("\n[namefinder] Process interrupted by user (Ctrl+C). Exiting cleanly.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
