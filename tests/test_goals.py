import hashlib
import json
from pathlib import Path

from analysis.goals import validate_goal_protocol


def actual_fixture():
    text = "Write the answer to the frozen fixture question."
    protocol = {
        "status": "frozen",
        "goal_text_hash": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        "family": "F5",
        "goal_class": "bounded",
        "signal_type": "judged",
        "evaluation_protocol": "Code-verify the frozen fixture answer; preference separately.",
        "G": [],
        "V": ["fixed-answer check"],
        "J": ["blind preference"],
        "S": ["interview"],
        "predicted_behavior": "Produce a concise answer.",
        "predicted_blockers": ["fixture unavailable"],
        "likely_asks": [],
        "deliverable_location": "/work/out/",
        "human_time_estimate": 2,
        "messiness": 1,
        "budget_multiple": 1,
        "gaming_channels": {
            "forbidden": ["grader reads"],
            "flagged": ["self-citation"],
            "counted": ["abstention"],
        },
        "frozen_checklist": [],
    }
    return text, protocol


def test_goal_protocol_binds_exact_text_and_actual_full_metadata():
    text, protocol = actual_fixture()
    assert validate_goal_protocol(text, protocol)["valid"]
    assert not validate_goal_protocol(text + " ", protocol)["valid"]
    protocol["human_time_estimate"] = None
    protocol["messiness"] = None
    result = validate_goal_protocol(text, protocol)
    assert not result["valid"] and len(result["errors"]) == 2


def test_all_draft_catalog_protocols_are_templates_not_scientific_goal_metadata():
    root = Path(__file__).parents[1]
    protocol = json.loads((root / "goals/01-open-budget/protocol.json").read_text())
    assert not validate_goal_protocol("", protocol)["valid"]
    text, complete = actual_fixture()
    complete["frozen_checklist"] = [{"item_id": "hidden"}]
    assert not validate_goal_protocol(text, complete)["valid"]
    complete["hidden_items_ref"] = {"path": "private/hidden.json", "hash": "sha256:" + "a" * 64}
    assert validate_goal_protocol(text, complete)["valid"]
