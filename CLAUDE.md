# NameFinder

Python CLI pipeline for brand-name discovery: prepare vocabulary, score with an LLM, coin neologisms, check domains and trademarks, rank a final shortlist.

## Layout
- `namefinder/cli.py` - argparse entry point (`prepare`, `score`, `coin`, `check-domains`, `check-trademarks`, `rank-final`, `top`, `status`)
- `namefinder/db.py` - SQLite schema and queries (`data/namefinder.db`)
- `namefinder/score.py` - LLM scoring; root mode for dictionary words, brand mode for coined words
- `namefinder/phonetics.py` - deterministic syllable/pronounceability checks
- `namefinder/refine.py` - pass 2: spoken-name LLM review of top coined words (`namefinder refine`)
- `namefinder/budget.py` - per-command OpenRouter spend tracking and `--max-cost` limit
- `namefinder/console.py` - terminal UI
- `prompts/*.md` - private, gitignored prompts (see `*.example.md`)
- `docs/` - local, gitignored notes

## Key facts
- `words.source` is `wordfreq` (dictionary base) or `coined`.
- `score --clear` backs up the DB to `data/backups/`, then deletes all scores and coined words. `score` skips coined words unless `--mode brand`.
- Secrets live in `.env` (gitignored). Never commit `.env`, `data/`, `results/` or private prompts.

## Commands
- Install: `pip install -e .` (venv in `.venv`)
- Run: `namefinder <command>`; see `USAGE.md`

## Git conventions
- Commits are authored by the repository owner only. Never add `Co-Authored-By` or "Generated with Claude" lines.
- Use professional Conventional Commit messages: `type: imperative summary` (feat, fix, docs, refactor, test, chore), a short body when needed.
- Commit only when asked.
