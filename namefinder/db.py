"""SQLite cache. Every stage writes here so reruns never repay for work."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS words (
    word       TEXT PRIMARY KEY,
    pos        TEXT,
    zipf       REAL,
    source     TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS scores (
    word       TEXT NOT NULL,
    qversion   TEXT NOT NULL,
    model      TEXT,
    fit        REAL,
    natural    REAL,
    spellable  REAL,
    memorable  REAL,
    neg        REAL,
    taken      REAL,
    common     REAL,
    composite  REAL,
    why        TEXT,
    why_not    TEXT,
    raw        TEXT,
    scored_at  TEXT,
    PRIMARY KEY (word, qversion)
);
CREATE INDEX IF NOT EXISTS idx_scores_comp ON scores(qversion, composite DESC);

CREATE TABLE IF NOT EXISTS domains (
    domain     TEXT PRIMARY KEY,
    word       TEXT NOT NULL,
    tld        TEXT NOT NULL,
    status     TEXT NOT NULL,          -- available | registered | unknown
    http_status INTEGER,
    detail     TEXT,
    checked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_domains_word ON domains(word);

CREATE TABLE IF NOT EXISTS mark_checks (
    word       TEXT NOT NULL,
    scope      TEXT NOT NULL,          -- e.g. "uspto:9,42" or "uspto:all"
    verdict    TEXT NOT NULL,          -- clear | watch | conflict | error
    highest_risk TEXT,
    n_hits     INTEGER,
    checked_at TEXT,
    PRIMARY KEY (word, scope)
);

CREATE TABLE IF NOT EXISTS mark_hits (
    word       TEXT NOT NULL,
    scope      TEXT NOT NULL,
    serial     TEXT NOT NULL,
    mark       TEXT,
    owner      TEXT,
    status_code TEXT,
    live       INTEGER,
    nice       TEXT,
    similarity REAL,
    risk       TEXT,
    goods      TEXT,
    url        TEXT,
    PRIMARY KEY (word, scope, serial)
);

CREATE TABLE IF NOT EXISTS ipgod_marks (
    mark_norm  TEXT NOT NULL,
    mark       TEXT,
    nice       TEXT,
    status     TEXT,
    owner      TEXT,
    source     TEXT
);
CREATE INDEX IF NOT EXISTS idx_ipgod_norm ON ipgod_marks(mark_norm);

CREATE TABLE IF NOT EXISTS repo_checks (
    word       TEXT NOT NULL,
    source     TEXT NOT NULL,          -- pypi | github
    verdict    TEXT NOT NULL,          -- clear | taken | watch | error
    n_hits     INTEGER,
    top        TEXT,
    checked_at TEXT,
    PRIMARY KEY (word, source)
);

CREATE TABLE IF NOT EXISTS final_tags (
    word        TEXT NOT NULL,
    model       TEXT NOT NULL,
    reason      TEXT,
    memorability INTEGER,
    fit         INTEGER,
    raw         TEXT,
    tagged_at   TEXT,
    PRIMARY KEY (word, model)
);
"""


# Columns added to `words` after the first release (name, SQL type).
WORD_EXTRA_COLUMNS = (
    ("engine", "TEXT"),    # coining engine that produced the word
    ("parents", "TEXT"),   # "base" or "base,partner" the word was derived from
)

# Columns added to `scores` after the first release (name, SQL type).
SCORE_EXTRA_COLUMNS = (
    ("why", "TEXT"),
    ("why_not", "TEXT"),
    ("syllables", "TEXT"),      # e.g. "met-a-prism"
    ("say_aloud", "TEXT"),      # plain respelling from the refine pass
    ("misheard_as", "TEXT"),    # sound-alike risk from the refine pass
    ("ambiguity", "REAL"),      # 0..1 chance a listener misspells / readers misread
    ("phonetic", "REAL"),       # 0..1 blended pronounceability
    ("refined", "INTEGER DEFAULT 0"),  # 1 once the refine pass has run
    ("verbable", "REAL"),       # 0..1 works as a verb: "I just ___ it and found what I needed"
)


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else config.DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    # Only the currently configured domain roles count; older cached rows (e.g. .app) are ignored.
    roles = ",".join("'" + r.replace("'", "") + "'" for r in config.active_domain_roles())
    conn.execute("DROP VIEW IF EXISTS active_domains")
    conn.execute(f"CREATE VIEW active_domains AS SELECT * FROM domains WHERE tld IN ({roles})")
    # Additive migrations for databases created before these columns existed
    for table, columns in (("scores", SCORE_EXTRA_COLUMNS), ("words", WORD_EXTRA_COLUMNS)):
        for col, decl in columns:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # column already present
    return conn


@contextmanager
def session(path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- meta ------------------------------------------------------------------
def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


# --- stage 1 ---------------------------------------------------------------
def upsert_words(conn: sqlite3.Connection, rows: Iterable[tuple[str, str, float]], ts: str) -> int:
    n = 0
    for word, pos, zipf in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO words(word, pos, zipf, source, created_at) VALUES(?,?,?,?,?)",
            (word, pos, zipf, "wordfreq", ts),
        )
        n += cur.rowcount
    return n


def count_words(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) c FROM words").fetchone()["c"]


def words_without_scores(
    conn: sqlite3.Connection, qversion: str, include_coined: bool = False
) -> list[str]:
    """Unscored words. Coined words are excluded unless ``include_coined`` is set."""
    coined_clause = "" if include_coined else "AND COALESCE(w.source, '') != 'coined'"
    rows = conn.execute(
        f"""
        SELECT w.word FROM words w
        LEFT JOIN scores s ON s.word = w.word AND s.qversion = ?
        WHERE s.word IS NULL {coined_clause}
        ORDER BY w.zipf DESC
        """,
        (qversion,),
    ).fetchall()
    return [r["word"] for r in rows]


def backup_database(conn: sqlite3.Connection, backup_dir: Path | None = None) -> Path:
    """Write a consistent snapshot of the database and return its path."""
    from datetime import datetime

    db_file = conn.execute("PRAGMA database_list").fetchone()["file"]
    target_dir = Path(backup_dir) if backup_dir else Path(db_file).parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"{Path(db_file).stem}-{stamp}.db"
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def clear_scores_and_coined(conn: sqlite3.Connection) -> tuple[int, int]:
    """Delete all scores and all coined words. Returns (scores, coined_words) removed."""
    n_scores = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    n_coined = conn.execute("SELECT COUNT(*) FROM words WHERE source = 'coined'").fetchone()[0]
    conn.execute("DELETE FROM scores")
    conn.execute("DELETE FROM words WHERE source = 'coined'")
    conn.commit()
    return n_scores, n_coined


# --- stage 2 ---------------------------------------------------------------
def upsert_score(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO scores
            (word, qversion, model, fit, natural, spellable, memorable, neg,
             taken, common, composite, why, why_not, raw, scored_at,
             syllables, say_aloud, misheard_as, ambiguity, phonetic, refined, verbable)
        VALUES (:word, :qversion, :model, :fit, :natural, :spellable, :memorable,
                :neg, :taken, :common, :composite, :why, :why_not, :raw, :scored_at,
                :syllables, :say_aloud, :misheard_as, :ambiguity, :phonetic, :refined, :verbable)
        """,
        {
            "why": None, "why_not": None, "syllables": None, "say_aloud": None,
            "misheard_as": None, "ambiguity": None, "phonetic": None, "refined": 0,
            "verbable": None,
            **row,
        },
    )


upsert_jev = upsert_score  # alias for backwards compatibility


def ranked_words(
    conn: sqlite3.Connection,
    qversion: str,
    limit: int | None = None,
    min_composite: float = 0.0,
    dict_only: bool = False,
) -> list[sqlite3.Row]:
    if dict_only:
        sql = """
            SELECT s.* FROM scores s
            JOIN words w ON w.word = s.word
            WHERE s.qversion = ? AND s.composite >= ? AND w.source != 'coined'
            ORDER BY s.composite DESC
        """
    else:
        sql = """
            SELECT * FROM scores
            WHERE qversion = ? AND composite >= ?
            ORDER BY composite DESC
        """
    params: list[Any] = [qversion, min_composite]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    if not rows and min_composite == 0.0:
        # Fallback when scoring has not run yet:
        # return words ordered by zipf frequency with dummy score values.
        filter_clause = "WHERE source != 'coined'" if dict_only else ""
        fallback_sql = f"""
            SELECT word, ? AS qversion, 'unscored' AS model,
                   0.0 AS fit, 0.0 AS natural, 0.0 AS spellable, 0.0 AS memorable,
                   0.0 AS neg, 0.0 AS taken, 0.0 AS common,
                   zipf AS composite, '{{}}' AS raw, created_at AS scored_at
            FROM words
            {filter_clause}
            ORDER BY zipf DESC
        """
        fallback_params: list[Any] = [qversion]
        if limit:
            fallback_sql += " LIMIT ?"
            fallback_params.append(limit)
        rows = conn.execute(fallback_sql, fallback_params).fetchall()
    return rows


# --- stage 3 ---------------------------------------------------------------
def upsert_domain(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO domains
            (domain, word, tld, status, http_status, detail, checked_at)
        VALUES (:domain, :word, :tld, :status, :http_status, :detail, :checked_at)
        """,
        row,
    )


def domain_map(conn: sqlite3.Connection, words: Sequence[str] | None = None) -> dict[str, dict[str, str]]:
    """Return {word: {tld_or_role: status}} for cached lookups."""
    if words:
        q = ",".join("?" * len(words))
        rows = conn.execute(f"SELECT * FROM active_domains WHERE word IN ({q})", list(words)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM active_domains").fetchall()
    out: dict[str, dict[str, str]] = {}
    for r in rows:
        out.setdefault(r["word"], {})[r["tld"]] = r["status"]
    return out


def words_passing_domains(conn: sqlite3.Connection, qversion: str, limit: int | None = None) -> list[sqlite3.Row]:
    """Words with at least one free option, ranked by composite (or zipf if unscored)."""
    has_scores = conn.execute(
        "SELECT 1 FROM scores WHERE qversion = ? LIMIT 1", (qversion,)
    ).fetchone() is not None
    if has_scores:
        sql = """
            SELECT s.*, MAX(CASE WHEN d.status = 'available' THEN 1 ELSE 0 END) AS any_free,
                   GROUP_CONCAT(d.tld || ':' || d.status) AS domain_state
            FROM scores s
            JOIN active_domains d ON d.word = s.word
            WHERE s.qversion = ?
            GROUP BY s.word
            HAVING any_free = 1
            ORDER BY s.composite DESC
        """
        rows = conn.execute(sql, (qversion,)).fetchall()
    else:
        sql = """
            SELECT w.word, w.zipf AS composite, 0.0 AS fit, 0.0 AS natural, 0.0 AS spellable, 0.0 AS memorable,
                   MAX(CASE WHEN d.status = 'available' THEN 1 ELSE 0 END) AS any_free,
                   GROUP_CONCAT(d.tld || ':' || d.status) AS domain_state
            FROM words w
            JOIN active_domains d ON d.word = w.word
            GROUP BY w.word
            HAVING any_free = 1
            ORDER BY w.zipf DESC
        """
        rows = conn.execute(sql).fetchall()
    return rows[:limit] if limit else rows


# --- stage 4 ---------------------------------------------------------------
def upsert_mark_check(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO mark_checks(word, scope, verdict, highest_risk, n_hits, checked_at)
        VALUES (:word, :scope, :verdict, :highest_risk, :n_hits, :checked_at)
        """,
        row,
    )


def replace_mark_hits(conn: sqlite3.Connection, word: str, scope: str, hits: Iterable[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM mark_hits WHERE word = ? AND scope = ?", (word, scope))
    for h in hits:
        conn.execute(
            """
            INSERT OR REPLACE INTO mark_hits
                (word, scope, serial, mark, owner, status_code, live, nice, similarity, risk, goods, url)
            VALUES (:word, :scope, :serial, :mark, :owner, :status_code, :live, :nice,
                    :similarity, :risk, :goods, :url)
            """,
            {"word": word, "scope": scope, **h},
        )


def upsert_repo_check(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO repo_checks(word, source, verdict, n_hits, top, checked_at)
        VALUES (:word, :source, :verdict, :n_hits, :top, :checked_at)
        """,
        row,
    )


def words_passing_trademarks(conn: sqlite3.Connection, qversion: str, limit: int | None = None) -> list[sqlite3.Row]:
    """Words whose Markbase + repo screens are all clear."""
    has_scores = conn.execute(
        "SELECT 1 FROM scores WHERE qversion = ? LIMIT 1", (qversion,)
    ).fetchone() is not None
    if has_scores:
        sql = """
            WITH t AS (
                SELECT word, MAX(CASE WHEN verdict = 'conflict' THEN 1 ELSE 0 END) AS conflict
                FROM mark_checks GROUP BY word
            ),
            r AS (
                SELECT word, MAX(CASE WHEN verdict = 'taken' THEN 1 ELSE 0 END) AS taken
                FROM repo_checks GROUP BY word
            ),
            d AS (
                SELECT word, MAX(CASE WHEN status = 'available' THEN 1 ELSE 0 END) AS free
                FROM active_domains GROUP BY word
            )
            SELECT s.word, s.composite, s.fit, s.natural, s.spellable, s.memorable, d.free
            FROM scores s
            JOIN d ON d.word = s.word AND d.free = 1
            LEFT JOIN t ON t.word = s.word
            LEFT JOIN r ON r.word = s.word
            WHERE s.qversion = ?
              AND COALESCE(t.conflict, 0) = 0
              AND COALESCE(r.taken, 0) = 0
            ORDER BY s.composite DESC
        """
        rows = conn.execute(sql, (qversion,)).fetchall()
    else:
        sql = """
            WITH t AS (
                SELECT word, MAX(CASE WHEN verdict = 'conflict' THEN 1 ELSE 0 END) AS conflict
                FROM mark_checks GROUP BY word
            ),
            r AS (
                SELECT word, MAX(CASE WHEN verdict = 'taken' THEN 1 ELSE 0 END) AS taken
                FROM repo_checks GROUP BY word
            ),
            d AS (
                SELECT word, MAX(CASE WHEN status = 'available' THEN 1 ELSE 0 END) AS free
                FROM active_domains GROUP BY word
            )
            SELECT w.word, w.zipf AS composite, 0.0 AS fit, 0.0 AS natural, 0.0 AS spellable, 0.0 AS memorable, d.free
            FROM words w
            JOIN d ON d.word = w.word AND d.free = 1
            LEFT JOIN t ON t.word = w.word
            LEFT JOIN r ON r.word = w.word
            WHERE COALESCE(t.conflict, 0) = 0
              AND COALESCE(r.taken, 0) = 0
            ORDER BY w.zipf DESC
        """
        rows = conn.execute(sql).fetchall()
    return rows[:limit] if limit else rows


def get_candidates_summary(
    conn: sqlite3.Connection,
    qversion: str,
    limit: int = 25,
    only_free_domains: bool = False,
    only_clean_trademarks: bool = False,
    bases_only: bool = False,
    all_domains_free: bool = False,
) -> list[dict[str, Any]]:
    """Return enriched rows for CLI inspection."""
    base_rows = ranked_words(conn, qversion, limit=None, dict_only=bases_only)
    out: list[dict[str, Any]] = []

    for r in base_rows:
        word = r["word"]
        # Domains check
        dom_rows = conn.execute(
            "SELECT tld, status FROM active_domains WHERE word = ? ORDER BY tld", (word,)
        ).fetchall()
        any_free = any(d["status"] == "available" for d in dom_rows)
        if only_free_domains and not any_free:
            continue
        if all_domains_free and not (dom_rows and all(d["status"] == "available" for d in dom_rows)):
            continue

        dom_summary = []
        for d in dom_rows:
            st = "FREE" if d["status"] == "available" else ("taken" if d["status"] == "registered" else "?")
            dom_summary.append(f"{d['tld']}={st}")
        domains_str = " ".join(dom_summary) if dom_summary else "not checked"

        # Trademarks check
        marks = conn.execute(
            "SELECT scope, verdict, highest_risk FROM mark_checks WHERE word = ?", (word,)
        ).fetchall()
        repos = conn.execute(
            "SELECT source, verdict FROM repo_checks WHERE word = ?", (word,)
        ).fetchall()

        has_conflict = any(m["verdict"] == "conflict" for m in marks) or any(rp["verdict"] == "taken" for rp in repos)
        has_checks = bool(marks or repos)

        if only_clean_trademarks and (not has_checks or has_conflict):
            continue

        if not has_checks:
            tm_str = "not checked"
        elif has_conflict:
            tm_str = "CONFLICT"
        elif any(m["verdict"] == "watch" for m in marks) or any(rp["verdict"] == "watch" for rp in repos):
            tm_str = "watch"
        else:
            tm_str = "CLEAR"

        why_str = r["why"] if ("why" in r.keys() and r["why"]) else None
        why_not_str = r["why_not"] if ("why_not" in r.keys() and r["why_not"]) else None

        if "raw" in r.keys() and r["raw"]:
            try:
                raw_data = json.loads(r["raw"])
                openr = raw_data.get("openrouter", {})
                if not why_str:
                    why_str = openr.get("why")
                if not why_not_str:
                    why_not_str = openr.get("why_not")
            except Exception:
                pass

        if not why_str:
            if r["composite"] >= 0.85:
                why_str = f"Top tier startup brand (fit: {r['fit']:.2f}, mem: {r['memorable']:.1f})"
            elif r["composite"] >= 0.70:
                why_str = f"Evocative candidate (fit: {r['fit']:.2f}, mem: {r['memorable']:.1f})"
            elif r["composite"] <= 0.45:
                why_str = f"Generic everyday term (fit: {r['fit']:.2f})"
            else:
                why_str = f"Plausible candidate (fit: {r['fit']:.2f})"

        out.append({
            "word": word,
            "composite": r["composite"],
            "fit": r["fit"],
            "natural": r["natural"],
            "spellable": r["spellable"],
            "memorable": r["memorable"],
            "domains": domains_str,
            "any_free": any_free,
            "trademark": tm_str,
            "why": why_str,
            "why_not": why_not_str,
            "syllables": r["syllables"] if "syllables" in r.keys() else None,
            "say_aloud": r["say_aloud"] if "say_aloud" in r.keys() else None,
            "misheard_as": r["misheard_as"] if "misheard_as" in r.keys() else None,
            "phonetic": r["phonetic"] if "phonetic" in r.keys() else None,
            "refined": bool(r["refined"]) if "refined" in r.keys() else False,
            "verbable": r["verbable"] if "verbable" in r.keys() else None,
        })
        if len(out) >= limit:
            break

    return out


def upsert_final_tag(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO final_tags(word, model, reason, memorability, fit, raw, tagged_at)
        VALUES (:word, :model, :reason, :memorability, :fit, :raw, :tagged_at)
        """,
        row,
    )


def dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
