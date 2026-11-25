# NameFinder

Python CLI pipeline for brand-name discovery: prepare vocabulary, score with an LLM, coin neologisms, check domains and trademarks, rank a final shortlist.

## Layout
- `namefinder/cli.py` - argparse entry point (`prepare`, `score`, `coin`, `check-domains`, `check-trademarks`, `rank-final`, `refine`, `rescore`, `recompute`, `tournament`, `ideas`, `generate`, `evolve`, `top`, `bases`, `engines`, `lexicon`, `families`, `embed`, `taste`, `status`)
- `namefinder/db.py` - SQLite schema and queries (`data/namefinder.db`)
- `namefinder/score.py` - LLM scoring; root mode for dictionary words, brand mode for coined words (notes first: root, story, fatal label; a fatal label raises `neg` to `SCORE_FATAL_NEG`)
- `namefinder/phonetics.py` - deterministic syllable/pronounceability checks
- `namefinder/llmcoin.py` - LLM coining engine (`engine='llm'`): proposes verb-like names from top bases, same quality gate, feedback from past scores
- `namefinder/lexicon.py` - `namefinder lexicon`: extra base-word layers (reinstated abstract nouns, domain jargon, foreign words from OMW translations) from WordNet, added unscored with a gloss; `score --lexicon LAYER` scores one layer
- `namefinder/coin.py` engines: suffix, trunc, portmanteau, compound, twist (`engine='twist'`: one small edit of a top coined name; `words.parents` is `base,winner`). vowel, mutate and respell are retired as blind engines and serve as twist operators
- `namefinder/engines.py` - `namefinder engines`: tail rate per coining engine, per operator (`words.operator`) or base layer (`--by`), and tail-rate lift by name feature, to judge rule changes on data
- `namefinder/wordlike.py` - letter-trigram wordlikeness score; used only as a floor against gibberish in the coin gate (it does not predict a top composite)
- `namefinder/bases.py` - base-word value (shrunk top-5% tail rate per base) and the Thompson-sampling bandit that weights base selection in `coin`
- `namefinder/refine.py` - pass 2: re-samples the spoken-name review for the top coined words and averages it with pass 1 to stabilise rankings (`namefinder refine`); pass 1 brand scoring already spells words out via `score.spoken_review`
- `namefinder/tournament.py` - ranks the best names against each other in groups of 8 (each group judged twice in opposite order); the judge writes per-name notes (reads, say, story, fatal label, flaw) before its ranking, and code ranks fatal names last in their group; ratings are stored per judge model + prompt hash in `tournament_ratings`, so switching models starts a fresh rating set
- `namefinder/taste.py` - `namefinder taste`: agreement of each stored rating set and the composite with your own tiers in `prompts/taste.txt`; `--eval N [--prompt FILE]` measures a judge prompt and `--scorer PASSES [--prompt FILE]` the brand scorer on your rated names, without saving anything
- `namefinder/ideas.py`, `ideanames.py`, `evolve.py` - idea-first generation: `ideas generate|merge|review|list|mark` makes, de-duplicates and prunes ideas (a statement about the product's mechanism or purpose plus candidate roots; angles are weighted; statuses pending/approved/rejected/merged); `generate` turns approved ideas into names (engine `idea`, with a construction note in `name_origins`); `evolve` makes variants (engine `variant`) of the best-rated names from the judge's stated flaws. `ideanames.register` is the shared gate; names already in the old pool are kept as `rediscovered` (origin row only, legacy rows untouched)
- `namefinder/families.py` - `namefinder families`: hard bans (`config.BANNED_*`, checked in `coin.acceptable`), name families (opening/ending) and their saturation among the best names, collisions with `prompts/collision_terms.txt`, sound-alikes and misspellings of common words, unreadable endings (`phonetics.bad_ending`); `families.Gate` screens new names in `coin`, `llmcoin`, `ideanames.register`, and `screen_pool` screens the tournament pool
- `namefinder/embed.py` - `namefinder embed`: meaning embeddings (OpenAI-compatible `/embeddings`, cached in the `embeddings` table); idea seeds (bases retrieved per approved idea, fed to the LLM coin engine, operator `seed:<id>`), obliqueness diagnostic vs tournament ratings
- `namefinder/llm.py` - shared JSON chat call for the generation stages
- `namefinder/ratings.py` - Plackett-Luce fit (with standard errors) over a judge's logged matches; the tournament refits it after every round and prunes on it
- `namefinder/budget.py` - per-command OpenRouter spend tracking and `--max-cost` limit
- `namefinder/console.py` - terminal UI
- Prompt set: `prompts/` (gitignored, the user's own) or the folder in `NAMEFINDER_PROMPTS`; `config.load_prompt` reads a file from it or falls back to the built-in text. `product.md` is the brief (`config.PRODUCT_BLURB`, `{product}` in every built-in prompt); `angles.txt`, `bans.txt`, `category_anchors.txt`, `collision_terms.txt`, `concept_roots.txt`, `taste.txt`, `tournament_refs.txt` are optional lists
- `examples/image-provenance/` - complete worked prompt set for an invented product; the README demo ran on it (`NAMEFINDER_PROMPTS=examples/image-provenance NAMEFINDER_DB=data/demo.db`)
- `docs/` - local, gitignored notes

## Key facts
- `words.source` is `wordfreq` (dictionary base), `lexicon` (extra base layers from `namefinder lexicon`; `words.lexicon` names the layer, `words.gloss` holds a WordNet meaning shown to the root scorer) or `coined`.
- `words.parents` on coined words is `base` or `base,partner`; the first entry is the base that `bases.py` credits.
- Each brand score's `raw` JSON stores `"prompt"`, a fingerprint of the scoring prompts (`score.prompt_fingerprint`); `rescore` re-scores coined names whose fingerprint is stale.
- The built-in judge and naming rules favour a sober technical term that ought to exist: a classical root the reader half-knows plus a standard ending. Hand ratings showed this beat an earlier oblique objective, which agreed with them less than chance. Measure prompt changes with `namefinder taste --eval` (judge) or `--scorer` (brand scorer) and never use `[held out]` names as prompt examples.
- `score --clear` backs up the DB to `data/backups/`, then deletes all scores and coined words. `score` skips coined words unless `--mode brand`.
- Secrets live in `.env` (gitignored). Never commit `.env`, `data/`, `results/` or anything in `prompts/`.

## Commands
- Install: `pip install -e .` (venv in `.venv`)
- Run: `namefinder <command>`; see `USAGE.md`

## Git conventions
- Commits are authored by the repository owner only. Never add `Co-Authored-By` or "Generated with Claude" lines.
- Use professional Conventional Commit messages: `type: imperative summary` (feat, fix, docs, refactor, test, chore), a short body when needed.
- Commit only when asked.
