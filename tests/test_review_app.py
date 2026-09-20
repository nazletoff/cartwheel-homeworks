"""Tests for the HW4 review app (analysis/review_app): loader, state store, API.

Everything runs offline. Traces come from tests/fixtures/review_app_traces.json,
eight raw Langfuse traces cut from the HW3 export: support-0213 (three turns),
support-0212 (three turns), support-0243 (parallel tool calls) and support-0205
(a permission_denied tool result). State goes to a tmp dir; no Langfuse calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from analysis.review_app import loader

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "review_app_traces.json"
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def raw():
    return json.load(open(FIXTURE))["traces"]


@pytest.fixture(scope="module")
def convs(raw):
    convs = loader.group_conversations(raw)
    loader.attach_answer_keys(
        convs,
        [REPO / "scenarios" / "support_scenarios.jsonl"],
        [REPO / "scenarios" / "final-results.jsonl"],
    )
    loader.compute_flags(convs)
    return {c["scenario_id"]: c for c in convs}


# ---------------------------------------------------------------------------
# loader
# ---------------------------------------------------------------------------


def test_three_turn_session_is_one_conversation_in_time_order(convs):
    c = convs["support-0213"]
    assert c["turn_count"] == 3 and len(c["turns"]) == 3
    starts = [t["started_at"] for t in c["turns"]]
    assert starts == sorted(starts)
    assert [t["turn_index"] for t in c["turns"]] == [1, 2, 3]
    assert len({t["trace_id"] for t in c["turns"]}) == 3


def test_parallel_tool_calls_sit_under_one_step(convs):
    c = convs["support-0243"]
    steps = c["turns"][0]["steps"]
    assert any(len(s["tool_calls"]) >= 2 for s in steps)
    for s in steps:
        for call in s["tool_calls"]:
            assert call["name"] and "result" in call


def test_ok_false_result_is_marked(convs):
    c = convs["support-0205"]
    calls = [call for s in c["turns"][0]["steps"] for call in s["tool_calls"]]
    denied = [call for call in calls if call["error"] == "permission_denied"]
    assert denied and denied[0]["ok"] is False and denied[0]["permission_denied"] is True


def test_reply_is_last_and_reasoning_is_not_reply(convs):
    t = convs["support-0212"]["turns"][0]
    assert t["reply"]["text"]
    assert all(s["reasoning"] != t["reply"]["text"] for s in t["steps"])


def test_answer_key_joined(convs):
    k = convs["support-0212"]["answer_key"]
    assert k["outcome"] == "refund_denied_status_refunded"
    assert k["policy_id"] == "cw-returns" and k["runner_status"] == "completed"
    assert k["tuple"]["user_style"] == "repetitive_pressuring"


def test_durations_present(convs):
    t = convs["support-0213"]["turns"][0]
    assert t["duration_s"] > 0
    assert all(call["duration_s"] >= 0 for s in t["steps"] for call in s["tool_calls"])


# ---------------------------------------------------------------------------
# state store
# ---------------------------------------------------------------------------

from analysis.review_app.state import StateStore  # noqa: E402


@pytest.fixture
def store(tmp_path, convs):
    written = []

    def writer(trace_id, mode, label, comment=None):
        written.append((trace_id, mode, label))

    s = StateStore(tmp_path, list(convs.values()), score_writer=writer)
    s.written = written
    return s


def test_note_marks_conversation_noted(store, convs):
    c = convs["support-0213"]
    store.add_annotation(
        {
            "session_id": c["session_id"],
            "trace_id": c["turns"][2]["trace_id"],
            "block": "r",
            "quote": "refund",
            "note": "second refund refused, no escalation",
        }
    )
    assert store.status(c["session_id"]) == "noted"
    assert (store.root / "annotations.json").exists()


def test_no_failure_is_a_recorded_note(store, convs):
    c = convs["support-0205"]
    store.mark_no_failure(c["session_id"], c["turns"][0]["trace_id"])
    assert store.status(c["session_id"]) == "noted"
    assert store.annotations()[0]["kind"] == "no_failure"


def test_present_label_fans_out_absent_to_sibling_turns(store, convs):
    c = convs["support-0213"]
    store.set_patterns({"modes": [{"name": "missing_escalation", "status": "final", "definition": "x"}]})
    ev = {"trace_id": c["turns"][2]["trace_id"], "quote": "refund"}
    recs = store.set_label(c["session_id"], "missing_escalation", 1, ev)
    by_trace = {r["trace_id"]: r for r in recs}
    assert by_trace[c["turns"][2]["trace_id"]]["label"] == 1
    assert by_trace[c["turns"][0]["trace_id"]]["label"] == 0
    assert by_trace[c["turns"][0]["trace_id"]]["source"] == "default_absent"
    assert sorted(m for _, m, _ in store.written) == ["missing_escalation"] * 3
    assert (store.root / "labels" / "missing_escalation.jsonl").exists()


def test_unset_label_removes_records_and_writes_no_score(store, convs):
    c = convs["support-0205"]
    store.set_patterns({"modes": [{"name": "m", "status": "final", "definition": "x"}]})
    store.set_label(c["session_id"], "m", 0, None)
    n = len(store.written)
    store.set_label(c["session_id"], "m", None, None)
    assert store.labels(c["session_id"]).get("m") in (None, [])
    assert len(store.written) == n


def test_rejected_suggestion_stays_on_file(store, convs):
    c = convs["support-0243"]
    store.set_suggestions(
        [
            {
                "id": "s1",
                "session_id": c["session_id"],
                "trace_id": c["turns"][0]["trace_id"],
                "block": "r",
                "quote": "x",
                "mode": "m",
                "method": "grep",
            }
        ]
    )
    store.decide_suggestion("s1", "rejected", reason="not the same thing")
    rec = [s for s in store.suggestions() if s["id"] == "s1"][0]
    assert rec["status"] == "rejected" and rec["reason"] == "not the same thing"


def test_accepted_suggestion_becomes_tagged_annotation(store, convs):
    c = convs["support-0243"]
    store.set_suggestions(
        [
            {
                "id": "s2",
                "session_id": c["session_id"],
                "trace_id": c["turns"][0]["trace_id"],
                "block": "r",
                "quote": "x",
                "mode": "m",
                "method": "grep",
            }
        ]
    )
    store.decide_suggestion("s2", "accepted")
    a = store.annotations()[-1]
    assert a["source"] == "accepted_suggestion" and a["mode"] == "m"


def test_batch_refuses_duplicate_session(store, convs):
    sid = convs["support-0205"]["session_id"]
    store.add_batch("batch1", "uniform", [{"session_id": sid, "reason": "random"}])
    with pytest.raises(ValueError):
        store.add_batch("batch2", "role", [{"session_id": sid, "reason": "shopper"}])


def test_queues_and_progress(store, convs):
    sids = [c["session_id"] for c in convs.values()]
    store.add_batch("batch1", "uniform", [{"session_id": s, "reason": "r"} for s in sids])
    assert store.queue("batch1") == sids
    assert set(store.queue("role:shopper")) <= set(sids)
    p = store.progress()
    assert p["coverage"]["role"]["shopper"]["in_sample"] >= 1
    assert "grid" in p and "modes" in p


def test_merge_modes_keeps_created_from(store):
    store.set_patterns(
        {
            "modes": [
                {"name": "a", "status": "candidate", "definition": "a", "created_from": ["n1"]},
                {"name": "b", "status": "candidate", "definition": "b", "created_from": ["n2"]},
            ]
        }
    )
    p = store.merge_modes("a", "b", reason="one fix")
    names = [m["name"] for m in p["modes"]]
    assert names == ["a"] and set(p["modes"][0]["created_from"]) == {"n1", "n2"}
    assert p["modes"][0]["revisions"][-1]["change"].startswith("merged b")
