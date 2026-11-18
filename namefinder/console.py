"""Pro-grade terminal UI styling, rich table formatting, and in-place refreshing live dashboards."""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from typing import Any, Sequence

import shutil
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table


def get_terminal_width() -> int:
    cols = shutil.get_terminal_size(fallback=(120, 30)).columns
    return max(cols, 110)


# Global console instance (auto-detects terminal width, clamped to >= 110)
_console = Console(width=get_terminal_width())

# Enable VT100 / ANSI escape sequences on Windows consoles
if sys.platform == "win32":
    os.system("")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def ansi_len(s: str) -> int:
    """Return visible length of string excluding ANSI escape codes."""
    return len(ANSI_RE.sub("", str(s)))


def ansi_ljust(s: str, width: int) -> str:
    """Pad string to visible width taking ANSI codes into account."""
    vlen = ansi_len(s)
    return str(s) + (" " * max(0, width - vlen))

# ANSI Color Codes
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[90m"

RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
WHITE = "\033[37m"

B_RED = "\033[1;31m"
B_GREEN = "\033[1;32m"
B_YELLOW = "\033[1;33m"
B_CYAN = "\033[1;36m"
B_WHITE = "\033[1;37m"
B_MAGENTA = "\033[1;35m"


def bold(text: Any) -> str:
    return f"{BOLD}{text}{RESET}"


def dim(text: Any) -> str:
    return f"{DIM}{text}{RESET}"


def green(text: Any) -> str:
    return f"{GREEN}{text}{RESET}"


def b_green(text: Any) -> str:
    return f"{B_GREEN}{text}{RESET}"


def yellow(text: Any) -> str:
    return f"{YELLOW}{text}{RESET}"


def red(text: Any) -> str:
    return f"{RED}{text}{RESET}"


def cyan(text: Any) -> str:
    return f"{CYAN}{text}{RESET}"


def b_cyan(text: Any) -> str:
    return f"{B_CYAN}{text}{RESET}"


def magenta(text: Any) -> str:
    return f"{MAGENTA}{text}{RESET}"


def b_magenta(text: Any) -> str:
    return f"{B_MAGENTA}{text}{RESET}"


def white(text: Any) -> str:
    return f"{WHITE}{text}{RESET}"


def b_white(text: Any) -> str:
    return f"{B_WHITE}{text}{RESET}"


def badge_score(score: float) -> str:
    """Format composite score with rich markup hierarchy."""
    s = f"{score:.3f}"
    if score >= 0.85:
        return f"[bold green]{s}[/]"
    if score >= 0.70:
        return f"[bold cyan]{s}[/]"
    if score >= 0.50:
        return f"[yellow]{s}[/]"
    return f"[dim]{s}[/]"


def format_domain_summary(domain_state: str | None) -> str:
    """Format domain tokens like 'com:available,dev:registered' or 'app=FREE com=taken' into rich badges."""
    if not domain_state or domain_state == "not checked":
        return "[dim]not checked[/]"

    parts = []
    cleaned = domain_state.replace(",", " ")
    tokens = cleaned.split()
    for t in sorted(tokens):
        sep = ":" if ":" in t else ("=" if "=" in t else None)
        if not sep:
            continue
        tld, status = t.split(sep, 1)
        is_free = status.lower() in ("available", "free")
        if is_free:
            parts.append(f"[bold white]{tld}[/] [bold green]FREE[/]")
        else:
            parts.append(f"[dim]{tld}=taken[/]")
    return "  ".join(parts) if parts else "[dim]not checked[/]"


def badge_trademark(status: str | None) -> str:
    """Format trademark verdict into rich markup badge."""
    if not status or status == "not checked":
        return "[dim]not checked[/]"
    st = status.lower()
    if "clear" in st:
        return "[bold green]CLEAR[/]"
    if "conflict" in st or "taken" in st:
        return "[bold red]CONFLICT[/]"
    return f"[bold yellow]{status}[/]"


def build_rich_table(
    rows: Sequence[sqlite3.Row | dict[str, Any]],
    title: str = "Top Candidates",
    limit: int = 10,
    show_domains: bool = True,
    show_reasoning: bool = True,
) -> Table:
    """Construct an auto-expanding rich Table that fills the full terminal width."""
    items = list(rows)[:limit]
    t = Table(
        title=f"[bold cyan]{title}[/]",
        box=box.ROUNDED,
        expand=True,
        header_style="bold cyan",
        show_header=True,
        border_style="dim",
    )

    t.add_column("#", justify="right", style="bold cyan", no_wrap=True, min_width=3)
    t.add_column("Candidate", style="bold white", no_wrap=True, min_width=14)
    t.add_column("Score", justify="center", no_wrap=True, min_width=7)
    t.add_column("Fit", justify="center", no_wrap=True, min_width=5)
    t.add_column("Nat", justify="center", no_wrap=True, min_width=5)

    if show_domains:
        t.add_column("Domains", no_wrap=True, min_width=25)
    if show_reasoning:
        t.add_column("Reasoning & Brand Fit", style="italic", min_width=20)

    for i, item in enumerate(items, 1):
        r = dict(item) if hasattr(item, "keys") else item
        word = r["word"]
        comp = r.get("composite", 0.0) or 0.0
        fit = r.get("fit", 0.0) or 0.0
        nat = r.get("natural", 0.0) or 0.0

        score_text = badge_score(comp)
        fit_text = f"{fit:.2f}"
        nat_text = f"{nat:.2f}"

        sound = r.get("say_aloud") or r.get("syllables")
        cand_cell = f"[bold white]{word}[/]"
        if sound and sound != word:
            cand_cell += f"\n[dim]{sound}[/]"
        row_cells = [
            str(i),
            cand_cell,
            score_text,
            fit_text,
            nat_text,
        ]

        if show_domains:
            dom_text = format_domain_summary(r.get("domains") or r.get("domain_state"))
            row_cells.append(dom_text)

        if show_reasoning:
            why = r.get("why") or ""
            why_not = r.get("why_not") or ""
            misheard = r.get("misheard_as")
            if misheard:
                why_not = f"{why_not}; may sound like: {misheard}" if why_not else f"may sound like: {misheard}"
            if why and why_not:
                reason = f"{why} [dim][Risk: {why_not}][/]"
            elif why:
                reason = why
            elif why_not:
                reason = f"[dim][Risk: {why_not}][/]"
            else:
                reason = "[dim]-[/]"
            row_cells.append(reason)

        t.add_row(*row_cells)

    return t


def print_leaderboard(
    rows: Sequence[sqlite3.Row | dict[str, Any]],
    title: str = "Top Candidates",
    limit: int = 10,
    show_domains: bool = True,
    show_reasoning: bool = True,
) -> None:
    """Print an auto-expanding leaderboard table."""
    if not rows:
        return
    t = build_rich_table(
        rows,
        title=title,
        limit=limit,
        show_domains=show_domains,
        show_reasoning=show_reasoning,
    )
    _console.print(t)


def show_live_top_10(
    conn: sqlite3.Connection,
    qversion: str,
    title: str = "Live Leaderboard: Top 10 Scored Candidates",
    limit: int = 10,
    free_domains_only: bool = False,
) -> None:
    """Fetch and display the live top 10 candidates directly from SQLite."""
    if free_domains_only:
        sql = """
            SELECT s.word, s.composite, s.fit, s.natural, s.why, s.why_not, s.syllables, s.say_aloud, s.misheard_as,
                   GROUP_CONCAT(d.tld || ':' || d.status) AS domain_state
            FROM scores s
            JOIN domains d ON d.word = s.word
            WHERE s.qversion = ?
            GROUP BY s.word
            HAVING MAX(CASE WHEN d.status = 'available' THEN 1 ELSE 0 END) = 1
            ORDER BY s.composite DESC
            LIMIT ?
        """
        rows = conn.execute(sql, (qversion, limit)).fetchall()
    else:
        sql = """
            SELECT s.word, s.composite, s.fit, s.natural, s.why, s.why_not, s.syllables, s.say_aloud, s.misheard_as,
                   (SELECT GROUP_CONCAT(d.tld || ':' || d.status) FROM domains d WHERE d.word = s.word) AS domain_state
            FROM scores s
            WHERE s.qversion = ?
            ORDER BY s.composite DESC
            LIMIT ?
        """
        rows = conn.execute(sql, (qversion, limit)).fetchall()

    if rows:
        print_leaderboard(rows, title=title, limit=limit)


class LiveScoringDashboard:
    """A pro-grade, in-place refreshing live dashboard displaying progress and top 10 leaderboard."""

    def __init__(
        self,
        description: str,
        total: int,
        qversion: str,
        db_path=None,
        free_domains_only: bool = False,
        leaderboard_title: str = "Live Leaderboard: Top 10 Scored Candidates",
    ):
        self.total = total
        self.qversion = qversion
        self.db_path = db_path
        self.free_domains_only = free_domains_only
        self.leaderboard_title = leaderboard_title
        self.console = Console(width=get_terminal_width())
        self.progress = Progress(
            SpinnerColumn(style="bold cyan"),
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(bar_width=None),
            TaskProgressColumn(),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            expand=True,
        )
        self.task_id = self.progress.add_task(description, total=total)
        self.live = Live(self._render_view(), console=self.console, refresh_per_second=4)

    def _fetch_rows(self, limit: int = 10):
        from . import db
        with db.session(self.db_path) as conn:
            if self.free_domains_only:
                sql = """
                    SELECT s.word, s.composite, s.fit, s.natural, s.why, s.why_not, s.syllables, s.say_aloud, s.misheard_as,
                           GROUP_CONCAT(d.tld || ':' || d.status) AS domain_state
                    FROM scores s
                    JOIN domains d ON d.word = s.word
                    WHERE s.qversion = ?
                    GROUP BY s.word
                    HAVING MAX(CASE WHEN d.status = 'available' THEN 1 ELSE 0 END) = 1
                    ORDER BY s.composite DESC
                    LIMIT ?
                """
                return conn.execute(sql, (self.qversion, limit)).fetchall()
            else:
                sql = """
                    SELECT s.word, s.composite, s.fit, s.natural, s.why, s.why_not, s.syllables, s.say_aloud, s.misheard_as,
                           (SELECT GROUP_CONCAT(d.tld || ':' || d.status) FROM domains d WHERE d.word = s.word) AS domain_state
                    FROM scores s
                    WHERE s.qversion = ?
                    ORDER BY s.composite DESC
                    LIMIT ?
                """
                return conn.execute(sql, (self.qversion, limit)).fetchall()

    def _render_view(self) -> Group:
        rows = self._fetch_rows(limit=10)
        if rows:
            table = build_rich_table(rows, title=self.leaderboard_title, limit=10)
            return Group(self.progress, table)
        return Group(self.progress)

    def update(self, advance: int = 0):
        if advance > 0:
            self.progress.advance(self.task_id, advance)
        self.live.update(self._render_view())

    def __enter__(self):
        self.live.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self.live.__exit__(exc_type, exc_val, exc_tb)
