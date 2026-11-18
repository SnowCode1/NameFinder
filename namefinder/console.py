"""Pro-grade terminal UI styling, ANSI color badges, and box-drawing leaderboards."""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from typing import Any, Sequence

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
    """Format composite score with color hierarchy."""
    s = f"{score:.3f}"
    if score >= 0.85:
        return f"{B_GREEN}{s}{RESET}"
    if score >= 0.70:
        return f"{B_CYAN}{s}{RESET}"
    if score >= 0.50:
        return f"{YELLOW}{s}{RESET}"
    return f"{DIM}{s}{RESET}"


def format_domain_summary(domain_state: str | None) -> str:
    """Format domain tokens like 'com:available,dev:registered' or 'app=FREE com=taken' into colored badges."""
    if not domain_state or domain_state == "not checked":
        return dim("not checked")

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
            parts.append(f"{tld}={B_GREEN}FREE{RESET}")
        else:
            parts.append(f"{DIM}{tld}=taken{RESET}")
    return " ".join(parts) if parts else dim("not checked")


def badge_trademark(status: str | None) -> str:
    """Format trademark verdict into color badge."""
    if not status or status == "not checked":
        return dim("not checked")
    st = status.lower()
    if "clear" in st:
        return f"{B_GREEN}CLEAR{RESET}"
    if "conflict" in st or "taken" in st:
        return f"{B_RED}CONFLICT{RESET}"
    return f"{B_YELLOW}{status}{RESET}"


def print_leaderboard(
    rows: Sequence[sqlite3.Row | dict[str, Any]],
    title: str = "Top 10 Candidates",
    limit: int = 10,
    show_domains: bool = True,
    show_reasoning: bool = True,
) -> None:
    """Render a clean, pro-grade box table of top ranked candidates."""
    items = list(rows)[:limit]
    if not items:
        return

    # Column widths
    w_rank = 3
    w_cand = 14
    w_score = 7
    w_fit = 5
    w_nat = 5
    w_dom = 34 if show_domains else 0

    header_parts = [
        f"{'#':<{w_rank}}",
        f"{'Candidate':<{w_cand}}",
        f"{'Score':<{w_score}}",
        f"{'Fit':<{w_fit}}",
        f"{'Nat':<{w_nat}}",
    ]
    if show_domains:
        header_parts.append(f"{'Domains':<{w_dom}}")
    if show_reasoning:
        header_parts.append("Reasoning & Brand Fit")

    header_line = " ".join(header_parts)
    box_width = max(len(header_line) + 2, 88)

    # Box title
    title_bar = f"── {bold(b_cyan(title))} "
    dashes = "─" * max(0, box_width - len(title) - 6)
    print(f"\n┌{title_bar}{dashes}┐")
    print(f"│ {bold(header_line):<{box_width - 2}} │")
    print(f"├{'─' * (box_width - 2)}┤")

    for i, item in enumerate(items, 1):
        r = dict(item) if hasattr(item, "keys") else item
        word = r["word"]
        comp = r.get("composite", 0.0) or 0.0
        fit = r.get("fit", 0.0) or 0.0
        nat = r.get("natural", 0.0) or 0.0

        rank_str = f"{i:<{w_rank}}"
        cand_str = ansi_ljust(bold(word), w_cand)
        score_str = ansi_ljust(badge_score(comp), w_score)
        fit_str = f"{fit:.2f}"
        nat_str = f"{nat:.2f}"

        if show_reasoning:
            why = r.get("why") or ""
            why_not = r.get("why_not") or ""
            if why and why_not:
                reason = f"{why} {dim(f'[Risk: {why_not}]')}"
            elif why:
                reason = why
            elif why_not:
                reason = dim(f"[Risk: {why_not}]")
            else:
                reason = dim("-")

        # Print line with colored values
        formatted_row = f"{rank_str} {cand_str} {score_str} {fit_str:<{w_fit}} {nat_str:<{w_nat}}"
        if show_domains:
            dom_text = format_domain_summary(r.get("domains") or r.get("domain_state"))
            dom_str = ansi_ljust(dom_text, w_dom)
            formatted_row += f" {dom_str}"
        if show_reasoning:
            formatted_row += f" {reason}"

        print(f"│ {formatted_row}")

    print(f"└{'─' * (box_width - 2)}┘\n")


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
            SELECT s.word, s.composite, s.fit, s.natural, s.why, s.why_not,
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
            SELECT s.word, s.composite, s.fit, s.natural, s.why, s.why_not,
                   (SELECT GROUP_CONCAT(d.tld || ':' || d.status) FROM domains d WHERE d.word = s.word) AS domain_state
            FROM scores s
            WHERE s.qversion = ?
            ORDER BY s.composite DESC
            LIMIT ?
        """
        rows = conn.execute(sql, (qversion, limit)).fetchall()

    if rows:
        print_leaderboard(rows, title=title, limit=limit)
