"""Per-task spending totals from recorded provider invocation responses."""
from __future__ import annotations

import math
from typing import Any


def _number(value: Any) -> float | None:
    if type(value) not in (int, float) or value < 0:
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def summary(budget: dict[str, Any], usages: list[Any]) -> dict[str, Any]:
    used_tokens = 0
    used_cost = 0.0
    missing_tokens = False
    missing_cost = False
    for usage in usages:
        if not isinstance(usage, dict):
            usage = {}
        tokens = _number(usage.get("total_tokens"))
        if tokens is None:
            inputs = _number(usage.get("input_tokens"))
            outputs = _number(usage.get("output_tokens"))
            tokens = inputs + outputs if inputs is not None and outputs is not None else None
        if tokens is None or not tokens.is_integer():
            missing_tokens = True
        else:
            used_tokens += int(tokens)
        cost = _number(usage.get("total_cost_usd", usage.get("cost_usd")))
        if cost is None:
            missing_cost = True
        else:
            used_cost += cost
    token_limit = budget["tokens"]
    cost_limit = budget["cost_usd"]
    return {
        "limit": budget,
        "used": {"tokens": None if missing_tokens else used_tokens,
                 "cost_usd": None if missing_cost else used_cost},
        "remaining": {"tokens": None if token_limit is None or missing_tokens else max(0, token_limit - used_tokens),
                      "cost_usd": None if cost_limit is None or missing_cost else max(0.0, cost_limit - used_cost)},
        "missing_usage": {"tokens": missing_tokens, "cost_usd": missing_cost},
    }


def pause_reason(state: dict[str, Any]) -> str | None:
    limit, used, missing = state["limit"], state["used"], state["missing_usage"]
    if limit["tokens"] is not None:
        if missing["tokens"]:
            return "Token budget cannot be checked: a recorded invocation has missing or invalid token usage."
        if used["tokens"] >= limit["tokens"]:
            return f"Token budget reached: {used['tokens']} / {limit['tokens']} tokens used."
    if limit["cost_usd"] is not None:
        if missing["cost_usd"]:
            return "Cost budget cannot be checked: a provider returned missing or invalid cost data."
        if used["cost_usd"] >= limit["cost_usd"]:
            return f"Cost budget reached: ${used['cost_usd']:.4f} / ${limit['cost_usd']:.4f} used."
    return None
