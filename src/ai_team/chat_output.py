"""Portable exports and reviewable coding briefs for saved conversations."""
from __future__ import annotations

import json

from .chat_providers import redact_credentials


BRIEF_FIELDS = {
    "objective": "Objective",
    "requirements": "Requirements",
    "decisions": "Decisions",
    "rejected_alternatives": "Rejected alternatives",
    "uncertainties": "Open questions and dissent",
    "acceptance_criteria": "Acceptance criteria",
    "context": "Reference context",
}
DIAGNOSTICS = {
    "budget", "note", "error", "input_tokens", "output_tokens", "total_tokens", "context_turns", "context_truncated",
    "output_limited",
}


def sanitized(value, *, omit: set[str] | None = None):
    if isinstance(value, str):
        return redact_credentials(value)
    if isinstance(value, dict):
        return {key: sanitized(item, omit=omit) for key, item in value.items() if omit is None or key not in omit}
    if isinstance(value, list):
        return [sanitized(item, omit=omit) for item in value]
    return value


def _section(parts: list[str], heading: str, text: str, level: int) -> None:
    parts.extend([f"{'#' * level} {heading}", text or "No saved reply."])


def _reply(parts: list[str], reply: dict, level: int) -> None:
    body = reply.get("response") or "No saved reply."
    if synthesis := reply.get("synthesis"):
        sections = [f"**Recommendation**\n\n{synthesis['recommendation']}"]
        for key, label in (("agreement", "Agreement"), ("disagreement", "Disagreement"),
                           ("uncertainties", "Uncertainties"), ("next_steps", "Next steps")):
            if synthesis[key]:
                sections.append(f"**{label}**\n\n" + "\n".join(f"- {value}" for value in synthesis[key]))
        body = "\n\n".join(sections)
    metadata = [reply["status"]]
    if reply.get("input_reply_ids"):
        metadata.append("From " + ", ".join(f"#{item}" for item in reply["input_reply_ids"]))
    if reply.get("total_tokens") is not None:
        metadata.append(f"{reply['total_tokens']} tokens")
    if reply.get("context_truncated"):
        metadata.append("Context shortened")
    if reply.get("output_limited"):
        metadata.append("Output limit reached")
    if reply.get("error"):
        metadata.append(reply["error"])
    source = reply.get("response_model") or reply["model"]
    number = f" · Reply #{reply['id']}" if "participant" in reply else ""
    _section(parts, f"{reply['provider']}/{source}{number}", f"*{' · '.join(metadata)}*\n\n{body}", level)


def export(view: dict, format: str, diagnostics: bool) -> str:
    saved = sanitized(view, omit={"controls", "revision"} | (set() if diagnostics else DIAGNOSTICS))
    if format == "json":
        return json.dumps({"schema_version": 1, **saved}, ensure_ascii=False, indent=2) + "\n"

    session = saved["session"]
    parts = [f"# {session['title']}", f"{session['strategy']} · Conversation {session['id']}"]
    if source := session.get("branch_source"):
        parts.append(f"Branched from {source['title']} ({source['session_id']}).")
    if system := session.get("system_prompt"):
        _section(parts, "System instructions", system, 2)
    for index, turn in enumerate(saved.get("turns", []), 1):
        _section(parts, f"Turn {index} · You", turn["prompt"], 2)
        _reply(parts, turn, 3)
    for index, run in enumerate(saved.get("runs", []), 1):
        _section(parts, f"Run {index} · You", run["prompt"], 2)
        if run.get("context"):
            _section(parts, "Shared context", run["context"], 3)
        parts.append(f"*{run['status']}{' · Incomplete' if run['incomplete'] else ''}*")
        if run.get("note"):
            parts.append(run["note"])
        for stage in run["stages"]:
            parts.append(f"### {stage['kind'].capitalize()} {stage['id']}")
            if stage["instruction"]:
                _section(parts, "Your direction", stage["instruction"], 4)
            for reply in stage["replies"]:
                _reply(parts, reply, 4)
    if saved["handoffs"]:
        parts.append("## Linked Code tasks")
        parts.extend(f"- {link['title']} ({link['task_id']})" for link in saved["handoffs"])
    return "\n\n".join(parts) + "\n"


def _excerpt(text: str, limit: int) -> str:
    data = redact_credentials(text).encode("utf-8")
    return data[:limit].decode("utf-8", errors="ignore") + ("…" if len(data) > limit else "")


def draft(view: dict) -> dict:
    items = view.get("turns", view.get("runs", []))
    completed = [item for item in items if item["status"] not in {"pending", "running", "pausing", "stopping"}]
    empty = dict.fromkeys(BRIEF_FIELDS, "")
    if not completed:
        return empty

    latest = completed[-1]
    objective = _excerpt(latest["prompt"].splitlines()[0], 300)
    context = []
    for earlier in completed[-4:-1]:
        context.append("Earlier request\n" + _excerpt(earlier["prompt"], 380))
    if latest.get("context"):
        context.append("Shared context\n" + _excerpt(latest["context"], 850))
    if latest.get("response"):
        context.append("Model reply for reference\n" + _excerpt(latest["response"], 1000))
    replies = [reply for stage in latest.get("stages", []) for reply in stage["replies"] if reply["status"] == "complete"]
    if replies and not replies[-1].get("synthesis"):
        for reply in replies[-3:]:
            context.append(f"{reply['provider']}/{reply['model']} for reference\n" + _excerpt(reply["response"], 400))
    synthesis = next((reply["synthesis"] for reply in reversed(replies) if reply.get("synthesis")), None)
    if synthesis:
        recommendation = [synthesis["recommendation"], *synthesis["agreement"]]
        empty["decisions"] = _excerpt("Model recommendations for review\n" + "\n".join(recommendation), 2100)
        empty["uncertainties"] = _excerpt("\n".join(synthesis["disagreement"] + synthesis["uncertainties"]), 2200)
        empty["acceptance_criteria"] = _excerpt("\n".join(synthesis["next_steps"]), 1600)
    return {**empty, "objective": objective, "requirements": _excerpt(latest["prompt"], 2200),
            "context": _excerpt("\n\n".join(context), 3500)}


def coding_prompt(brief: dict) -> str:
    return "\n\n".join([brief["objective"], *(f"## {label}\n{brief[key]}"
                         for key, label in BRIEF_FIELDS.items() if key != "objective" and brief[key])])
