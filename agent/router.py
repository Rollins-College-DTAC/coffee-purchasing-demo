"""Mixture-of-experts router, at the application level: each task goes to the expert model
best suited to it, and the reason is logged. (Inside some large models "MoE" means a learned
gate over sub-networks; here it is plain, readable rules over whole models.)"""

from agent import settings

MODELS = settings.CONFIG["models"]

EXPERTS = {
    "jev": "Jev (TypeSafe): category, urgency and injection as calibrated probabilities",
    MODELS["extract"]: "Claude, fast: extracts values from the note; Jev's classify fallback",
    MODELS["escalate"]: "Claude, strong: re-extracts hard notes; explains held imports",
    "rules": "Deterministic keyword rules: used when models are off, over budget or unavailable",
}


def route(task, note, context):
    """Return (expert, reason) for one task on one note.

    context keys: kill_switch, over_budget, jev_available, claude_available, escalate.
    """
    if context.get("kill_switch"):
        return "rules", "kill switch on"
    if context.get("over_budget"):
        return "rules", "daily budget reached"
    if not note.strip() and task in ("classify", "extract"):
        return "rules", "empty note: nothing for a model to read"

    if task == "classify":
        if context.get("jev_available"):
            return "jev", "classify questions go to Jev"
        if context.get("claude_available"):
            return MODELS["extract"], "Jev unavailable: Claude answers the same questions"
        return "rules", "Jev and Claude unavailable"

    if task == "extract":
        if not context.get("claude_available"):
            return "rules", "Claude unavailable"
        if context.get("escalate"):
            return MODELS["escalate"], f"escalated: {context['escalate']}"
        return MODELS["extract"], "values are extracted by the fast Claude model"

    if task == "explain":
        if not context.get("claude_available"):
            return "rules", "Claude unavailable"
        return MODELS["escalate"], "a held import needs a plain-English explanation"

    raise ValueError(f"unknown task {task!r}")
