"""Student-added tests for the HW1 additional tool (explain_refund_eligibility)."""

from __future__ import annotations

from agent import tools
from agent.auth import AuthContext

SHOPPER_1 = AuthContext(user_id=1, role="shopper")
SHOPPER_2 = AuthContext(user_id=2, role="shopper")
MERCHANT_STORE_2 = AuthContext(user_id=9002, role="merchant", store_id=2)


def test_explains_window_closed(world: dict) -> None:
    r = tools.explain_refund_eligibility(SHOPPER_1, 3980)
    assert r["ok"] is True and r["eligible"] is False
    assert r["days_since_delivery"] == 45 and r["return_window_days"] == 30
    assert r["window_ends_on"] == "2026-06-16" and r["policy_id"] == "cw-returns"
    assert "45 days" in r["reason"] and "30 days" in r["reason"]


def test_explains_eligible(world: dict) -> None:
    r = tools.explain_refund_eligibility(SHOPPER_1, 4127)
    assert r["eligible"] is True and r["days_since_delivery"] == 12


def test_store_override_changes_window_and_citation(world: dict) -> None:
    # Order 2485: shopper 1 bought from Juniper Home Goods (store 2, 14-day override).
    r = tools.explain_refund_eligibility(SHOPPER_1, 2485)
    assert r["store_override"] is True and r["return_window_days"] == 14
    assert r["policy_id"] == "store-juniper-home-goods-policy"


def test_scope_is_enforced(world: dict) -> None:
    assert tools.explain_refund_eligibility(SHOPPER_2, 4127)["error"] == "permission_denied"
    assert tools.explain_refund_eligibility(MERCHANT_STORE_2, 4127)["error"] == "permission_denied"
    assert tools.explain_refund_eligibility(SHOPPER_1, 999_999)["error"] == "not_found"


def test_registered_for_every_role() -> None:
    from agent.agent import TOOLS_BY_ROLE

    for role, tool_list in TOOLS_BY_ROLE.items():
        assert any(t.name == "explain_refund_eligibility" for t in tool_list), role
