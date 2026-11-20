# NameFinder

A resumable, stage-by-stage brand discovery, domain screening, trademark clearance, and neologism pipeline for software products.

NameFinder filters English vocabulary, scores brand resonance using local or cloud LLMs with calibrated conceptual rubrics, screens domain availability via RDAP, verifies trademark clearance against the USPTO, and programmatically generates novel brand variations through algorithmic linguistic coining.

All progress is persisted to an SQLite database (`WAL` mode) so any stage can be stopped, resumed, or rerun without redundant work.

---

## Core Capabilities

1. **Vocabulary Filtering (`prepare`)**
   - Scans 90,000 English words via `wordfreq`.
   - Constrains Zipf frequency (2.60–5.55) and word length (4–8 letters).
   - WordNet part-of-speech filtering to prune prepositions, conjunctions, and inflections.
   - Cleans suffixes (`-ing`, `-ed`, `-ly`, `-ness`, `-tion`, `-s`/`-es`) via stem-in-vocabulary testing.
   - Removes AI clichés and profanity.

2. **Calibrated Brand Scoring (`score`)**
   - Evaluates brand alignment against a 5-pillar conceptual rubric (Weaving, Depth/Investigation, Optics/Illumination, Ancestry, Logic/Proof).
   - Penalizes disjointed metaphors (e.g., arbitrary minerals/rocks) and generic everyday nouns.
   - Enforces collision checks: identifies homophone traps, typo traps, and awkward phonetic collisions, storing candid `why` and `why_not` reasoning.
   - **Early-Out Rule**: Automatically detects non-starters and exits in ~10 tokens, achieving ~2.1s per word on an RTX 3060 mobile GPU.
   - Supports both local inference (LM Studio, Ollama, vLLM on `localhost:1234`) and cloud inference (OpenRouter free and paid models).

3. **Keyless Domain Screening (`check-domains`)**
   - Queries official registry RDAP servers directly over HTTPS:
     - `.com` via Verisign (`rdap.verisign.com`)
     - `.dev` via Google Registry (`pubapi.registry.google`) and `.io` via Identity Digital (`rdap.identitydigital.services`)
   - Checks `{word}.com`, `{word}.io` and `{word}.dev` (configurable via `TLDS`, `CHECK_PLAIN_COM` and `USE_PREFIX` in `config.py`; older cached `.app` / `use` rows are ignored).
   - Rate-paced (≤ 4 req/s) with exponential backoff on HTTP 429.

4. **Trademark & Namespace Clearance (`check-trademarks`)**
   - **USPTO**: Keyless clearance via Markbase across Nice classes 9 (software), 42 (SaaS/AI), 35 (data/search), and 41 (education). Filters for active/pending marks with overlapping goods and services descriptions.
   - **PyPI**: Validates namespace availability against `pypi.org/pypi/{word}/json`.
   - **GitHub**: Screens for repository name collisions with ≥ 2 stars.
   - **IP Australia**: Optional local indexing of IPGOD CSV dumps placed in `data/ipgod/`.

5. **Algorithmic Lexical Coining (`coin`)**
   - 5 programmatic mutation engines:
     - **Prefix Blends**: Classical prefixes (`in-`, `syn-`, `con-`, `meta-`, `neo-`, `re-`, `epi-`, `ultra-`).
     - **Latin/Greek Suffixation**: (`-ic`, `-al`, `-is`, `-ex`, `-ix`, `-on`, `-or`, `-ia`, `-io`, `-oid`).
     - **Overlapping Portmanteaus**: Merges phonetic overlaps between root candidates (e.g., `inspect` + `spectral` → `Inspectral`).
     - **Vowel Shifts (Ablaut)**: Controlled phonotactic vowel mutations.
     - **Syllabic Truncation (Apocope) + Coda**: Truncates stem and attaches crisp phonetic endings (`-x`, `-ix`, `-is`, `-on`).
   - Phonotactic pruning: Enforces syllable structure, avoids unpronounceable consonant clusters, and rejects repetitive n-grams.

6. **Continuous Autonomous Discovery Loop (`coin --continuous`)**
   - Implements rank-based exponential falloff sampling ($P(r) \propto 2^{-r / k}$) with replacement across the scored vocabulary.
   - Rather than clumping variations around a few base words, every generated candidate is an independent draw across the ranking.
   - Automatically chains candidate generation → RDAP domain verification → LLM scoring of domain survivors.
   - Displays live discovery alerts and handles `Ctrl+C` gracefully without data loss.

7. **Terminal Ranking Inspector (`top`)**
   - Displays ranked candidates directly in the terminal with live domain status (`.com`, `.io`, `.dev`), trademark flags, and model reasoning.

---

## Installation

### Requirements
- Python 3.10 or higher
- Git
- (Optional) Local LLM runner such as LM Studio or Ollama

### Setup

```bash
# Clone the repository
git clone https://github.com/SnowCode1/NameFinder.git
cd NameFinder

# Create and activate virtual environment
python -m venv .venv

# On Linux / macOS:
source .venv/bin/activate

# On Windows (PowerShell):
.venv\Scripts\Activate.ps1

# Install editable package dependencies
pip install -e .

# (Optional) Copy environment template
cp .env.example .env
```

---

## Usage Guide

### 1. Preparing the Dictionary Vocabulary

```bash
# Prepare ~11,000 filtered English words:
namefinder prepare

# Quick check on database status:
namefinder status
```

### 2. Brand Scoring

#### Option A: Local GPU (LM Studio / Ollama) — Recommended for $0 Budget
Start LM Studio, load an instruct model (e.g. `Qwen 2.5/3 Instruct` or `Llama 3.1/3.2`), and start the local server on port 1234.

```bash
# Score the first 100 base words locally:
namefinder score --backend local --limit 100

# Score 1,000 base words:
namefinder score --backend local --limit 1000
```

#### Option B: Cloud (OpenRouter)
Add `OPENROUTER_API_KEY=your_key_here` to `.env`:

```bash
# Score using OpenRouter (defaults to z-ai/glm-5.3-flash):
namefinder score --backend openrouter --limit 100
```

### 3. Continuous Neologism Coining & Discovery

Run the autonomous discovery engine. It continuously samples base words across your scored rankings using exponential decay, coins variations, checks RDAP domain availability, scores survivors, and stores winners:

```bash
# Run continuous discovery loop (press Ctrl+C anytime to pause):
namefinder coin --continuous --backend local

# Run a single-pass coining run:
namefinder coin --limit 30 --backend local

# Coin from specific base words:
namefinder coin --bases spectral,fathom,lattice --limit 20
```

### 4. Direct Domain & Trademark Clearance on Base Words

If you wish to screen original dictionary words for domain and trademark clearance directly:

```bash
# Check RDAP domains for the top 500 scored words:
namefinder check-domains --top-n 500

# Run USPTO, PyPI, and GitHub collision checks:
namefinder check-trademarks --top-n 100
```

### 5. Inspect Rankings in the Terminal

```bash
# View top 25 overall candidates:
namefinder top

# View only candidates with verified free domains (.com, .io, or .dev):
namefinder top --free-domains --top 50

# View only candidates clear of trademark conflicts:
namefinder top --clean
```

### 6. Generate Shortlist Report

```bash
# Export ranked markdown and CSV reports to results/:
namefinder rank-final --skip-llm
```

---

## CLI Reference

| Flag | Applicable Stages | Description |
|---|---|---|
| `--limit N` | All stages | Process only the first N candidates (useful for testing) |
| `--refresh` | All stages | Bypass cache and re-run the stage |
| `--clear` | `score` | Back up the database to `data/backups/`, then delete all scores and coined words |
| `--free-domains-only` | `score` | Score only words that have at least one free domain |
| `--backend {local,openrouter}` | `score`, `coin` | Select scoring backend (`local` for LM Studio, `openrouter` for cloud) |
| `--continuous`, `-c` | `coin` | Run autonomous generation, domain check, and scoring loop |
| `--half-life FLOAT` | `coin` | Rank falloff half-life for base word sampling (default: `25.0`) |
| `--top-bases N` | `coin` | Number of candidates to sample when coining |
| `--min-len N` / `--max-len N` | `coin` | Candidate character length bounds (default: 5–10) |
| `--top N`, `-n N` | `top` | Number of rows to display (default: 25) |
| `--free-domains` | `top` | Show only candidates with at least one free domain |
| `--clean` | `top` | Show only candidates clear of trademark conflicts |
| `--bases-only` | `top` | Hide coined names; show dictionary base words only |

---

## Project Structure

```text
NameFinder/
├── namefinder/
│   ├── __init__.py       # Package marker
│   ├── __main__.py       # CLI dispatch entrypoint
│   ├── cli.py            # Command-line interface and argument parser
│   ├── config.py         # Config constants, weights, endpoints, rate limits
│   ├── db.py             # SQLite repository and schema management
│   ├── net.py            # HTTP client helpers, rate pacers, retry logic
│   ├── words.py          # Stage 1: Wordlist ingestion, WordNet POS pruning
│   ├── score.py          # Stage 2: LLM brand resonance scoring & early-out
│   ├── domains.py        # Stage 3: Async RDAP domain availability engine
│   ├── trademarks.py     # Stage 4: USPTO (Markbase), PyPI, GitHub clearance
│   ├── final.py          # Stage 5: Final composite ranking and report export
│   └── coin.py           # Algorithmic neologism engine & continuous discovery
├── pyproject.toml        # Build configuration and dependencies
├── .env.example          # Environment variables template
├── .gitignore            # Git exclusion rules
├── USAGE.md              # Detailed stage-by-stage command cheatsheet
└── README.md             # Project documentation
```

---

## License

MIT
