# NameFinder — Quick Reference

## Setup (one time)

### 1. Virtual Environment

If you have **not** created `.venv` yet:
```bash
python -m venv .venv
```

**Activate it:**
- **PowerShell:**
  ```powershell
  Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
  .\.venv\Scripts\Activate.ps1
  ```
- **Command Prompt (CMD):**
  ```cmd
  .venv\Scripts\activate.bat
  ```

> **Important Windows note:**
> If your terminal prompt already shows `(.venv)`, your virtual environment is **already active**.
> Do **not** run `python -m venv .venv` while `(.venv)` is active; Windows locks `python.exe` while it is running, causing:
> `Error: [Errno 13] Permission denied: '...\\.venv\\Scripts\\python.exe'`.
> Simply proceed to the next step.

### 2. Install Package

```bash
pip install -e .
```

### 3. Copy Environment Template

- **CMD:** `copy .env.example .env`
- **PowerShell:** `Copy-Item .env.example .env`
- **Git Bash:** `cp .env.example .env`

Fill in `OPENROUTER_API_KEY` in `.env` if you want Stage 2 (scoring) and Stage 5 (shortlist tags). Stage 1, 3, and 4 work without any API keys.

---

## Standard Workflows

### Recommended Strategy: Root Scoring + Autonomous Coining

Because ~96% of pure dictionary words have their `.com` already registered, the most effective workflow is:
1. Prepare English roots (~11,000 words).
2. Score base roots to identify strong conceptual candidates (`lattice`, `fathom`, `lens`, `lineage`, `algebra`).
3. Run the autonomous coining loop to generate variations, screen available domains on the fly, and score winners.

```powershell
# 1. Ingest & filter English vocabulary (~11,000 words)
namefinder prepare

# 2. Score top base roots locally (~2.1s per word with early-out on RTX 3060)
namefinder score --backend local --limit 200

# 3. Launch continuous autonomous discovery stream (press Ctrl+C anytime to pause)
namefinder coin --continuous --backend local

# 4. View top discovered brands with free domains
namefinder top --free-domains
```

---

### Alternative: Direct Dictionary Screening

If you want to evaluate pure dictionary words directly for domain and trademark clearance:

```powershell
# 1. Ingest dictionary words
namefinder prepare

# 2. Score dictionary words
namefinder score --backend local --limit 100

# 3. Check domains via keyless RDAP (.com, .io, .dev)
namefinder check-domains --top-n 200

# 4. Check USPTO trademarks (Markbase), PyPI, and GitHub
namefinder check-trademarks --top-n 50

# ... or screen your tournament leaders (USPTO via Markbase, PyPI, GitHub) and print a summary table:
namefinder check-trademarks --tournament 25 --no-ipgod
# or specific names:
namefinder check-trademarks --word origset --word fotogram

# 5. Export final shortlist
namefinder rank-final --skip-llm
```

---

## Widen the Base Dictionary (`namefinder lexicon`)

`prepare` keeps mid-frequency WordNet nouns and verbs and drops many endings (-ity, -ance, -ence, -ism, -tion ...), which also removes good abstract nouns (provenance, precedence, antecedent) and every rare technical term. `lexicon` adds those back as extra base-word layers:

| Layer | What it is |
|---|---|
| `reinstated` | ordinary-frequency nouns and verbs that `prepare` dropped for an ending or for length (up to 11 letters), minus inflections and weak endings (-ness, -ist, -ful ...) |
| `jargon` | rarer words (zipf 1.5 and up) that WordNet files under a subject domain (law, mathematics, logic, linguistics ...) or under an abstract category (perplexity, corollary), minus sport, cooking, anatomy and similar domains |
| `foreign` | common Romance and Scandinavian words from the OMW wordnet translations of abstract English concepts, shared by at least two languages (fase, pausa, consulta), minus near-respellings of the English word (kontroll, strategi). Each carries a gloss such as "Italian/Spanish: 'phase'". Downloads `omw-1.4` on first use |
| `rare` | the same rare band with no tag. Large and mostly obscure, so off by default |

```powershell
namefinder lexicon --dry-run               # counts and random samples, writes nothing
namefinder lexicon                         # adds reinstated + jargon + foreign (source 'lexicon', with a gloss)
namefinder lexicon --layers jargon --limit 500
namefinder lexicon --layers foreign --languages ita,fra,spa --min-languages 1   # one language family, wider net
namefinder score --mode root --lexicon foreign --limit 600   # root-score one layer, best-first
namefinder score --mode root --lexicon jargon --limit 600
```

The new words are unscored, so the coin loop ignores them until `score --mode root` has run (ranking uses root scores). `score --lexicon LAYER` scores one layer in the order `lexicon` inserted it (foreign: most widely shared words first), and `--limit` takes the first N of it. Each root-scoring call shows the model a one-line meaning for any word that has one (WordNet gloss, or the translation for foreign words), so rare and foreign words are scored on what they mean. The root prompt's "CANDIDATE TYPES" section tells the model how to treat them.

Changing `prompts/root_prompt.md` changes the root-prompt fingerprint, so scores from before and after are not strictly comparable. If the ranking matters at the margin, re-score the core roots too with `namefinder score --mode root --refresh`. Root scoring is `--mode root` and uses `prompts/root_prompt.md`. The list lives in `words` (`lexicon` column names the layer; `words.gloss` holds the meaning); `config.LEXICON_*` tunes the frequency floor, length, skip-domains and endings.

## Coin Custom Brand Variations (`namefinder coin`)

The coining engine algorithmically generates novel, pronounceable neologisms using five rule engines (suffixes, whole-syllable truncations, overlapping portmanteau blending, compounds and twists of winners), plus the LLM engine.

### Independent Per-Variation Sampling

Rather than drawing 2–3 base words and producing 20 variations of each (which causes repetitive clumping), **every single variation is an independent sample drawn with replacement from your scored vocabulary** using rank-weighted exponential decay ($P(r) \propto 2^{-r / k}$):

- **Top 10 candidates**: Sampled ~24% of the time.
- **Top 50 candidates**: Sampled ~74% of the time.
- **Ranks 51–300+**: Retain ~26% probability for long-tail discovery.
- **Root Protection**: Coining only samples pure dictionary roots (`source != 'coined'`), preventing previously coined words from compounding into awkward chimeras.

```powershell
# Continuous autonomous loop (samples roots -> coins -> checks RDAP -> scores winners):
namefinder coin --continuous --backend local

# Single-pass: draw 30 independent variations across your rankings:
namefinder coin --limit 30 --backend local

# Adjust the sampling half-life (default: 25.0; lower = more top-heavy, higher = broader):
namefinder coin --continuous --half-life 40.0 --backend local

# Coin from specific base words:
namefinder coin --bases spectral,fathom,lattice --limit 20

# Continuous loop focused on chosen roots (blends may still pair them with any other word):
namefinder coin --continuous --bases root

# Fast dry-run without RDAP checks or LLM scoring:
namefinder coin --no-domains --no-score --limit 50
```

---

### How Coined Names Are Generated

Each candidate comes from one of five rule engines. Four apply to a base word sampled by rank:

- **suffix**: swaps the ending for one of `COIN_SUFFIXES` (chosen by measured lift).
- **trunc**: clips a long base (7+ letters) to whole leading syllables and adds a suffix (casix from cascade).
- **portmanteau**: fuses two words at vowel/consonant seams.
- **compound**: two whole short words (each at most `COIN_COMPOUND_MAX_PART` letters) joined with no letters lost.

The fifth, **twist**, does not start from a root: it takes one of the best coined names (the top `COIN_TWIST_POOL`, 300, by composite, taken `.com`s included, since a strong name with a lost domain still has free neighbours) and makes one small edit, so the search keeps exploring around proven names. Operators: `vowel` (shift one vowel), `mutate` (near-word edit), `respell` (sound-preserving respelling) and `ending` (a different `COIN_SUFFIXES` ending). An edit must not read worse than its winner (`COIN_TWIST_PHONETIC_SLACK`, no new vowel pair, a speakable cluster). Names are stored as engine `twist` with `parents = base,winner`: the first entry is the winner's own base (so the base bandit credits it correctly), the second is the winner that was edited. Winners are drawn best-first (geometric by rank, mean 60). With `--bases`, only winners built on those roots are used.

Retired engines: `prefix` (2% tail rate), and after a measured run `vowel` (1 to 4%), `mutate` (5%, the baseline, against about 30% for the others in the same window) and `respell` (0 of 70). Names already coined with them keep their engine label, and their helpers (`vowel_shift`, `mutate_word`, `respell_word`) stay in `coin.py` for operators applied to proven winners.

Each name also records its **operator**, how the engine built it (`words.operator`): suffix `replace-1`, `replace-2`, `replace-3` or `append`; trunc `clip3` to `clip6` (letters kept); portmanteau and compound the partner kind (`concept` root or `scored` word), compounds also the order (`base+concept`, `scored+base`). Read them with `namefinder engines --by operator`.

Portmanteaus fuse two words at vowel/consonant seams, so both parents are shortened (each keeps at least 40% of its letters) and the join alternates sounds. Every candidate passes one quality gate before it is stored or scored: pronounceability (`phonetics.py`, floor `COIN_MIN_PHONETIC`), no real English words, no crude fragments (`COIN_BLOCK_SUBSTRINGS`), and not already in the database.

**Near-duplicates.** A candidate one letter away from an existing coined name (one letter added, removed or changed: `origagrapf` vs `origagraph`) is also dropped. The exception is when that neighbour scored in the top 5% of coined names, so variations on winners are still explored while respellings of weak names stop costing scoring calls. The LLM engine is shown its recently dropped near-copies and told to vary the idea, not a single letter. It also adjusts how many names it requests to the share that actually passes. When the rule engines come up short, the LLM fills the gap. Tune it with `COIN_NEAR_DUP` / `COIN_NEAR_DUP_KEEP_QUANTILE` in `config.py`. Before this filter, about half of a day's coined names were such respellings.

**Brevity and recomputing.** Brand composites carry a syllable penalty (`SYLLABLE_PENALTY` in `config.py`: 4% off for 3 syllables, 10% for 4+). After changing any weight or penalty in `config.py`, run `namefinder recompute` to re-derive every coined word's composite from its stored sub-scores. It makes no LLM calls and backs up the DB first (`--no-backup` to skip). Words with no verbable rating are treated as 0.5, so the first recompute also lowers unrated words by 4%.

**Re-scoring after a prompt change.** Every brand score records a fingerprint of the scoring prompts it was made with. After editing `prompts/system_prompt.md` or `prompts/user_prompt.md`, run `namefinder rescore` to re-score the best 10,000 coined names still on an older prompt (`--top-n` to change, `--max-cost` to cap spend; roughly $0.10 per 1,000 names on the default model). Rows are overwritten in place and the DB is backed up first. A rerun skips names already current and continues down the list. Re-scored names lose their refine flag, so run `namefinder refine` afterwards. Brand scoring never stores a word the model returned without scores; such words stay unscored until the next `score --mode brand`.

**Tournament.** Scores in isolation are noisy and saturate at the top, so `namefinder tournament` ranks the best names *against each other*. The pool is your top coined names with a free `.com` (`--pool`, default 300) plus reference names from `prompts/tournament_refs.txt` (or `--ref`), which always enter even if they are not in the database, so a rating reads as "better or worse than origalia". Each round sorts the pool by rating, cuts it into groups of 5 and shows every group to the judge twice, the second time in reverse order to cancel position bias. The judge ranks each group best to worst and states each name's biggest flaw; rankings become Elo updates. It prints how consistent the judge was across the two orders (100% = stable). Runs resume: ratings and a match log live in the database, so more `--rounds` sharpen the ranking. Ratings are tagged with the judge model and a hash of the judge prompt (`prompts/tournament_prompt.md`, else a built-in prompt using the product brief); change either and ratings start again, while old sets stay stored. Pick the judge with `--model` or `OPENROUTER_TOURNAMENT_MODEL` (default: the scoring model). A 300-name, 6-round run is roughly 700 calls; `--show-only` prints the stored ranking for free. **Pool sources.** `--source score` (default) takes the top `--pool` coined names by the old composite. `--source ideas` takes the idea, variant and rediscovered names plus the best `--carry` rated names. `--source both` plays everything already rated, the idea names and the top `--pool` old names that no earlier tournament has rated, tagging those *elite* (selected earlier) and *bulk* (untouched) so the ranking ends with a "Rating by source" table: names per source, mean and median, and how many reach the top 25, 50 and 100. `--source rated` plays the top `--pool` already-rated names plus any unrated idea or variant names, which is how to give the leaders more games and how new variants join. Near the top the judge agrees with itself much less (about 60% of pairs, against 85% on a mixed pool), so treat neighbouring ranks as ties and choose among the top few dozen yourself.

**Ratings, pruning and cost.** Ratings are a Plackett-Luce fit over every logged match of the judge (`namefinder/ratings.py`), refitted after each round, with a standard error per name; on this project's data it is far more reliable than a running Elo (split-half rank correlation 0.76 vs 0.30 on the top 100), and it accounts for how strong each name's opponents were. `--refit` refits from the stored matches without any API calls. Groups hold 8 names by default (`--group-size`; 2.4x cheaper per comparison than 5 at the same consistency), each group is judged twice in opposite order, and only the first ordering asks for flaw notes. Pruning is on by default: once a name has `--min-games` (4) games and its rating plus `--z` (2) standard errors is still below the 100th best (`--prune-rank`), it stops playing; reference names always play, `--prune-below WORD` adds a benchmark name, `--margin` adds slack and `--no-prune` turns it off. A name with no rating yet is placed at a random point of the current rating spread so its first opponents are a fair cross-section. Without a pinned provider, OpenRouter requests go to the cheapest provider for the model (`OPENROUTER_SORT=price`, the default; `off` restores OpenRouter's balancing), about 3x cheaper per judge call with no measurable loss in consistency.

**Reading the results in `top`:** `namefinder top` still ranks by the composite score, but any name the tournament has rated gets an `Elo` column (rating and rank). `namefinder top --by tournament` ranks by rating instead, over rated names only, and the usual filters (`--free-domains`, `--clean`, `--spoken`, `--new`) still apply. It reads the rating set for the configured judge model and current judge prompt, falling back to the latest set with a note; `--judge MODEL` picks another.

**Idea-first generation.** Instead of scoring tens of thousands of cheap candidates, this path spends effort on a few good ideas and judges the results by tournament. The loop is:

1. `namefinder ideas generate` asks the model for ideas, several per *angle*. An idea is one statement plus the roots (Latin or Greek by default) that could carry it, e.g. "every copy has a parent" with `stemma`, `propago`. Ideas come at two levels: *mechanism* (how the product works) and *purpose* (what it lets a journalist do or understand, such as finding copies no single search shows or seeing where the trail goes cold). The angles are origin, spread, evidence, discovery, gaps, synthesis, inquiry, time, alteration and exploration; `--angle` picks some, and `--per-angle` (default 5) is scaled by a weight per angle, so the central ones get more ideas and the data-feature ones (time, alteration, exploration) fewer. The model sees every existing idea across angles, and rejected ones, so it does not restate a point. Afterwards a merge pass folds duplicate ideas (same point, different wording) into one and unites their root lists, so a single idea carries a wide root menu (`--no-merge` skips it; `ideas merge` runs it alone, and never rewrites an idea you approved).
2. `namefinder ideas review` walks through pending ideas: `a` approve, `r` reject, `e` edit the wording, `s` skip, `q` quit. This is the step where your taste matters most. Without a terminal, use `ideas list` and `ideas mark --approve 1 4 7 --reject 2`.
3. `namefinder generate` makes `--per-idea` names (default 20) for every approved idea, one request per idea, each name with a short construction note ("imago + -ome: the whole body of copies of one image"). Names pass the same quality gate as the old engines and their `.com` availability is checked (`--all-tlds` also checks `.io` and `.dev`; `.dev` is rate-limited by Google's registry and makes the check several times slower; `--no-domains` skips it). A stem (first four letters) may appear at most twice per idea and five times per run, so one root with many endings does not stand in for many names. If a tournament has run, the model is also shown the best-rated names and the flaws the judge named (`--no-feedback` turns that off). Names the old engines already made are not re-inserted; they are recorded as *rediscovered* (the idea process found them independently) and can enter the tournament.
4. `namefinder tournament --source ideas` ranks them. The pool is every idea/variant/rediscovered name with a free `.com`, your reference names, and the best `--carry` (default 60) previously rated names as competition. New names move faster for their first few games. The ranking gains a `Src` column (`idea`, `variant`, `both` = found by both processes, `old`, `ref`) and the caption counts where the top 20 came from, which is how you tell whether this path beats the old one.
5. `namefinder evolve` takes the best-rated names (`--top`, default 20, your references included unless `--no-refs`) with the flaws the judge named for each, and asks for `--variants` redesigns that keep the root and fix the flaw. Variants remember their parent. Run `tournament --source ideas` again, then `evolve` again, until the top stops changing.

Each stage takes `--model` (defaults `OPENROUTER_IDEAS_MODEL` and `OPENROUTER_NAMEGEN_MODEL`, else the scoring model) and `--max-cost`. Prompts can be overridden in `prompts/ideas_prompt.md`, `names_prompt.md` and `evolve_prompt.md` (placeholders `{product}`, `{max_len}`); the defaults use the product brief. Few names have a free `.com` (often 5 to 10% of proposals), so volume of approved ideas matters more than names per idea.

**The product brief.** `prompts/product.md` (private; falls back to `prompts/product.example.md`) describes the product in a few lines. The refine pass and the built-in LLM coin prompt include it, so edit it when the product or its voice changes. The scoring, root, coin and final prompts each carry their own fuller description; keep them consistent with it.

**Steering toward verbs.** Names that work as "I just ___ it" are favoured three ways, all tunable in `config.py` (`COIN_VERB_*`, `COIN_LEARN_VERB`): (1) a free `phonetics.verb_score` (short, hard-consonant ending, clean -ed/-ing) softly biases every engine, so unlikely verbs are kept less often but never banned, and also helps pick among splice options; (2) parents whose common -ing/-ed forms exist in wordfreq (trace, link, focus) are picked up to twice as often; (3) engine and partner learning now weights each composite by how verb-like the LLM rated the name.

**LLM coining (`engine='llm'`).** A sixth engine asks a model to invent names from your top base words. `--llm-share` (default 0.3, `0` disables) sets the fraction of each batch it supplies; `--coin-model` / `OPENROUTER_COIN_MODEL` picks its model (default: the scoring model). The model only proposes: every name goes through the same quality gate, de-duplication, scoring, domain check and engine learning as the rule engines, and is stored with its parent word. Two feedback loops run automatically: the prompt shows the model your best and worst earlier LLM names plus the names the filters recently rejected (and, for parents from the extra layers, the meaning stored in `words.gloss`, so the model builds on what a rare or foreign word means), and once 15+ rated LLM names exist its share is scaled between 0.5x and 2x by how they score against the rule engines (printed as `[learn] llm share`). Override the system prompt with `prompts/coin_prompt.md` (gitignored). A failed LLM request never stops the loop. It runs under the same `--max-cost`. One-shot `coin --limit N` uses it too; `--bases` runs stay rule-only.

Each coined word records its engine and parents. Between cycles the loop re-weights engines and portmanteau and compound partner roots by the scores their earlier words earned (shrunk toward the overall mean, with 30% kept uniform so no engine starves), and prints the engine weights each cycle.

**Focus mode (`coin --continuous --bases a,b`).** Every name is built on the given roots; portmanteaus still blend them with any scored word or concept root. The LLM engine sees each root's strongest existing names (to build on, not repeat), takes its best/worst feedback from names on those roots, and fills any gap the rule engines leave once a root's simple variations run out.

### Base Value and the Base Bandit (`namefinder bases`)

Some bases produce far better names than others. A base's value is its **tail rate**: the share of its scored coined names that reach the global top 5% of coined composites, shrunk toward a 5% prior worth 20 names, `(hits + 1) / (names + 20)`. A raw maximum would favour bases that have simply been tried most, and a mean rewards consistent mediocrity; the tail rate measures how often a base produces a winner, per attempt. A base is the first entry of `words.parents`, so blends credit their base, not their partner.

```powershell
namefinder bases                          # top 30 bases by tail rate
namefinder bases --sort recent -n 50      # by rate over each base's last 150 names
namefinder bases --sort best --min-names 50
namefinder bases --spoken                 # only names the LLM spelled out (what the coin loop ranks on)
```

The coin loop's bandit judges bases only on names with a spoken review (`BASE_SPOKEN_ONLY`). Older names scored without one are more lenient, so mixing them in would favour bases that were coined heavily before the review existed. Until 500 such names exist (`BASE_SPOKEN_MIN_NAMES`), the loop falls back to all names, and the `[learn] bases favoured (...)` line says which set it used.

The `recent` column is red when a base's last 150 names hit well below its all-time rate (it is running dry) and green when it is improving.

Each coin cycle draws a hit rate for every base from `Beta(1 + hits, 19 + misses)` over its recent window (Thompson sampling) and multiplies that base's sampling weight by the draw relative to the 5% prior, printed as `[learn] bases favoured`. Proven bases are picked more, exhausted ones fade, and untried bases still get explored. The dictionary rank prior fades as a base gathers evidence (half its influence after 50 names), so a strong base deep in the ranking can still be chosen often. In focus mode with several roots, the bandit splits picks between them. Tune or disable it with `BASE_*` in `config.py` (`BASE_BANDIT = False` restores rank-only sampling).

### Engine Report (`namefinder engines`)

Shows which coining engines produce top-tier names, so engine and rule changes can be judged on data.

```powershell
namefinder engines                         # tail rate, mean and best name per engine
namefinder engines --threshold 0.372       # pin the tail threshold to compare before/after a change
namefinder engines --lift ending           # tail-rate lift by the last two letters (also: last, start, length)
namefinder engines --by operator --since 6h  # tail rate per engine:operator (names coined since operators were tracked)
namefinder engines --by layer --since 6h     # tail rate by the base word's layer: core, reinstated, jargon, foreign
namefinder engines --spoken                # only names with an LLM pronunciation review
```

The tail threshold defaults to the top 5% of coined composites, which moves as the pool grows. Pin it with `--threshold` when comparing a new engine against an old run, and wait for a few hundred scored names per engine before reading its rate. `--lift` is how `COIN_SUFFIXES` in `config.py` was chosen: endings with lift under 1x (`-el`, `-ar`, `-or`) were dropped, the 2x-plus ones (`-ex`, `-um`, `-os`) kept. The retired `prefix` engine and the rewritten `trunc` (whole-syllable clips) and `vowel` (best-reading single vowel shift) engines are judged the same way.

## View Rankings and Status in CLI

```powershell
# View top 25 overall candidates in terminal:
namefinder top

# View only candidates with verified free domains (.com, .io, or .dev):
namefinder top --free-domains

# View only candidates clear of trademark conflicts:
namefinder top --clean

# View more rows:
namefinder top --free-domains --top 50

# Inspect overall pipeline database counts and funnel progression:
namefinder status
```

---

## Cache Management & Re-running

All stages record progress in `data/namefinder.db`. Re-running any stage automatically skips already processed candidates.

```powershell
# Clear all cached scores from the database without re-running:
namefinder score --clear

# Force a stage to re-evaluate from scratch (bypasses cache):
namefinder prepare --refresh
namefinder score --refresh
namefinder check-domains --refresh
namefinder check-trademarks --refresh
namefinder rank-final --refresh
```

---

## Flags Accepted Across Stages

| Flag | Subcommands | Description |
|---|---|---|
| `--limit N` | All stages | Process only the first N candidates (useful for testing) |
| `--refresh` | All stages | Ignore cached answers and re-evaluate |
| `--clear` | `score` | Back up the database to `data/backups/`, then delete all scores and coined words |
| `--free-domains-only` | `score` | Score only candidates that have at least one verified free domain |
| `--mode {auto,root,brand}` | `score` | Scoring rubric: `root` (base potency for coining), `brand` (standalone name), `auto` (default) |
| `--backend {local,openrouter}` | `score`, `coin` | Scoring engine (`local` for LM Studio, `openrouter` for cloud) |
| `--continuous`, `-c` | `coin` | Run autonomous generation, RDAP check, and scoring loop |
| `--half-life FLOAT` | `coin` | Rank falloff half-life for base word sampling (default: `25.0`) |
| `--top-bases N` | `coin` | Number of base words to sample across vocabulary (default: `25`) |
| `--bases WORD1,WORD2` | `coin` | Explicit root words to coin from; with `--continuous`, focus the loop on them |
| `--sort KEY` / `--min-names N` | `bases` | Sort by `rate` (default), `recent`, `best`, `names` or `hits`; hide bases with fewer names |
| `--spoken` | `bases` | Count only names with an LLM pronunciation review; the hit threshold is taken from those names too |
| `--min-len N` / `--max-len N` | `coin` | Character length constraints for coined words (default: 5–10) |
| `--no-domains` | `coin` | Skip automatic RDAP domain checks |
| `--no-score` | `coin` | Skip automatic LLM brand scoring |
| `--top N`, `-n N` | `top` | Number of ranked candidates to display (default: 25) |
| `--free-domains` | `top` | Filter display to candidates with at least one free domain |
| `--all-free` | `top` | Only candidates whose every checked domain is free (words with no domain check are excluded) |
| `--clean` | `top` | Filter display to candidates clear of trademark conflicts |
| `--spoken` | `top` | Only names with an LLM pronunciation review (spelled out by brand scoring or `refine`); hides older rows judged by code-side phonetics alone |
| `--bases-only` | `top` | Hide coined names; show dictionary base words only |
| `--since AGE` | `top` | Mark names scored within the window with a yellow `NEW` and names refined within it with a cyan `REF` (units `s`, `m`, `h`, `d`, `w`, combinable and spellable in full: `30s`, `10m`, `1h30m`, `6h`, `2d`, `1w`, `"2 days"`; a bare number is minutes; `all` marks everything; default `10m` (`TOP_SINCE` in `config.py`); `0`/`off` turns marks off). Names refined before this feature existed carry no timestamp, so they are never marked `REF` |
| `--new` | `top` | Show only names marked `NEW` or `REF`; combine with `-n` and the other filters |
| `--batch N` / `--domain-top F` / `--concurrency N` | `coin` | Continuous mode: coined words per cycle (default 120), fraction of each cycle's best scores that get a domain check (default 0.25; `1.0` = all), parallel scoring and LLM-coining requests (default 20; rate limits back off automatically) |
| `--max-cost USD` | `score`, `coin`, `refine` | Stop sending paid requests once this much is spent in the command (default `$1.00`, env `NAMEFINDER_MAX_COST`; `0` = no limit). In-flight requests finish, so spend can overshoot slightly; progress is saved and a rerun continues |
| `--top-n N` / `--samples N` | `refine` | How many top coined words to review, and independent reviews per word (defaults: 100 / 3) |
| `--model MODEL` | `score`, `rank-final` | Override the model ID |
| `--concurrency N` | `score` | Number of parallel requests (default: 6) |
| `--skip-llm` | `rank-final` | Write reports from cached SQLite data without calling an LLM |
| `--db PATH` | All stages | Override SQLite database file path |

---

## Spoken-Name Review and Ranking Stabilisation (`namefinder refine`)

**Pass 1 (`coin` and `score --mode brand`) spells every name out.** Before any score, the model writes the syllables, a say-aloud respelling with stress (`LOOM-trays`), how a listener would spell it after hearing it once, and its sound-alikes. Then it rates `sayable`, `ambiguity` and `mishear_risk`. `score.spoken_review` blends those with the code-side checks in `namefinder/phonetics.py`:

- **Phonetic score:** half the code-side score, half `sayable * (1 - 0.5 * ambiguity)`. This feeds the pronounceability gate.
- **Heard spelling:** if the heard spelling differs from the word, ambiguity is at least 0.4.
- **Sound-alikes:** a damaging sound-alike raises `neg`, so `seenly` heard as "seemly" loses points.

The questions are added in code, so private prompt files need no edits. If a private `user_prompt.md` already asks for `"say_aloud"`, nothing is appended.

**Pass 2 (`refine`) stabilises the top of the table.** A single pass-1 answer is noisy, and the top of a large pool is mostly the luckiest samples. Refine asks the same spoken-name questions several more times for only the best coined words, at varied temperatures. It averages those answers together with the pass-1 answer and writes the result back.

Names scored before pass 1 spelled words out keep their old, code-side-only phonetic score, which is more lenient. Run `refine` over the top few hundred to put them on the same footing as new names.

```powershell
namefinder coin --continuous --backend local     # pass 1 (builds the pool)
namefinder refine --top-n 100 --samples 3        # pass 2 on the best 100 coined words
namefinder refine --free-domains-only --backend openrouter --model <stronger-model>
namefinder top --free-domains                    # shows syllables / say-aloud / sound-alike risks
```

Pass 1 already applies a pronounceability gate and a small penalty to names that are just a generic prefix (`meta`, `pan`, `ultra`, ...) on a dictionary word. Tune both in `namefinder/config.py` (`PHONETIC_WEIGHT`, `GENERIC_PREFIX_PENALTY`). Coined names also get a **verbable** axis (0-1): does the name work as a verb in "I just ___ it and found what I needed" (googled, pinged, grepped)? It is a slight multiplier, `composite *= (1 - VERB_BONUS) + VERB_BONUS * verbable` (default 0.08, so 0.92 to 1.0; set `VERB_BONUS = 0` to disable) and appears as the `Verb` column in `top`. Both passes ask for it; the pass-1 question is added in code, so private prompt files need no edits. Words scored before this existed show `-` and keep their old composite until re-scored or refined (a recompute treats them as 0.5). Override the review prompt with `prompts/refine_prompt.md` and `prompts/refine_user_prompt.md` (gitignored).

---

## Output Files

| File | What it is |
|---|---|
| `data/namefinder.db` | SQLite cache — all stages write here (WAL mode) |
| `results/shortlist.md` | Final ranked markdown table (Stage 5) |
| `results/shortlist.csv` | Same shortlist data as CSV |

Each stage is resumable — stop and restart anytime. Cached rows are skipped automatically.
