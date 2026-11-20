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

# 5. Export final shortlist
namefinder rank-final --skip-llm
```

---

## Coin Custom Brand Variations (`namefinder coin`)

The coining engine algorithmically generates novel, pronounceable neologisms using 5 mutation engines (prefixes, Latin/Greek suffixes, overlapping portmanteau blending, syllabic truncations, and vowel shifts).

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

Each candidate comes from one of five engines (prefix, suffix, truncation, portmanteau, vowel shift) applied to a base word sampled by rank. Portmanteaus fuse two words at vowel/consonant seams, so both parents are shortened (each keeps at least 40% of its letters) and the join alternates sounds. Every candidate passes one quality gate before it is stored or scored: pronounceability (`phonetics.py`, floor `COIN_MIN_PHONETIC`), no real English words, no crude fragments (`COIN_BLOCK_SUBSTRINGS`), and not already in the database.

**Brevity and recomputing.** Brand composites carry a syllable penalty (`SYLLABLE_PENALTY` in `config.py`: 4% off for 3 syllables, 10% for 4+). After changing any weight or penalty in `config.py`, run `namefinder recompute` to re-derive every coined word's composite from its stored sub-scores. It makes no LLM calls and backs up the DB first (`--no-backup` to skip). Words with no verbable rating are treated as 0.5, so the first recompute also lowers unrated words by 4%.

**Steering toward verbs.** Names that work as "I just ___ it" are favoured three ways, all tunable in `config.py` (`COIN_VERB_*`, `COIN_LEARN_VERB`): (1) a free `phonetics.verb_score` (short, hard-consonant ending, clean -ed/-ing) softly biases every engine, so unlikely verbs are kept less often but never banned, and also helps pick among splice options; (2) parents whose common -ing/-ed forms exist in wordfreq (trace, link, focus) are picked up to twice as often; (3) engine and partner learning now weights each composite by how verb-like the LLM rated the name.

**LLM coining (`engine='llm'`).** A sixth engine asks a model to invent names from your top base words. `--llm-share` (default 0.3, `0` disables) sets the fraction of each batch it supplies; `--coin-model` / `OPENROUTER_COIN_MODEL` picks its model (default: the scoring model). The model only proposes: every name goes through the same quality gate, de-duplication, scoring, domain check and engine learning as the rule engines, and is stored with its parent word. Two feedback loops run automatically: the prompt shows the model your best and worst earlier LLM names plus the names the filters recently rejected, and once 15+ rated LLM names exist its share is scaled between 0.5x and 2x by how they score against the rule engines (printed as `[learn] llm share`). Override the system prompt with `prompts/coin_prompt.md` (gitignored). A failed LLM request never stops the loop. It runs under the same `--max-cost`. One-shot `coin --limit N` uses it too; `--bases` runs stay rule-only.

Each coined word records its engine and parents. Between cycles the loop re-weights engines and portmanteau partner roots by the scores their earlier words earned (shrunk toward the overall mean, with 30% kept uniform so no engine starves), and prints the engine weights each cycle.

**Focus mode (`coin --continuous --bases a,b`).** Every name is built on the given roots; portmanteaus still blend them with any scored word or concept root. The LLM engine sees each root's strongest existing names (to build on, not repeat), takes its best/worst feedback from names on those roots, and fills any gap the rule engines leave once a root's simple variations run out.

### Base Value and the Base Bandit (`namefinder bases`)

Some bases produce far better names than others. A base's value is its **tail rate**: the share of its scored coined names that reach the global top 5% of coined composites, shrunk toward a 5% prior worth 20 names, `(hits + 1) / (names + 20)`. A raw maximum would favour bases that have simply been tried most, and a mean rewards consistent mediocrity; the tail rate measures how often a base produces a winner, per attempt. A base is the first entry of `words.parents`, so blends credit their base, not their partner.

```powershell
namefinder bases                          # top 30 bases by tail rate
namefinder bases --sort recent -n 50      # by rate over each base's last 150 names
namefinder bases --sort best --min-names 50
```

The `recent` column is red when a base's last 150 names hit well below its all-time rate (it is running dry) and green when it is improving.

Each coin cycle draws a hit rate for every base from `Beta(1 + hits, 19 + misses)` over its recent window (Thompson sampling) and multiplies that base's sampling weight by the draw relative to the 5% prior, printed as `[learn] bases favoured`. Proven bases are picked more, exhausted ones fade, and untried bases still get explored. The dictionary rank prior fades as a base gathers evidence (half its influence after 50 names), so a strong base deep in the ranking can still be chosen often. In focus mode with several roots, the bandit splits picks between them. Tune or disable it with `BASE_*` in `config.py` (`BASE_BANDIT = False` restores rank-only sampling).

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
| `--min-len N` / `--max-len N` | `coin` | Character length constraints for coined words (default: 5–10) |
| `--no-domains` | `coin` | Skip automatic RDAP domain checks |
| `--no-score` | `coin` | Skip automatic LLM brand scoring |
| `--top N`, `-n N` | `top` | Number of ranked candidates to display (default: 25) |
| `--free-domains` | `top` | Filter display to candidates with at least one free domain |
| `--all-free` | `top` | Only candidates whose every checked domain is free (words with no domain check are excluded) |
| `--clean` | `top` | Filter display to candidates clear of trademark conflicts |
| `--bases-only` | `top` | Hide coined names; show dictionary base words only |
| `--since AGE` | `top` | Mark names scored within the window with a yellow `NEW` and names refined within it with a cyan `REF` (e.g. `90m`, `6h`, `2d`; default `24h`; `0` turns marks off). Names refined before this feature existed carry no timestamp, so they are never marked `REF` |
| `--new` | `top` | Show only names marked `NEW` or `REF`; combine with `-n` and the other filters |
| `--batch N` / `--domain-top F` / `--concurrency N` | `coin` | Continuous mode: coined words per cycle (default 120), fraction of each cycle's best scores that get a domain check (default 0.25; `1.0` = all), parallel scoring and LLM-coining requests (default 20; rate limits back off automatically) |
| `--max-cost USD` | `score`, `coin`, `refine` | Stop sending paid requests once this much is spent in the command (default `$1.00`, env `NAMEFINDER_MAX_COST`; `0` = no limit). In-flight requests finish, so spend can overshoot slightly; progress is saved and a rerun continues |
| `--top-n N` / `--samples N` | `refine` | How many top coined words to review, and independent reviews per word (defaults: 100 / 3) |
| `--model MODEL` | `score`, `rank-final` | Override the model ID |
| `--concurrency N` | `score` | Number of parallel requests (default: 6) |
| `--skip-llm` | `rank-final` | Write reports from cached SQLite data without calling an LLM |
| `--db PATH` | All stages | Override SQLite database file path |

---

## Spoken-Name Review (`namefinder refine`)

Pass 1 (`score` / `coin`) ranks everything cheaply. Pass 2 reviews only the best coined words: the model splits each word into syllables, respells it, writes how a listener would spell it after hearing it once, and lists embarrassing sound-alikes. Each word is reviewed several times and the answers are averaged, then blended with the code-side checks in `namefinder/phonetics.py`.

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
