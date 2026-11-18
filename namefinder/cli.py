"""Command line interface: one subcommand per stage so costs stay inspectable."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config, db


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", type=Path, default=None, help="override the SQLite path")
    p.add_argument("--limit", type=int, default=None,
                   help="cap how many words this stage processes (use it for dry runs)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="namefinder",
        description="Discover and clear an original single-word brand name, stage by stage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Typical run:
  namefinder prepare
  namefinder score --limit 50            # probe score top candidates
  namefinder score                       # full pass (cached, resumable)
  namefinder check-domains
  namefinder check-trademarks
  namefinder rank-final
  namefinder top                         # view top ranked candidates in terminal
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
    p2 = sub.add_parser("score", help="stage 2: brand vibe scoring (OpenRouter / local LLM)")
    _add_common(p2)
    p2.add_argument("--backend", choices=["openrouter", "local", "lmstudio", "typesafe"], default=None,
                    help="scoring backend (default: openrouter; 'local' for LM Studio)")
    p2.add_argument("--clear", action="store_true",
                    help="wipe all cached scores from the database without re-scoring")
    p2.add_argument("--free-domains-only", action="store_true",
                    help="score ONLY words that already have a free domain (massive speedup!)")
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

    p_coin = sub.add_parser("coin", help="programmatically generate and clear novel brand variations")
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

        print(f"\n=== NameFinder Rankings: Top {len(items)}{filters_str} ===\n")
        if not items:
            print("No candidates match the specified criteria.")
            print("Run `namefinder score` or `namefinder check-domains` first.\n")
            return

        header = f"{'#':<3} {'Candidate':<12} {'Score':<7} {'Domains':<26} {'Trademark':<11} {'Why & Risk Assessment'}"
        print(header)
        print("-" * len(header))
        for i, item in enumerate(items, 1):
            comp_str = f"{item['composite']:.3f}" if item['composite'] is not None else "-"
            why_text = item.get("why") or "-"
            why_not = item.get("why_not")
            reasoning = f"{why_text} [Risk: {why_not}]" if why_not else why_text

            print(
                f"{i:<3} {item['word']:<12} {comp_str:<7} "
                f"{item['domains'][:25]:<26} {item['trademark']:<11} {reasoning}"
            )
        print()


def cmd_status(db_path) -> None:
    with db.session(db_path) as conn:
        def one(sql, *args):
            return conn.execute(sql, args).fetchone()[0]

        qversion = config.QUESTION_VERSION
        print(f"\n=== NameFinder Pipeline Status ===")
        print(f"database : {db_path or config.DB_PATH}")
        print(f"words    : {one('SELECT COUNT(*) FROM words'):,} vocabulary words prepared")
        print(f"scores   : {one('SELECT COUNT(*) FROM scores WHERE qversion = ?', qversion):,} scored candidates (version: {qversion})")
        print(f"domains  : {one('SELECT COUNT(*) FROM domains'):,} checked, "
              f"{one('SELECT COUNT(*) FROM domains WHERE status = ?', 'available'):,} available")
        print(f"marks    : {one('SELECT COUNT(*) FROM mark_checks')} screened, "
              f"{one('SELECT COUNT(*) FROM mark_hits')} hits, "
              f"{one('SELECT COUNT(*) FROM ipgod_marks')} IP Australia entries")
        print(f"repos    : {one('SELECT COUNT(*) FROM repo_checks')} PyPI / GitHub screens")
        print(f"final    : {one('SELECT COUNT(*) FROM final_tags')} tagged shortlist words")

        print("\nPipeline Funnel:")
        print(f"  1. Prepared words      : {one('SELECT COUNT(*) FROM words'):,}")
        print(f"  2. Scored words        : {one('SELECT COUNT(*) FROM scores WHERE qversion = ?', qversion):,}")
        print(f"  3. With a free domain  : {len(db.words_passing_domains(conn, qversion)):,}")
        print(f"  4. Clean trademarks    : {len(db.words_passing_trademarks(conn, qversion)):,}")

        # Quick preview of top 5
        top5 = db.get_candidates_summary(conn, qversion, limit=5)
        if top5:
            print("\nTop Candidates Preview:")
            for i, r in enumerate(top5, 1):
                print(f"  {i}. {r['word']:<10} score={r['composite']:.3f}  domains={r['domains']}  marks={r['trademark']}")
        print()


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
