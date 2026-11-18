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

## Run the pipeline

```bash
# Stage 1 — build the wordlist (~11,000 words, no key needed)
namefinder prepare

# Stage 2 — vibe-score words (needs OPENROUTER_API_KEY in .env, free models supported)
namefinder score

# Stage 2 with explicit limit or model override:
namefinder score --limit 50
namefinder score --model nvidia/nemotron-3.5-lightning:free

# Stage 3 — check domain availability (.dev, .app, use<word>.com, <word>.com; no key needed)
namefinder check-domains

# Stage 4 — screen trademarks + repo collisions (USPTO, PyPI, GitHub; no key needed)
namefinder check-trademarks

# Stage 5 — write shortlist.md and shortlist.csv (needs OPENROUTER_API_KEY in .env)
namefinder rank-final

# Stage 5 alternative without LLM key (uses cached data & composite scores):
namefinder rank-final --skip-llm
```

---

## Coin custom brand variations (Neologisms)

Programmatically generate custom words from top winners or specific bases (supports prefixes like `in-`, `re-`, portmanteau blending like `inspect` + `spectral` -> `inspectral`, and classical suffixes):

```bash
# Coin variations from specific bases (e.g. spectral, quartz, atlas, fathom):
namefinder coin --bases spectral,quartz,atlas,fathom

# Run continuous autonomous discovery loop (samples full ranking with exponential decay, coins, checks RDAP, scores winners):
namefinder coin --continuous --backend local

# Single-pass coining across vocabulary with rank-based exponential falloff (half-life: 25):
namefinder coin --top-bases 25 --half-life 25 --backend local

# Options:
namefinder coin --bases spectral,fathom --limit 30
namefinder coin --no-domains     # skip domain check
namefinder coin --no-score       # skip LLM scoring
```

---

## View rankings and status in CLI

```bash
# View top ranked candidates in the terminal with domains & trademarks
namefinder top

# Filter by availability
namefinder top --top 50
namefinder top --free-domains          # show only words with an available domain
namefinder top --clean                 # show only words clear of trademark conflicts

# See pipeline funnel progress & status overview
namefinder status
```

---

## Between stages

```bash
# Clear all cached scores from the database without running scoring:
namefinder score --clear

# Re-run a stage on fresh data (bypasses cache)
namefinder prepare --refresh
namefinder score --refresh
namefinder check-domains --refresh
namefinder check-trademarks --refresh
namefinder rank-final --refresh

# Test with a small set (dry-run)
namefinder prepare --limit 50
namefinder score --limit 20
namefinder check-domains --limit 10
namefinder check-trademarks --limit 10
```

---

## Flags accepted across stages

| Flag | Where accepted | What it does |
|---|---|---|
| `--limit N` | All stages | Process only the first N words (dry-run) |
| `--refresh` | All stages | Ignore cache and re-run the stage |
| `--clear` | `score` | Wipe all cached scores from the database without re-scoring |
| `--free-domains-only` | `score` | Score ONLY candidates that already have a free domain |
| `--top-n N` | `score`, `check-domains`, `check-trademarks` | Keep only top N candidates |
| `--top N` | `top` | Number of ranked candidates to display (default: 25) |
| `--free-domains` | `top` | Show only candidates with at least one free domain |
| `--clean` | `top` | Show only candidates clear of trademark conflicts |
| `--backend {openrouter,local}` | `score` | Scoring backend (default: `openrouter`; 'local' for LM Studio) |
| `--concurrency N` | `score` | Parallel API calls (default: 6) |
| `--model MODEL` | `score`, `rank-final` | Override the model ID |
| `--skip-llm` | `rank-final` | Write report from cached data without calling OpenRouter |
| `--no-markbase` | `check-trademarks` | Skip USPTO trademark screening |
| `--no-ipgod` | `check-trademarks` | Skip local IP Australia CSV screening |
| `--no-pypi` | `check-trademarks` | Skip PyPI package name collision check |
| `--no-github` | `check-trademarks` | Skip GitHub AI repo search |
| `--db PATH` | All stages | Override SQLite database file path |

---

## Output files

| File | What it is |
|---|---|
| `data/namefinder.db` | SQLite cache — all stages write here (WAL mode) |
| `results/shortlist.md` | Final ranked markdown table (Stage 5) |
| `results/shortlist.csv` | Same shortlist data as CSV |

---

## Stage-by-stage flow

```
11,474 words (prepare)
    │
    ▼
~500 top-scoring (score)  [or top by frequency if stage 2 is skipped]
    │
    ▼
Words with a free domain (check-domains)
    │
    ▼
Clean of trademarks and repo collisions (check-trademarks)
    │
    ▼
Final ranked shortlist (rank-final / rank-final --skip-llm / namefinder top)
```

Each stage is resumable — stop and restart anytime. Cached rows are skipped automatically.
