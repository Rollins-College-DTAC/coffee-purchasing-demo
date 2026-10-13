from agent import settings
from agent.router import EXPERTS, route

FAST, STRONG = settings.CONFIG["models"]["extract"], settings.CONFIG["models"]["escalate"]
UP = {"jev_available": True, "claude_available": True}


def test_kill_switch_sends_everything_to_rules():
    for task in ("classify", "extract", "explain"):
        assert route(task, "Moisture 11.2%", {**UP, "kill_switch": True})[0] == "rules"


def test_budget_cap_sends_everything_to_rules():
    expert, reason = route("extract", "Moisture 11.2%", {**UP, "over_budget": True})
    assert expert == "rules" and "budget" in reason


def test_classify_goes_to_jev():
    assert route("classify", "Moisture 11.2%", UP)[0] == "jev"


def test_classify_falls_back_to_claude_then_rules():
    assert route("classify", "x", {**UP, "jev_available": False}) == (
        FAST, "Jev unavailable: Claude answers the same questions")
    assert route("classify", "x", {})[0] == "rules"


def test_extract_goes_to_fast_claude_and_escalates_to_strong():
    assert route("extract", "Net 30", UP)[0] == FAST
    expert, reason = route("extract", "Net 30", {**UP, "escalate": "low confidence"})
    assert expert == STRONG and "low confidence" in reason


def test_explain_goes_to_strong_claude():
    assert route("explain", "held import", UP)[0] == STRONG


def test_empty_note_never_calls_a_model():
    assert route("classify", "  ", UP)[0] == "rules"


def test_every_routed_expert_is_registered():
    assert {"jev", FAST, STRONG, "rules"} == set(EXPERTS)
