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

# 3. Check domains via keyless RDAP (.com, .dev, .app)
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

# Fast dry-run without RDAP checks or LLM scoring:
namefinder coin --no-domains --no-score --limit 50
```

---

## View Rankings and Status in CLI

```powershell
# View top 25 overall candidates in terminal:
namefinder top

# View only candidates with verified free domains (.com, .dev, or .app):
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
| `--clear` | `score` | Wipe all cached scores from database without re-scoring |
| `--free-domains-only` | `score` | Score only candidates that have at least one verified free domain |
| `--mode {auto,root,brand}` | `score` | Scoring rubric: `root` (base potency for coining), `brand` (standalone name), `auto` (default) |
| `--backend {local,openrouter}` | `score`, `coin` | Scoring engine (`local` for LM Studio, `openrouter` for cloud) |
| `--continuous`, `-c` | `coin` | Run autonomous generation, RDAP check, and scoring loop |
| `--half-life FLOAT` | `coin` | Rank falloff half-life for base word sampling (default: `25.0`) |
| `--top-bases N` | `coin` | Number of base words to sample across vocabulary (default: `25`) |
| `--bases WORD1,WORD2` | `coin` | Explicit root words to coin from |
| `--min-len N` / `--max-len N` | `coin` | Character length constraints for coined words (default: 5–10) |
| `--no-domains` | `coin` | Skip automatic RDAP domain checks |
| `--no-score` | `coin` | Skip automatic LLM brand scoring |
| `--top N`, `-n N` | `top` | Number of ranked candidates to display (default: 25) |
| `--free-domains` | `top` | Filter display to candidates with at least one free domain |
| `--clean` | `top` | Filter display to candidates clear of trademark conflicts |
| `--model MODEL` | `score`, `rank-final` | Override the model ID |
| `--concurrency N` | `score` | Number of parallel requests (default: 6) |
| `--skip-llm` | `rank-final` | Write reports from cached SQLite data without calling an LLM |
| `--db PATH` | All stages | Override SQLite database file path |

---

## Output Files

| File | What it is |
|---|---|
| `data/namefinder.db` | SQLite cache — all stages write here (WAL mode) |
| `results/shortlist.md` | Final ranked markdown table (Stage 5) |
| `results/shortlist.csv` | Same shortlist data as CSV |

Each stage is resumable — stop and restart anytime. Cached rows are skipped automatically.
