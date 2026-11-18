"""Per-command spend tracking and cost limit for OpenRouter calls.

OpenRouter reports `usage.cost` on every response. Totals are kept for the
lifetime of one CLI command. Once the limit is reached no *new* requests are
issued; requests already in flight finish, so spend can overshoot by roughly
`concurrency x cost-per-request` (a fraction of a cent for typical batches).
A limit of 0 disables the check.
"""

from __future__ import annotations

from . import config

_state: dict[str, float] = {}
_limit: float = config.MAX_COST


def reset(limit: float | None = None) -> None:
    """Zero the counters at the start of a command and set its cost limit."""
    global _limit
    _limit = config.MAX_COST if limit is None else float(limit)
    _state.clear()
    _state.update({"calls": 0, "prompt": 0, "completion": 0, "reasoning": 0, "cost": 0.0})


reset()


def limit() -> float:
    return _limit


def record(payload: dict) -> None:
    u = payload.get("usage") or {}
    _state["calls"] += 1
    _state["prompt"] += u.get("prompt_tokens") or 0
    _state["completion"] += u.get("completion_tokens") or 0
    _state["reasoning"] += (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
    _state["cost"] += u.get("cost") or 0.0


def spent() -> float:
    return _state["cost"]


def calls() -> int:
    return int(_state["calls"])


def exhausted() -> bool:
    return _limit > 0 and _state["cost"] >= _limit


def _usd(v: float) -> str:
    return f"${v:.2f}" if v >= 0.1 else f"${v:.4f}"


def _k(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(int(n))


def metrics_text() -> str:
    """Compact live string for progress bars."""
    cap = _usd(_limit) if _limit > 0 else "no limit"
    return f"${_state['cost']:.4f}/{cap} | {_k(_state['prompt'])} in {_k(_state['completion'])} out"


def summary() -> str:
    s = _state
    cap = _usd(_limit) if _limit > 0 else "none"
    return (f"[usage] calls={int(s['calls'])} in={int(s['prompt']):,} out={int(s['completion']):,} "
            f"(reasoning={int(s['reasoning']):,}) cost=${s['cost']:.5f} (limit {cap})")


def stop_notice(remaining: int | None = None) -> str:
    extra = f" {remaining} items were left unprocessed;" if remaining else ""
    return (f"[budget] cost limit {_usd(_limit)} reached (spent ${_state['cost']:.4f});{extra} "
            "progress is saved - rerun to continue, or raise it with --max-cost")
