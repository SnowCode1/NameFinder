"""Pro-grade terminal UI styling, rich table formatting, and in-place refreshing live dashboards."""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from typing import Any, Collection, Sequence

import shutil
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text


def _force_utf8_streams() -> None:
    """Windows consoles and pipes default to cp1252, which cannot encode glyphs such as the refined check mark."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_force_utf8_streams()


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


def _tld_cells(domain_state: str | None) -> str:
    """Compact per-TLD status: green = free, red = taken, dim = unknown. One short token per TLD."""
    if not domain_state or domain_state == "not checked":
        return "[dim]?[/]"
    parts = []
    for t in sorted(domain_state.replace(",", " ").split()):
        sep = ":" if ":" in t else ("=" if "=" in t else None)
        if not sep:
            continue
        tld, status = t.split(sep, 1)
        s = status.lower()
        if s in ("available", "free"):
            parts.append(f"[bold green]{tld}[/]")
        elif s in ("registered", "taken"):
            parts.append(f"[red]{tld}[/]")
        else:
            parts.append(f"[dim]{tld}?[/]")
    return " ".join(parts) or "[dim]?[/]"


def _tm_cell(status: str | None) -> str:
    """One-glyph trademark verdict."""
    st = (status or "").lower()
    if not st or st == "not checked":
        return "[dim]·[/]"
    if "clear" in st:
        return "[green]✓[/]"
    if "conflict" in st or "taken" in st:
        return "[bold red]✗[/]"
    return "[yellow]![/]"


def _reason_text(r: dict[str, Any], detail: bool) -> str:
    """The judge's reason. One short line by default; with `detail`, the risks and mishearing too."""
    why = r.get("why") or ""
    if not detail:
        return why or "[dim]-[/]"
    why_not = r.get("why_not") or ""
    misheard = r.get("misheard_as")
    if misheard:
        why_not = f"{why_not}; may sound like: {misheard}" if why_not else f"may sound like: {misheard}"
    if why and why_not:
        return f"{why} [dim][Risk: {why_not}][/]"
    return why or (f"[dim][Risk: {why_not}][/]" if why_not else "[dim]-[/]")


def _add_rank_name_columns(t: Table) -> None:
    """The leading `#` and `Name` columns shared by every ranking table."""
    t.add_column("#", justify="right", style="bold cyan", no_wrap=True, min_width=3)
    t.add_column("Name", no_wrap=True, min_width=14)


def build_rich_table(
    rows: Sequence[sqlite3.Row | dict[str, Any]],
    title: str = "Top Candidates",
    limit: int = 10,
    show_domains: bool = True,
    show_reasoning: bool = True,
    detail: bool = False,
    show_say: bool = False,
) -> Table:
    """Compact leaderboard: one line per row, the reason column takes the leftover width.

    Narrow terminals drop the sub-scores (under 120 columns) and the reason (under 90).
    `detail` wraps the full reason with risks; `show_say` adds the pronunciation next to the name.
    """
    items = list(rows)[:limit]
    width = _console.width
    show_elo = any(dict(i).get("elo") is not None for i in items)
    show_parts = width >= 120
    show_reasoning = show_reasoning and width >= 90
    show_tm = any(dict(i).get("trademark") not in (None, "not checked") for i in items)
    t = Table(
        title=f"[bold cyan]{title}[/]",
        box=box.ROUNDED,
        expand=True,
        header_style="bold cyan",
        show_header=True,
        border_style="dim",
    )

    _add_rank_name_columns(t)
    t.add_column("Score", justify="right", no_wrap=True, min_width=5)
    if show_elo:
        t.add_column("Elo", justify="right", no_wrap=True, min_width=9)
    if show_parts:
        t.add_column("Fit·Nat·Verb", justify="center", no_wrap=True, min_width=12)
    if show_domains:
        t.add_column("Domains", no_wrap=True, min_width=7)
    if show_tm:
        t.add_column("TM", justify="center", no_wrap=True, min_width=2)
    if show_reasoning:
        if detail:
            t.add_column("Reasoning & Brand Fit", style="italic", ratio=1, min_width=20)
        else:
            t.add_column("Why", style="italic", ratio=1, min_width=20, no_wrap=True, overflow="ellipsis")

    def part(v: float | None) -> str:
        return f"{v:.2f}".lstrip("0") if v is not None else "-"

    for i, item in enumerate(items, 1):
        r = dict(item) if hasattr(item, "keys") else item
        word = r["word"]
        name = f"[bold white]{word}[/]"
        if r.get("refined"):
            name += " [bold green]✓[/]"
        fresh = r.get("fresh")
        if fresh == "new":
            name += " [black on yellow]N[/]"
        elif fresh == "refined":
            name += " [black on cyan]R[/]"
        sound = r.get("say_aloud") or r.get("syllables")
        if show_say and sound and sound != word:
            name += f" [dim]{sound}[/]"

        cells = [str(i), name, badge_score(r.get("composite", 0.0) or 0.0)]
        if show_elo:
            elo = r.get("elo")
            cells.append(f"[bold cyan]{elo:.0f}[/] [dim]#{r['elo_rank']}[/]" if elo is not None else "[dim]-[/]")
        if show_parts:
            cells.append(f"{part(r.get('fit'))}  {part(r.get('natural'))}  {part(r.get('verbable'))}")
        if show_domains:
            cells.append(_tld_cells(r.get("domains") or r.get("domain_state")))
        if show_tm:
            cells.append(_tm_cell(r.get("trademark")))
        if show_reasoning:
            cells.append(_reason_text(r, detail))
        t.add_row(*cells)

    return t


def print_leaderboard(
    rows: Sequence[sqlite3.Row | dict[str, Any]],
    title: str = "Top Candidates",
    limit: int = 10,
    show_domains: bool = True,
    show_reasoning: bool = True,
    detail: bool = False,
    show_say: bool = False,
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
        detail=detail,
        show_say=show_say,
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
            SELECT s.word, s.composite, s.fit, s.natural, s.why, s.why_not, s.syllables, s.say_aloud, s.misheard_as, s.verbable, s.refined,
                   GROUP_CONCAT(d.tld || ':' || d.status) AS domain_state
            FROM scores s
            JOIN active_domains d ON d.word = s.word
            WHERE s.qversion = ?
            GROUP BY s.word
            HAVING MAX(CASE WHEN d.status = 'available' THEN 1 ELSE 0 END) = 1
            ORDER BY s.composite DESC
            LIMIT ?
        """
        rows = conn.execute(sql, (qversion, limit)).fetchall()
    else:
        sql = """
            SELECT s.word, s.composite, s.fit, s.natural, s.why, s.why_not, s.syllables, s.say_aloud, s.misheard_as, s.verbable, s.refined,
                   (SELECT GROUP_CONCAT(d.tld || ':' || d.status) FROM active_domains d WHERE d.word = s.word) AS domain_state
            FROM scores s
            WHERE s.qversion = ?
            ORDER BY s.composite DESC
            LIMIT ?
        """
        rows = conn.execute(sql, (qversion, limit)).fetchall()

    if rows:
        print_leaderboard(rows, title=title, limit=limit)


def print_score_grid(
    items: Sequence[tuple[str, float, str | None]],
    title: str = "",
    checked: Collection[str] = (),
) -> None:
    """Scored names grouped by engine: a header per engine, then its names best first, reading down columns.

    `items` are (word, composite, engine). Names in `checked` got a domain check and are underlined.
    Engines are ordered by their best name, so the group holding the cycle's winner comes first.
    """
    if not items:
        return
    groups: dict[str, list[tuple[str, float]]] = {}
    for word, comp, engine in items:
        groups.setdefault(engine or "other", []).append((word, comp or 0.0))
    for g in groups.values():
        g.sort(key=lambda t: -t[1])
    order = sorted(groups, key=lambda e: -groups[e][0][1])

    wlen = max(len(w) for w, _, _ in items)
    cell = wlen + 6                      # word + space + score
    gap = 3
    cols = max(1, min(5, (_console.width + gap) // (cell + gap)))

    _console.print(f"[bold cyan]{title}[/]  [dim]underlined = domain-checked[/]" if title
                   else "[dim]underlined = domain-checked[/]")
    for engine in order:
        names = groups[engine]
        mean = sum(c for _, c in names) / len(names)
        head = Text()
        head.append(f" {engine} ", style="bold white on magenta" if engine == "llm" else "bold white on blue")
        head.append(f"  {len(names)} name{'' if len(names) == 1 else 's'}, best {names[0][1]:.3f}, mean {mean:.3f}", style="dim")
        _console.print(head)
        rows = -(-len(names) // cols)
        for r in range(rows):
            line = Text("  ")
            for c in range(cols):
                idx = c * rows + r
                if idx >= len(names):
                    break
                word, comp = names[idx]
                color = ("bold green" if comp >= 0.85 else "bold cyan" if comp >= 0.70
                         else "yellow" if comp >= 0.50 else "dim")
                if c:
                    line.append(" " * gap)
                line.append(word, style="bold white underline" if word in checked else "white")
                line.append(" " * (wlen - len(word)))
                line.append(f" {comp:.3f}", style=color)
            _console.print(line)


def print_base_table(stats: Sequence[Any], threshold: float, title: str = "") -> None:
    """Base-word value table: rows are bases.BaseStat, already sorted and trimmed."""
    table = Table(title=title or None, box=box.SIMPLE_HEAVY, title_style="bold cyan", header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("base", style="bold white")
    table.add_column("names", justify="right")
    table.add_column(f"hits ≥{threshold:.3f}", justify="right")
    table.add_column("tail rate", justify="right")
    table.add_column("recent", justify="right")
    table.add_column("best", justify="right")
    table.add_column("best name", style="white")
    for i, s in enumerate(stats, 1):
        rate = s.rate
        rate_style = "bold green" if rate >= 0.15 else "cyan" if rate >= 0.08 else "dim"
        # recent well below all-time = the base is running dry
        trend = "red" if s.recent_rate < 0.6 * rate else "green" if s.recent_rate > 1.3 * rate else "white"
        table.add_row(
            str(i), s.word, str(s.n), str(s.hits),
            Text(f"{rate:.3f}", style=rate_style),
            Text(f"{s.recent_rate:.3f}", style=trend),
            f"{s.best:.3f}", s.best_word,
        )
    _console.print(table)


def print_engine_table(rows: Sequence[Any], threshold: float, title: str = "", label: str = "engine") -> None:
    """Engine report: rows are (engine, names, mean, tail_rate, best_word, best)."""
    table = Table(title=title or None, box=box.SIMPLE_HEAVY, title_style="bold cyan", header_style="bold")
    table.add_column(label, style="bold white")
    table.add_column("names", justify="right")
    table.add_column("mean", justify="right")
    table.add_column(f"tail rate (≥{threshold:.3f})", justify="right")
    table.add_column("best", justify="right")
    table.add_column("best name", style="white")
    for eng, n, mean, rate, best_word, best in rows:
        style = "bold green" if rate >= 0.06 else "cyan" if rate >= 0.03 else "dim"
        table.add_row(eng, str(n), f"{mean:.3f}", Text(f"{rate:.1%}", style=style), f"{best:.3f}", best_word)
    _console.print(table)


def print_lift_table(rows: Sequence[Any], base_rate: float, feature: str, title: str = "") -> None:
    """Feature lift: rows are (value, names, tail_rate, lift), best lift first."""
    table = Table(title=title or None, box=box.SIMPLE_HEAVY, title_style="bold cyan", header_style="bold")
    table.add_column(feature, style="bold white")
    table.add_column("names", justify="right")
    table.add_column("tail rate", justify="right")
    table.add_column(f"lift (base {base_rate:.1%})", justify="right")
    for value, n, rate, lift in rows:
        style = "bold green" if lift >= 1.5 else "red" if lift <= 0.5 else "white"
        table.add_row(str(value), str(n), f"{rate:.1%}", Text(f"{lift:.2f}x", style=style))
    _console.print(table)


class CostColumn(ProgressColumn):
    """Live spend / limit and token counts, re-rendered on every refresh."""

    def render(self, task) -> Text:
        from . import budget
        text = budget.metrics_text()
        style = "bold red" if budget.exhausted() else "green"
        return Text(text, style=style, no_wrap=True, overflow="ellipsis")


class RichBar:
    """A one-task rich progress bar with the small tqdm surface the pipeline uses.

    `update(n)` advances, `set_postfix_str` is accepted and ignored (the cost column
    re-renders spend itself), and `note(text)` sets a live status after the bar.
    Prints made while it is open appear above the bar.
    """

    def __init__(self, description: str, total: int, unit: str = "word", style: str = "cyan"):
        self.progress = Progress(
            SpinnerColumn(style=f"bold {style}"),
            TextColumn(f"[bold {style}]{{task.description}}"),
            BarColumn(bar_width=32, complete_style=style, finished_style=f"bold {style}"),
            MofNCompleteColumn(),
            TextColumn(f"[dim]{unit}s[/]"),
            TimeRemainingColumn(),
            TextColumn("{task.fields[note]}"),
            CostColumn(),
            console=_console,
        )
        self.task_id = self.progress.add_task(description, total=total, note="")

    def update(self, n: int = 1) -> None:
        self.progress.advance(self.task_id, n)

    def set_postfix_str(self, *_args, **_kwargs) -> None:
        pass

    def note(self, text: str) -> None:
        self.progress.update(self.task_id, note=text)

    def __enter__(self) -> "RichBar":
        self.progress.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.progress.stop()


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
            CostColumn(),
            expand=True,
        )
        self.task_id = self.progress.add_task(description, total=total)
        self.live = Live(self._render_view(), console=self.console, refresh_per_second=4)

    def _fetch_rows(self, limit: int = 10):
        from . import db
        with db.session(self.db_path) as conn:
            if self.free_domains_only:
                sql = """
                    SELECT s.word, s.composite, s.fit, s.natural, s.why, s.why_not, s.syllables, s.say_aloud, s.misheard_as, s.verbable, s.refined,
                           GROUP_CONCAT(d.tld || ':' || d.status) AS domain_state
                    FROM scores s
                    JOIN active_domains d ON d.word = s.word
                    WHERE s.qversion = ?
                    GROUP BY s.word
                    HAVING MAX(CASE WHEN d.status = 'available' THEN 1 ELSE 0 END) = 1
                    ORDER BY s.composite DESC
                    LIMIT ?
                """
                return conn.execute(sql, (self.qversion, limit)).fetchall()
            else:
                sql = """
                    SELECT s.word, s.composite, s.fit, s.natural, s.why, s.why_not, s.syllables, s.say_aloud, s.misheard_as, s.verbable, s.refined,
                           (SELECT GROUP_CONCAT(d.tld || ':' || d.status) FROM active_domains d WHERE d.word = s.word) AS domain_state
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


# --- tournament ------------------------------------------------------------
def log(text: str, markup: bool = False) -> None:
    """Print a line through the shared console, so it lands above any live bar or dashboard."""
    _console.print(text, markup=markup, highlight=False)


def _rating_bar(value: float, lo: float, hi: float, width: int = 8, style: str = "cyan") -> Text:
    span = max(hi - lo, 1e-9)
    filled = max(0, min(width, round((value - lo) / span * width)))
    return Text("█" * filled, style=style) + Text("░" * (width - filled), style="grey30")


def _move_cell(move: int | None) -> str:
    if move is None:
        return "[dim]new[/]"
    if move > 0:
        return f"[bold green]▲{move}[/]"
    if move < 0:
        return f"[bold red]▼{-move}[/]"
    return "[dim]·[/]"


def _delta_cell(delta: float | None) -> str:
    if delta is None or abs(delta) < 0.5:
        return "[dim]·[/]"
    return f"[green]+{delta:.0f}[/]" if delta > 0 else f"[red]{delta:.0f}[/]"


def build_tournament_table(
    rows: Sequence[dict[str, Any]],
    title: str = "Tournament ranking",
    limit: int = 30,
    bench: str | None = None,
    bench_rating: float | None = None,
    tld: str = "com",
    caption: str | None = None,
) -> Table:
    """Rating table. Each row: word, rating, games, rank, move, delta, domain, old, note, is_ref."""
    items = list(rows)[:limit]
    t = Table(
        title=f"[bold cyan]{title}[/]",
        caption=caption,
        box=box.ROUNDED,
        expand=True,
        header_style="bold cyan",
        border_style="dim",
        caption_style="dim",
    )
    _add_rank_name_columns(t)
    show_origin = any(r.get("origin") not in (None, "old") for r in items)
    if show_origin:
        t.add_column("Src", no_wrap=True, min_width=5)
    t.add_column("Rating", no_wrap=True, min_width=14)
    show_moves = any(r.get("move") is not None or r.get("delta") is not None for r in items)
    if show_moves:
        t.add_column("Move", justify="center", no_wrap=True, min_width=4)
        t.add_column("Δ rnd", justify="right", no_wrap=True, min_width=5)
    if bench and bench_rating is not None:
        t.add_column(f"vs {bench}", justify="right", no_wrap=True, min_width=8)
    t.add_column("Games", justify="right", no_wrap=True, min_width=5)
    t.add_column(f".{tld}", no_wrap=True, min_width=5)
    t.add_column("Old", justify="center", no_wrap=True, min_width=5)
    t.add_column("Biggest flaw (judge)", style="italic", ratio=1, min_width=20)

    # Fixed floor/ceiling around the 1500 start so a small spread does not look dramatic.
    ratings = [r["rating"] for r in items] or [1500.0]
    lo, hi = min(ratings + [1400.0]), max(ratings + [1600.0])
    for r in items:
        word = r["word"]
        name = f"[bold magenta]{word}[/] [dim magenta]ref[/]" if r.get("is_ref") else f"[bold white]{word}[/]"
        rating = Text(f"{r['rating']:>5.0f} ", style="bold white") + _rating_bar(
            r["rating"], lo, hi, style="magenta" if r.get("is_ref") else "cyan")
        cells = [str(r["rank"]), name]
        if show_origin:
            style = {"idea": "green", "variant": "yellow", "both": "cyan", "ref": "magenta",
                     "elite": "blue", "idea2": "bold green"}.get(r.get("origin"), "dim")
            cells.append(f"[{style}]{r.get('origin') or 'old'}[/]")
        cells.append(rating)
        if show_moves:
            cells += [_move_cell(r.get("move")), _delta_cell(r.get("delta"))]
        if bench and bench_rating is not None:
            if word == bench:
                cells.append("[dim]benchmark[/]")
            else:
                d = r["rating"] - bench_rating
                cells.append(f"[bold green]+{d:.0f}[/]" if d > 0 else f"[red]{d:.0f}[/]")
        dom = r.get("domain")
        dom_cell = "[bold green]FREE[/]" if dom == "available" else ("[dim]taken[/]" if dom else "[dim]?[/]")
        old = r.get("old")
        cells += [str(r["games"]), dom_cell, badge_score(old) if old is not None else "[dim]-[/]",
                  r.get("note") or "[dim]-[/]"]
        t.add_row(*cells)
    return t


class TournamentDashboard:
    """Live view for `tournament`: header, round progress with spend, and the rating table.

    Quacks like the progress bar `play_round` expects (`update`, `set_postfix_str`), so the
    judge loop does not know it is drawing a dashboard.
    """

    def __init__(self, header: Text, total_rounds: int, table_rows: int = 15):
        from rich.panel import Panel
        self._Panel = Panel
        self.header = header
        self.total_rounds = total_rounds
        self.table_rows = max(6, table_rows)
        self.facts: dict[str, str] = {}
        self.table: Table | None = None
        self.progress = Progress(
            SpinnerColumn(style="bold cyan"),
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(bar_width=None, complete_style="cyan", finished_style="bold cyan"),
            MofNCompleteColumn(),
            TextColumn("[dim]calls[/]"),
            TimeRemainingColumn(),
            CostColumn(),
            expand=True,
        )
        self.task_id = self.progress.add_task("Round", total=1)
        self.live = Live(self._view(), console=_console, refresh_per_second=6, transient=False)

    def _view(self) -> Group:
        facts = Text()
        for i, (k, v) in enumerate(self.facts.items()):
            facts.append(("   " if i else "") + f"{k} ", style="dim")
            facts.append(v, style="bold white")
        parts: list[Any] = [self._Panel(Group(self.header, facts) if self.facts else self.header,
                                        border_style="cyan", title="[bold cyan]Tournament[/]"),
                            self.progress]
        if self.table is not None:
            parts.append(self.table)
        return Group(*parts)

    def refresh(self) -> None:
        self.live.update(self._view())

    def begin_round(self, round_no: int, index: int, calls: int) -> None:
        self.progress.reset(self.task_id, total=calls, completed=0,
                            description=f"Round {round_no} ({index}/{self.total_rounds})")
        self.refresh()

    def set_table(self, table: Table) -> None:
        self.table = table
        self.refresh()

    def set_facts(self, **facts: str) -> None:
        self.facts = {k: str(v) for k, v in facts.items()}
        self.refresh()

    def update(self, n: int = 1) -> None:
        self.progress.advance(self.task_id, n)

    def set_postfix_str(self, *_args, **_kwargs) -> None:
        pass

    def __enter__(self) -> "TournamentDashboard":
        self.live.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self.live.__exit__(exc_type, exc_val, exc_tb)


ORIGIN_ORDER = ("ref", "elite", "idea2", "idea", "variant", "both", "bulk", "old")
ORIGIN_NOTES = {
    "ref": "your reference names", "elite": "old-pool names already selected by an earlier tournament",
    "idea": "names from ideas (first prompt: varied roots)",
    "idea2": "names from ideas (retuned: transparent roots)", "variant": "variants of the best names",
    "both": "found by the idea process and already in the old pool", "bulk": "old-pool names added unselected (control)",
    "old": "other old-pool names",
}


def build_origin_table(rows: Sequence[dict[str, Any]], tops: Sequence[int] = (25, 50, 100)) -> Table:
    """One row per source: how many names, how they rate, and how many reach the top N."""
    import statistics
    by: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(r.get("origin") or "old", []).append(r)
    t = Table(title="[bold cyan]Rating by source[/]", box=box.ROUNDED, header_style="bold cyan", border_style="dim",
              caption="Mean/median are over all names of that source; Top N counts how many of them are in the overall top N.",
              caption_style="dim")
    t.add_column("Source", no_wrap=True)
    t.add_column("Names", justify="right")
    t.add_column("Mean", justify="right")
    t.add_column("Median", justify="right")
    t.add_column("Best (rank)", justify="right")
    for n in tops:
        t.add_column(f"Top {n}", justify="right")
    t.add_column("What it is", style="dim")
    for origin in (o for o in ORIGIN_ORDER if o in by):
        grp = by[origin]
        ratings = [g["rating"] for g in grp]
        best = min(grp, key=lambda g: g["rank"])
        cells = [f"[bold]{origin}[/]", str(len(grp)), f"{statistics.fmean(ratings):.0f}",
                 f"{statistics.median(ratings):.0f}", f"{max(ratings):.0f} (#{best['rank']})"]
        for n in tops:
            k = sum(1 for g in grp if g["rank"] <= n)
            cells.append(f"{k}" if k else "[dim]0[/]")
        cells.append(ORIGIN_NOTES.get(origin, ""))
        t.add_row(*cells)
    return t
