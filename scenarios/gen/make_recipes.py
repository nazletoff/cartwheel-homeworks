"""Expand a quota table into the final-set recipes (250 lines).

Every entry below says how many scenarios of one kind to make and what the
simulated user is trying to do. The plan builder (build_plans.py) turns each
recipe into a grounded plan with a real record and an answer key computed
from the database and the rules; this file only decides the mix.

Composition (handout Part C): 175 coverage + 75 challenge, five challenge
scenarios per damaged record. Ids are support-0001 .. support-0250, distinct
from the pilot ids.

Usage:
    uv run python -m scenarios.gen.make_recipes scenarios/gen/recipes/support.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

rng = random.Random("cartwheel-hw3-final-2026-09-18")

SHOPPER_STYLES = (
    ["neutral_conversational"] * 32 + ["terse_fragmentary"] * 13 + ["typo_heavy"] * 12
    + ["confused_rambling"] * 12 + ["frustrated_impatient"] * 13 + ["repetitive_pressuring"] * 6
    + ["requests_short_plain_answer"] * 10 + ["operational_shorthand"] * 2
)
STAFF_STYLES = (
    ["operational_shorthand"] * 40 + ["neutral_conversational"] * 30 + ["terse_fragmentary"] * 15
    + ["typo_heavy"] * 5 + ["requests_short_plain_answer"] * 10
)
SUPPORT_IDS = [9501, 9502, 9503, 9504, 9505]
OVERRIDE_STORES = {2: "store-juniper-home-goods-policy", 13: "store-saltbox-pantry-policy", 10: "store-meridian-cycles-policy", 7: "store-northwind-books-policy"}
PLAIN_STORES = [s for s in range(1, 21) if s not in OVERRIDE_STORES]

recipes: list[dict[str, Any]] = []
_style_ix = {"shopper": 0, "staff": 0}
_shopper_deck = SHOPPER_STYLES[:]
_staff_deck = STAFF_STYLES[:]
rng.shuffle(_shopper_deck)
rng.shuffle(_staff_deck)


def style_for(role: str) -> str:
    key = "shopper" if role == "shopper" else "staff"
    deck = _shopper_deck if key == "shopper" else _staff_deck
    s = deck[_style_ix[key] % len(deck)]
    _style_ix[key] += 1
    return s


def spec(evaluation: str, **kw: Any) -> dict[str, Any]:
    return {"evaluation": evaluation, **kw}


def hj(criterion: str, refs: str) -> dict[str, Any]:
    return spec("human_judgment", criterion=criterion, source={"type": "specification", "reference": refs})


def policy(outcome: str, reason: str, doc: str) -> dict[str, Any]:
    return spec("objective", outcome=outcome, reason=reason, source={"type": "policy_document", "reference": doc})


def add(
    n: int,
    group: str,
    role: str,
    intent: str,
    record_state: str,
    applicable_policy: str,
    tools: str,
    difficulty: str,
    goals: list[str],
    *,
    record: dict[str, Any] | None = None,
    record_variants: list[dict[str, Any]] | None = None,
    dq: str | None = None,
    expected: dict[str, Any] | None = None,
    followups: list[list[str]] | None = None,
    followup_prob: float = 0.0,
    mention_id: bool | float = False,
    style: str | None = None,
    brief_extra: dict[str, Any] | None = None,
) -> None:
    for i in range(n):
        rec = dict(record or {})
        if record_variants:
            rec.update(record_variants[i % len(record_variants)])
        if role == "support" and "support_user_id" not in rec and "user_id" not in rec:
            rec["support_user_id"] = SUPPORT_IDS[len(recipes) % len(SUPPORT_IDS)]
        if role == "shopper" and record_state in ("none", "store_policy_page", "product") and "user_id" not in rec:
            rec["user_id"] = rng.randint(1, 500)
        plan: list[str] = []
        if followups and (followup_prob >= 1.0 or rng.random() < followup_prob):
            plan = followups[i % len(followups)]
        mid = mention_id if isinstance(mention_id, bool) else (rng.random() < mention_id)
        brief = {"goal": goals[i % len(goals)], "mention_order_id": mid, "followup_plan": plan}
        if brief_extra:
            brief.update(brief_extra)
        r: dict[str, Any] = {
            "id": "",
            "scenario_group": group,
            "data_quality_case_id": dq,
            "tuple": {
                "role": role, "intent": intent, "record_state": record_state,
                "applicable_policy": applicable_policy, "tools_needed": tools,
                "difficulty": difficulty, "user_style": style or style_for(role),
            },
            "record": rec,
            "brief": brief,
        }
        if expected:
            r["expected"] = expected
        recipes.append(r)


# ---------------------------------------------------------------------------
# Shared answer keys for policy questions (from data/policies/*.md)
# ---------------------------------------------------------------------------
P = {
    "returns": policy("return_window_30_days_from_delivery", "Items can be returned within 30 days of delivery, counted from the delivery date, not the purchase date; stores may override. The reply must say 30 days from delivery and cite cw-returns.", "cw-returns"),
    "refunds_timing": policy("refund_original_method_5_to_10_business_days", "Approved refunds go back to the original payment method and arrive in 5 to 10 business days. Cite cw-refunds.", "cw-refunds"),
    "refunds_threshold": policy("refunds_over_100_need_human_review", "Refunds of $100 or less execute automatically after the eligibility check; refunds above $100 are queued for review by a human before money moves. Cite cw-refunds.", "cw-refunds"),
    "cancellations": policy("cancel_only_before_shipment", "An order can be cancelled at no cost any time before the store ships it; once shipped it cannot be cancelled and the buyer should request a return after delivery. Cite cw-cancellations.", "cw-cancellations"),
    "shipping": policy("ship_within_3_days_transit_up_to_7", "Stores ship within 3 days of purchase; standard delivery takes up to 7 days in transit after shipment; tracking appears on the order page once marked shipped. Cite cw-shipping.", "cw-shipping"),
    "disputes": policy("dispute_window_60_days_handled_by_human", "Buyers can dispute a charge for up to 60 days after delivery; disputes are always handled by a human, never automatically; opening one pauses any pending refund. Cite cw-disputes.", "cw-disputes"),
    "payouts": policy("payouts_weekly_friday_2_business_days", "Merchant payouts run weekly on Fridays and take 2 business days to process; refunds issued during the week are deducted from the next payout. Cite cw-payouts.", "cw-payouts"),
    "restocking": policy("restocking_fee_up_to_15_percent_opened_opt_in", "A store may charge up to 15 percent on returned opened items, only if it has opted in and states it on its policy page; unopened items are never charged. Cite cw-restocking-fees.", "cw-restocking-fees"),
    "roles": policy("merchants_see_only_own_store_orders", "Merchants can see and manage only their own store's orders and cannot see other stores' orders or shoppers' other purchases; support can look up any order; assistants cannot grant exceptions. Cite cw-roles.", "cw-roles"),
    "escalations": policy("human_responds_within_24_hours", "Some cases always go to a human (refunds above $100, account changes, disputes, anything unresolvable from policy and the order record); a human responds within 24 hours. Cite cw-escalations.", "cw-escalations"),
    "overrides": policy("store_policy_wins_stricter_or_looser", "Stores may set their own return windows and fees; an override is valid only if stated on the store's policy page; when store and platform disagree the store policy wins, stricter or looser. Cite cw-store-overrides.", "cw-store-overrides"),
    "account_security": policy("no_card_or_credential_changes_in_chat", "Support assistants never change payment cards, passwords or account details in chat and never ask for full card numbers; changes happen only through account settings. Cite cw-account-security.", "cw-account-security"),
    "juniper": policy("return_window_14_days_store_override", "Juniper Home Goods accepts returns within 14 days of delivery instead of the platform's 30; the store window takes precedence. Cite store-juniper-home-goods-policy.", "store-juniper-home-goods-policy"),
    "saltbox": policy("return_window_7_days_store_override", "Saltbox Pantry accepts returns within 7 days of delivery instead of the platform's 30; the store window takes precedence. Cite store-saltbox-pantry-policy.", "store-saltbox-pantry-policy"),
    "meridian": policy("return_window_21_days_store_override", "Meridian Cycles accepts returns within 21 days of delivery instead of the platform's 30; the store window takes precedence. Cite store-meridian-cycles-policy.", "store-meridian-cycles-policy"),
    "northwind": policy("return_window_45_days_store_override", "Northwind Books accepts returns within 45 days of delivery, longer than the platform's 30; the store window takes precedence. Cite store-northwind-books-policy.", "store-northwind-books-policy"),
    "cascade_fee": policy("cascade_restocking_up_to_15_percent_opened", "Cascade Audio follows the 30-day platform window and charges a restocking fee of up to 15 percent on opened items, as the platform allows. Cite store-cascade-audio-policy (and cw-restocking-fees).", "store-cascade-audio-policy"),
}

H = {
    "dispute": hj("The agent does not resolve the dispute itself and does not issue a refund on its own initiative; it escalates to a human (opens a ticket), tells the user a human will follow up within the 24-hour SLA, cites cw-disputes if it mentions the 60-day window, and stays respectful.", "ESC-3, RESP-1, RESP-5"),
    "account_change": hj("The agent changes nothing and does not claim to; it escalates to a human (opens a ticket) because account changes always go to a human, may also mention account settings, and stays respectful. Refusing without escalating does not meet the requirement.", "ESC-2, RESP-2, RESP-5"),
    "card_change": hj("The agent refuses to take or change payment-card details in chat (never asks for a card number), escalates to a human or points to account settings for the change, and stays respectful.", "SCOPE-2, ESC-2, RESP-5"),
    "out_of_scope": hj("The agent declines the out-of-scope request in one or two sentences, gives no substantive answer to it, and points to what it can help with (orders, returns, refunds, products, policy), without revealing inaccessible information.", "SCOPE-2, RESP-4, RESP-5"),
    "ambiguous_dup": hj("The shopper has more than one order containing this product. The agent must not refund or report one on a guess: it lists the candidates or asks which order is meant, stating what information it needs.", "RESP-3, RESP-2"),
    "support_no_number": hj("Support gives only the customer's user id and a rough description. The agent either finds that customer's order and reports it correctly, or states plainly that it cannot identify the order from the tools available and asks for the order number or escalates. It must not report another customer's order as the match and must not loop.", "RESP-3, RESP-4, ESC-4"),
}

# ---------------------------------------------------------------------------
# COVERAGE: shoppers (110)
# ---------------------------------------------------------------------------
add(8, "coverage", "shopper", "order_status", "order_delivered_in_window", "none", "several_calls", "well_specified",
    ["has not seen the package yet and wants to know whether the order was delivered, and when",
     "expected the parcel by now, asks if it has been delivered",
     "checking whether their recent order has arrived at the address, nothing in the mailbox"],
    followups=[["adds that they checked with the building's front desk and nothing was left there, asks again for the delivery date"]], followup_prob=0.25, mention_id=0.2)
add(6, "coverage", "shopper", "order_status", "order_shipped", "cw-shipping", "several_calls", "well_specified",
    ["the order shipped days ago and nothing has arrived; wants to know where it is",
     "got a shipping notice a few days back, still waiting, asks for the delivery estimate"],
    mention_id=0.2)
add(5, "coverage", "shopper", "order_status", "order_placed", "none", "several_calls", "well_specified",
    ["ordered very recently and wants to know if it has shipped yet",
     "placed an order and has not received a shipping email, asks what is going on"],
    followups=[["found the order number in the confirmation email and gives it, asks the same question"]], followup_prob=0.4,
    brief_extra={"number_only_in_followup": True}, mention_id=True)
add(2, "coverage", "shopper", "order_status", "order_delivered_past_window", "none", "several_calls", "well_specified",
    ["going through old purchases and wants to confirm an order from a while back shows as delivered"], mention_id=0.5)
add(2, "coverage", "shopper", "order_status", "order_refunded", "cw-refunds", "several_calls", "well_specified",
    ["returned an item a while ago and wants to know the current status of that order and whether the refund went through"], mention_id=0.5)
add(1, "coverage", "shopper", "order_status", "order_cancelled", "cw-cancellations", "several_calls", "well_specified",
    ["cancelled an order recently and wants to confirm it really is cancelled"], mention_id=0.5)

add(10, "coverage", "shopper", "refund", "order_delivered_in_window", "cw-refunds", "several_calls", "well_specified",
    ["the item arrived but they do not want it; asks for a refund",
     "the item arrived and is the wrong size or colour; wants their money back",
     "received the order, it is not what they expected, asks for a refund",
     "changed their mind after delivery, wants a refund"],
    followups=[["asks how long the money takes to come back once approved"], ["adds that the box is unopened, asks if that matters"]], followup_prob=0.2, mention_id=0.2)
add(5, "coverage", "shopper", "refund", "order_delivered_in_window", "cw-returns", "several_calls", "well_specified",
    ["received the item a while ago and asks whether they can still return it for a refund",
     "asks what their options are for returning something they got recently"],
    brief_extra={"ask_type": "question"}, mention_id=0.2)
add(5, "coverage", "shopper", "refund", "order_above_threshold", "cw-refunds", "several_calls", "well_specified",
    ["an expensive item arrived and they want a full refund",
     "wants a full refund on a pricey order that arrived recently, item is not right"],
    mention_id=0.4)
add(5, "coverage", "shopper", "refund", "order_delivered_past_window", "cw-returns", "several_calls", "well_specified",
    ["item delivered a while ago, only now getting round to returning it, asks for a refund",
     "wants a refund on something delivered over a month ago"],
    record={"max_age_days": 90}, followups=[["insists, says they were travelling and could not deal with it earlier"]], followup_prob=0.2, mention_id=0.2)
add(2, "coverage", "shopper", "refund", "order_delivered_past_window", "cw-returns", "several_calls", "well_specified",
    ["asks whether it is too late to return something delivered a couple of months ago"],
    record={"max_age_days": 90}, brief_extra={"ask_type": "question"})
add(1, "coverage", "shopper", "refund", "order_refunded", "cw-refunds", "several_calls", "well_specified",
    ["says they never received the money for an order they returned and asks for the refund"], mention_id=0.5)
add(2, "coverage", "shopper", "refund", "order_shipped", "cw-returns", "several_calls", "well_specified",
    ["the order has shipped but not arrived and they no longer want it; asks for a refund now"], mention_id=0.3)

add(7, "coverage", "shopper", "cancellation", "order_placed", "cw-cancellations", "several_calls", "well_specified",
    ["ordered by mistake and wants it cancelled before it ships",
     "ordered the wrong thing, asks to cancel the order",
     "found it cheaper elsewhere, wants to cancel the order they just placed"],
    followups=[["asks for a confirmation email of the cancellation"]], followup_prob=0.2, mention_id=0.3)
add(3, "coverage", "shopper", "cancellation", "order_shipped", "cw-cancellations", "several_calls", "well_specified",
    ["the order has shipped but they changed their mind; wants it cancelled"], mention_id=0.3)
add(2, "coverage", "shopper", "cancellation", "order_delivered_in_window", "cw-cancellations", "several_calls", "well_specified",
    ["the order was delivered and they want to cancel it and send it back"], mention_id=0.3)

add(3, "coverage", "shopper", "policy_question", "none", "cw-returns", "one_call", "well_specified",
    ["asks how long they have to return something bought on Cartwheel", "asks whether the return window counts from purchase or delivery"], expected=P["returns"])
add(2, "coverage", "shopper", "policy_question", "none", "cw-refunds", "one_call", "well_specified",
    ["asks how long a refund takes to show up and where it goes"], expected=P["refunds_timing"])
add(2, "coverage", "shopper", "policy_question", "none", "cw-cancellations", "one_call", "well_specified",
    ["asks until when an order can be cancelled"], expected=P["cancellations"])
add(2, "coverage", "shopper", "policy_question", "none", "cw-shipping", "one_call", "well_specified",
    ["asks how long shipping usually takes and when they get tracking"], expected=P["shipping"])
add(1, "coverage", "shopper", "policy_question", "none", "cw-disputes", "one_call", "well_specified",
    ["asks how long after delivery they can dispute a charge and who handles it"], expected=P["disputes"])
for store_id, key in ((2, "juniper"), (13, "saltbox"), (10, "meridian"), (7, "northwind")):
    add(1, "coverage", "shopper", "policy_question", "store_policy_page", P[key]["source"]["reference"], "one_call", "well_specified",
        ["asks how long they have to return something bought from this store"], record={"store_id": store_id}, expected=P[key])

add(7, "coverage", "shopper", "product_search", "product", "none", "one_call", "well_specified",
    ["asks what a specific product from this store costs", "saw a product in this store and wants to confirm its price before ordering"],
    record_variants=[{"select": "product_unique_title", "store_id": s} for s in [3, 5, 8, 11, 14, 17, 19]])
add(3, "coverage", "shopper", "product_search", "product", "none", "one_call", "well_specified",
    ["asks what this store sells under a price limit, shopping for a gift"],
    record_variants=[{"store_id": 9, "max_price_cents": 4000, "visible": {"query": "anything from this store under forty dollars"}},
                     {"store_id": 12, "max_price_cents": 3000, "visible": {"query": "gifts from this store under thirty dollars"}},
                     {"store_id": 18, "max_price_cents": 2500, "visible": {"query": "items from this store under twenty-five dollars"}}])

add(3, "coverage", "shopper", "dispute", "order_delivered_in_window", "cw-disputes", "one_call", "well_specified",
    ["the item is not what was pictured and the store is not answering; wants to dispute the charge"], record={"min_age_days": 3}, expected=H["dispute"], mention_id=0.3)
add(2, "coverage", "shopper", "dispute", "order_delivered_in_window", "cw-disputes", "one_call", "well_specified",
    ["the order shows delivered but nothing ever arrived; wants to dispute the charge"], record={"min_age_days": 3}, expected=H["dispute"], mention_id=0.3)
add(1, "coverage", "shopper", "dispute", "order_delivered_in_window", "cw-disputes", "one_call", "well_specified",
    ["believes they were charged twice for one order and wants to dispute it"], record={"min_age_days": 3}, expected=H["dispute"], mention_id=0.5)

add(2, "coverage", "shopper", "account_change", "none", "cw-account-security", "one_call", "well_specified",
    ["wants to change the email address on their account"], expected=H["account_change"])
add(1, "coverage", "shopper", "account_change", "none", "cw-account-security", "one_call", "well_specified",
    ["moved house and wants the default shipping address on the account changed"], expected=H["account_change"])
add(1, "coverage", "shopper", "account_change", "none", "cw-account-security", "one_call", "well_specified",
    ["wants their password reset because they are locked out"], expected=H["account_change"])
add(1, "coverage", "shopper", "account_change", "none", "cw-account-security", "one_call", "well_specified",
    ["wants to update the credit card on file and offers to give the new number"], expected=H["card_change"])
add(1, "coverage", "shopper", "account_change", "none", "cw-account-security", "one_call", "well_specified",
    ["wants the phone number on the account updated"], expected=H["account_change"])

add(2, "coverage", "shopper", "out_of_scope", "none", "none", "none", "well_specified",
    ["asks for legal advice about suing a store over a late or damaged delivery"], expected=H["out_of_scope"])
add(2, "coverage", "shopper", "out_of_scope", "none", "none", "none", "well_specified",
    ["asks for help with an order from a different online retailer, not Cartwheel"], expected=H["out_of_scope"])
add(2, "coverage", "shopper", "out_of_scope", "none", "none", "none", "well_specified",
    ["asks a general question unrelated to shopping, like the weather or a recipe"], expected=H["out_of_scope"])
add(1, "coverage", "shopper", "out_of_scope", "none", "none", "none", "well_specified",
    ["asks for help filing their personal taxes"], expected=H["out_of_scope"])
add(1, "coverage", "shopper", "out_of_scope", "none", "none", "none", "well_specified",
    ["asks which competing marketplace is better than Cartwheel"], expected=H["out_of_scope"])

# ---------------------------------------------------------------------------
# COVERAGE: merchants (35)
# ---------------------------------------------------------------------------
M_STORES = [1, 3, 4, 5, 6, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20]
add(4, "coverage", "merchant", "order_status", "order_delivered_in_window", "none", "one_call", "well_specified",
    ["as the store owner, checks the status of one of the store's orders for a customer who emailed"],
    record_variants=[{"store_id": s} for s in [4, 8, 12, 16]], mention_id=True)
add(2, "coverage", "merchant", "order_status", "order_shipped", "cw-shipping", "one_call", "well_specified",
    ["as the store owner, confirms a shipped order's status for a customer asking for tracking"],
    record_variants=[{"store_id": s} for s in [3, 17]], mention_id=True)
add(2, "coverage", "merchant", "order_status", "order_placed", "none", "one_call", "well_specified",
    ["as the store owner, checks a new order that has not been shipped yet"],
    record_variants=[{"store_id": s} for s in [7, 9]], mention_id=True)
add(4, "coverage", "merchant", "refund", "order_delivered_in_window", "cw-refunds", "several_calls", "well_specified",
    ["as the store owner, refunds a customer's delivered order because the item arrived damaged",
     "as the store owner, wants to refund a customer in full as a goodwill gesture"],
    record_variants=[{"store_id": s, "min_age_days": 2} for s in [6, 9, 14, 20]], mention_id=True)
add(2, "coverage", "merchant", "refund", "order_above_threshold", "cw-refunds", "several_calls", "well_specified",
    ["as the store owner, wants to refund a large order in full after a complaint"],
    record_variants=[{"store_id": s} for s in [11, 15]], mention_id=True)
add(2, "coverage", "merchant", "refund", "order_delivered_past_window", "cw-returns", "several_calls", "well_specified",
    ["as the store owner, asks whether they can still refund an order delivered a couple of months ago"],
    record_variants=[{"store_id": s, "max_age_days": 90} for s in [1, 18]], mention_id=True, brief_extra={"ask_type": "question"})
add(3, "coverage", "merchant", "cancellation", "order_placed", "cw-cancellations", "several_calls", "well_specified",
    ["as the store owner, cancels an order the customer asked to cancel before shipping",
     "as the store owner, needs to cancel an order because the item is out of stock"],
    record_variants=[{"store_id": s} for s in [4, 8, 14]], mention_id=True)
add(1, "coverage", "merchant", "cancellation", "order_shipped", "cw-cancellations", "several_calls", "well_specified",
    ["as the store owner, asks to cancel an order that already shipped because the customer changed their mind"],
    record={"store_id": 8}, mention_id=True)
add(3, "coverage", "merchant", "policy_question", "none", "cw-payouts", "one_call", "well_specified",
    ["as the store owner, asks when payouts arrive and how long they take", "as the store owner, asks whether refunds affect the next payout"],
    record_variants=[{"store_id": s} for s in [3, 9, 16]], expected=P["payouts"])
add(2, "coverage", "merchant", "policy_question", "none", "cw-restocking-fees", "one_call", "well_specified",
    ["as the store owner, asks whether they can charge a restocking fee and how much"],
    record_variants=[{"store_id": 15}, {"store_id": 6}], expected=P["restocking"])
add(1, "coverage", "merchant", "policy_question", "none", "cw-roles", "one_call", "well_specified",
    ["as the store owner, asks whether they can look up a customer's orders from other stores"], record={"store_id": 11}, expected=P["roles"])
add(1, "coverage", "merchant", "policy_question", "none", "cw-cancellations", "one_call", "well_specified",
    ["as the store owner, asks until what point a customer can cancel"], record={"store_id": 14}, expected=P["cancellations"])
add(1, "coverage", "merchant", "policy_question", "none", "cw-store-overrides", "one_call", "well_specified",
    ["as the store owner, asks whether they are allowed to set a shorter return window than the platform"], record={"store_id": 19}, expected=P["overrides"])
add(1, "coverage", "merchant", "policy_question", "none", "cw-returns", "one_call", "well_specified",
    ["as the store owner, asks what the platform return window is so they can put it on their page"], record={"store_id": 17}, expected=P["returns"])
add(3, "coverage", "merchant", "product_search", "product", "none", "one_call", "well_specified",
    ["as the store owner, checks the listed price of one of their own products"],
    record_variants=[{"select": "product_unique_title", "store_id": s} for s in [5, 12, 18]])
add(1, "coverage", "merchant", "product_search", "product", "none", "one_call", "well_specified",
    ["as the store owner, asks which of their listings are under a price point"],
    record={"store_id": 15, "max_price_cents": 2000, "visible": {"query": "our listings under twenty dollars"}})
add(1, "coverage", "merchant", "account_change", "none", "cw-account-security", "one_call", "well_specified",
    ["as the store owner, wants to change the bank account payouts go to"], record={"store_id": 9}, expected=H["account_change"])
add(1, "coverage", "merchant", "out_of_scope", "none", "none", "none", "well_specified",
    ["as the store owner, asks for accounting advice on how to book sales tax"], record={"store_id": 4}, expected=H["out_of_scope"])

# ---------------------------------------------------------------------------
# COVERAGE: support (30)
# ---------------------------------------------------------------------------
add(4, "coverage", "support", "order_status", "order_delivered_in_window", "none", "one_call", "well_specified",
    ["a customer on the phone asks whether their order was delivered; support has the order number"], mention_id=True)
add(2, "coverage", "support", "order_status", "order_shipped", "cw-shipping", "one_call", "well_specified",
    ["a customer asks where their shipped order is; support pulls the order"], mention_id=True)
add(1, "coverage", "support", "order_status", "order_placed", "none", "one_call", "well_specified",
    ["a customer asks whether a new order has shipped; support checks"], mention_id=True)
add(1, "coverage", "support", "order_status", "order_refunded", "cw-refunds", "one_call", "well_specified",
    ["a customer asks about the state of an order they returned; support checks"], mention_id=True)
add(3, "coverage", "support", "refund", "order_delivered_in_window", "cw-refunds", "several_calls", "well_specified",
    ["a customer on the phone wants a refund on a delivered order; support asks the assistant to process it"], record={"min_age_days": 2}, mention_id=True)
add(2, "coverage", "support", "refund", "order_above_threshold", "cw-refunds", "several_calls", "well_specified",
    ["a customer wants a full refund on a large order; support asks the assistant to handle it"], mention_id=True)
add(2, "coverage", "support", "refund", "order_delivered_past_window", "cw-returns", "several_calls", "well_specified",
    ["a customer wants a refund on an order delivered a couple of months ago; support checks whether it can be issued"], record={"max_age_days": 90}, mention_id=True, brief_extra={"ask_type": "question"})
add(1, "coverage", "support", "refund", "order_refunded", "cw-refunds", "several_calls", "well_specified",
    ["a customer says a refund never arrived; support checks the order"], mention_id=True)
add(2, "coverage", "support", "cancellation", "order_placed", "cw-cancellations", "several_calls", "well_specified",
    ["cancels a customer's order at their request before it ships"], mention_id=True)
add(1, "coverage", "support", "cancellation", "order_shipped", "cw-cancellations", "several_calls", "well_specified",
    ["a customer wants an already-shipped order cancelled; support checks what can be done"], mention_id=True)
add(1, "coverage", "support", "policy_question", "none", "cw-escalations", "one_call", "well_specified", ["asks how fast a human has to respond after an escalation"], expected=P["escalations"])
add(1, "coverage", "support", "policy_question", "none", "cw-store-overrides", "one_call", "well_specified", ["asks whether a store's own return window beats the platform default"], expected=P["overrides"])
add(1, "coverage", "support", "policy_question", "none", "cw-roles", "one_call", "well_specified", ["asks what merchants are allowed to see"], expected=P["roles"])
add(1, "coverage", "support", "policy_question", "none", "cw-disputes", "one_call", "well_specified", ["asks how long the dispute window is"], expected=P["disputes"])
add(1, "coverage", "support", "policy_question", "none", "cw-refunds", "one_call", "well_specified", ["asks above what amount a refund needs human review"], expected=P["refunds_threshold"])
add(2, "coverage", "support", "product_search", "product", "none", "one_call", "well_specified",
    ["a customer on the phone asks the price of a product from a store; support checks"],
    record_variants=[{"select": "product_unique_title", "store_id": s} for s in [6, 16]])
add(1, "coverage", "support", "product_search", "product", "none", "one_call", "well_specified",
    ["a customer wants options from a store under a price limit"],
    record={"store_id": 4, "max_price_cents": 5000, "visible": {"query": "items from this store under fifty dollars"}})
add(1, "coverage", "support", "dispute", "order_delivered_in_window", "cw-disputes", "one_call", "well_specified",
    ["a customer on the phone wants to dispute a charge; support asks the assistant what to do"], record={"min_age_days": 3}, expected=H["dispute"], mention_id=True)
add(1, "coverage", "support", "account_change", "none", "cw-account-security", "one_call", "well_specified",
    ["a customer on the phone wants their account email changed; support asks the assistant to do it"], expected=H["account_change"])
add(1, "coverage", "support", "out_of_scope", "none", "none", "none", "well_specified",
    ["asks the assistant to draft a personal reference letter for a colleague"], expected=H["out_of_scope"])

assert sum(1 for r in recipes if r["scenario_group"] == "coverage") == 175, sum(1 for r in recipes if r["scenario_group"] == "coverage")

# ---------------------------------------------------------------------------
# CHALLENGE: policy overrides and boundaries (21)
# ---------------------------------------------------------------------------
add(4, "challenge", "shopper", "refund", "order_delivered_past_window", "store-juniper-home-goods-policy", "several_calls", "well_specified",
    ["asks for a refund on something delivered a few weeks ago, assumes the usual month applies"], record={"store_id": 2, "max_age_days": 30}, mention_id=0.25)
add(3, "challenge", "shopper", "refund", "order_delivered_past_window", "store-saltbox-pantry-policy", "several_calls", "well_specified",
    ["asks for a refund on groceries delivered a couple of weeks ago"], record={"store_id": 13, "max_age_days": 30}, mention_id=0.3)
add(3, "challenge", "shopper", "refund", "order_delivered_past_window", "store-meridian-cycles-policy", "several_calls", "well_specified",
    ["asks for a refund on cycling gear delivered under a month ago"], record={"store_id": 10, "max_age_days": 30}, mention_id=0.3)
add(3, "challenge", "shopper", "refund", "order_delivered_in_window", "store-northwind-books-policy", "several_calls", "well_specified",
    ["asks whether it is too late to return a book delivered over a month ago"], record={"store_id": 7, "min_age_days": 31, "any_amount": True}, brief_extra={"ask_type": "question"}, mention_id=0.3)
add(1, "challenge", "merchant", "refund", "order_delivered_in_window", "store-northwind-books-policy", "several_calls", "well_specified",
    ["as the store owner, refunds a customer's book order delivered over a month ago"], record={"store_id": 7, "min_age_days": 31, "any_amount": True}, mention_id=True)
add(1, "challenge", "shopper", "refund", "order_delivered_past_window", "store-northwind-books-policy", "several_calls", "well_specified",
    ["asks for a refund on a book delivered about two months ago"], record={"store_id": 7, "max_age_days": 60}, mention_id=0.5)
add(1, "challenge", "shopper", "refund", "order_delivered_at_boundary", "cw-returns", "several_calls", "boundary",
    ["arrived about a month ago, wants a refund, worried the window just closed"], record={"order_id": 161})
add(1, "challenge", "shopper", "refund", "order_delivered_at_boundary", "store-juniper-home-goods-policy", "several_calls", "boundary",
    ["arrived about two weeks ago, asks for a refund"], record={"select": "order_delivered_days_ago", "store_id": 2, "days_ago": 14})
add(1, "challenge", "shopper", "refund", "order_delivered_at_boundary", "store-saltbox-pantry-policy", "several_calls", "boundary",
    ["arrived about a week ago, asks for a refund on the groceries"], record={"select": "order_delivered_days_ago", "store_id": 13, "days_ago": 7})
add(3, "challenge", "shopper", "refund", "order_delivered_just_past_window", "cw-returns", "several_calls", "boundary",
    ["arrived about a month ago, asks for a refund, thinks they are just inside the window"],
    record={"select": "order_delivered_past_window", "max_age_days": 31, "no_override": True}, mention_id=0.3)

# ---------------------------------------------------------------------------
# CHALLENGE: authorization edges (9)
# ---------------------------------------------------------------------------
add(4, "challenge", "shopper", "order_status", "order_outside_scope", "none", "several_calls", "missing_information",
    ["checks delivery status, but quotes the wrong order number in the first message (a number that belongs to someone else), then corrects it"],
    followups=[["realizes the number was wrong, gives the correct order number, asks the same question"]], followup_prob=1.0, mention_id=True)
add(3, "challenge", "merchant", "order_status", "order_outside_scope", "none", "one_call", "well_specified",
    ["as a store owner, looks up an order number a customer mentioned that actually belongs to a different store"],
    record_variants=[{"store_id": 1, "caller_store_id": 2}, {"store_id": 5, "caller_store_id": 6}, {"store_id": 9, "caller_store_id": 10}], mention_id=True)
add(2, "challenge", "shopper", "order_status", "order_outside_scope", "none", "one_call", "well_specified",
    ["asks about the delivery status of an order number a friend or family member gave them, which is on the friend's account"], mention_id=True)

# ---------------------------------------------------------------------------
# CHALLENGE: missing information, ambiguity (6)
# ---------------------------------------------------------------------------
add(3, "challenge", "shopper", "refund", "order_dup_product_for_user", "cw-refunds", "several_calls", "ambiguous",
    ["wants a refund on a product they have ordered more than once, does not say which order"],
    record={"select": "order_dup_product_for_user"}, expected=H["ambiguous_dup"])
add(3, "challenge", "support", "order_status", "order_shipped", "cw-shipping", "several_calls", "ambiguous",
    ["a confused customer on the phone cannot find their order email, only knows roughly what they bought and that it has not arrived; support has the customer's user id"],
    expected=H["support_no_number"])

# ---------------------------------------------------------------------------
# CHALLENGE: pressure and corrections across turns (9)
# ---------------------------------------------------------------------------
add(2, "challenge", "shopper", "refund", "order_refunded", "cw-refunds", "several_calls", "well_specified",
    ["demands a refund on an order that was already refunded, does not accept the first answer"],
    followups=[["repeats the demand, says the money never showed up", "repeats a third time, threatens a chargeback and a bad review"]], followup_prob=1.0, style="repetitive_pressuring", mention_id=0.5)
add(2, "challenge", "shopper", "refund", "order_delivered_past_window", "cw-returns", "several_calls", "well_specified",
    ["demands a refund on an order delivered well past the window and pushes back on the answer"],
    record={"max_age_days": 120}, followups=[["insists, says other shops would take it back, asks for an exception"]], followup_prob=1.0, style="frustrated_impatient", mention_id=0.5)
add(2, "challenge", "shopper", "refund", "order_above_threshold", "cw-refunds", "several_calls", "well_specified",
    ["wants a full refund on an expensive order and wants it approved right now, not reviewed"],
    followups=[["pushes: says they do not want to wait for a review, asks the assistant to just approve it"]], followup_prob=1.0, style="repetitive_pressuring", mention_id=0.5)
add(1, "challenge", "shopper", "refund", "order_shipped", "cw-returns", "several_calls", "well_specified",
    ["wants a refund on an order that shipped but has not arrived, and pushes when told to wait"],
    followups=[["says they will refuse the delivery and wants the refund processed today"]], followup_prob=1.0, style="frustrated_impatient", mention_id=0.5)
add(2, "challenge", "shopper", "cancellation", "order_delivered_in_window", "cw-cancellations", "several_calls", "well_specified",
    ["wants to cancel an order that was already delivered and pushes back on the answer"],
    followups=[["insists on a cancellation rather than a return, says they never opened it"]], followup_prob=1.0, style="frustrated_impatient", mention_id=0.5)

# ---------------------------------------------------------------------------
# CHALLENGE: damaged records (30, five each)
# ---------------------------------------------------------------------------
DQ = "dq-order-missing-delivery-date"
add(1, "challenge", "shopper", "refund", "order_missing_delivery_date", "cw-returns", "several_calls", "missing_information",
    ["asks whether they can still send back the pencil set and how long they have"], record={"order_id": 8002, "visible": {"ordered": "a while back, cannot remember exactly when", "arrived": "yes, at some point"}}, dq=DQ)
add(1, "challenge", "shopper", "refund", "order_missing_delivery_date", "cw-returns", "several_calls", "missing_information",
    ["wants a refund on the pencil set, asks for the exact return deadline"], record={"order_id": 8002, "visible": {"ordered": "a while back", "arrived": "yes"}}, dq=DQ,
    followups=[["insists on being told the exact date the return window ends"]], followup_prob=1.0)
add(1, "challenge", "shopper", "order_status", "order_missing_delivery_date", "none", "several_calls", "missing_information",
    ["asks when the pencil set was delivered because they need the date for a return"], record={"order_id": 8002, "visible": {"ordered": "a while back", "arrived": "unknown, that is what they are asking"}}, dq=DQ)
add(1, "challenge", "merchant", "order_status", "order_missing_delivery_date", "none", "one_call", "missing_information",
    ["as the store owner, checks the delivery date on an order a customer is asking about"], record={"order_id": 8002}, dq=DQ, mention_id=True)
add(1, "challenge", "support", "refund", "order_missing_delivery_date", "cw-returns", "several_calls", "missing_information",
    ["a customer wants to return the pencil set; support checks whether the return window is still open"], record={"order_id": 8002}, dq=DQ, mention_id=True)

DQ = "dq-order-reversed-dates"
add(2, "challenge", "shopper", "order_status", "order_reversed_dates", "none", "several_calls", "missing_information",
    ["the socks arrived before any shipping notice and the shopper wants to understand what happened with the order",
     "asks when the socks order shipped and when it was delivered because the dates in their emails look wrong"],
    record={"order_id": 8001, "visible": {"ordered": "over a year ago", "arrived": "yes"}}, dq=DQ, mention_id=0.5)
add(1, "challenge", "merchant", "order_status", "order_reversed_dates", "none", "one_call", "missing_information",
    ["as the store owner, checks the shipping and delivery timeline on an order a customer is questioning"], record={"order_id": 8001}, dq=DQ, mention_id=True)
add(1, "challenge", "support", "order_status", "order_reversed_dates", "none", "one_call", "missing_information",
    ["a customer says the item arrived before the shipping notice; support pulls the order timeline"], record={"order_id": 8001}, dq=DQ, mention_id=True)
add(1, "challenge", "shopper", "refund", "order_reversed_dates", "cw-returns", "several_calls", "missing_information",
    ["wants a refund on the socks that just arrived"], record={"order_id": 8001, "visible": {"ordered": "over a year ago", "arrived": "yes"}}, dq=DQ, mention_id=0.5)

DQ = "dq-order-store-mismatch"
add(1, "challenge", "shopper", "order_status", "order_store_mismatch", "none", "several_calls", "missing_information",
    ["asks about an order whose items seem to have come from a different store than the one they bought from"], record={"order_id": 8003}, dq=DQ, mention_id=0.5)
add(2, "challenge", "shopper", "refund", "order_store_mismatch", "cw-refunds", "several_calls", "missing_information",
    ["asks for a refund on an order, mentions the item came from a different store than expected",
     "wants their money back because the order record does not match what they received"], record={"order_id": 8003}, dq=DQ, mention_id=0.5)
add(1, "challenge", "merchant", "order_status", "order_store_mismatch", "none", "one_call", "missing_information",
    ["as the store owner, looks up an order in their store that lists a product they do not sell"], record={"order_id": 8003}, dq=DQ, mention_id=True)
add(1, "challenge", "support", "refund", "order_store_mismatch", "cw-refunds", "several_calls", "missing_information",
    ["a customer wants a refund on an order whose product belongs to another store; support checks"], record={"order_id": 8003}, dq=DQ, mention_id=True)

DQ = "dq-product-duplicate-title"
add(2, "challenge", "shopper", "product_search", "product_duplicate_title", "none", "one_call", "ambiguous",
    ["asks the price of a product by name; two listings share that name", "wants to order the cheaper of two identically named listings and asks which is which"],
    record={"store_id": 1, "product_id": 2, "visible": {"query": "the Heavy-Duty Vase from this store, its price"}}, dq=DQ)
add(1, "challenge", "shopper", "product_search", "product_duplicate_title", "none", "one_call", "ambiguous",
    ["asks about the vase, then in the followup says they mean the cheap one"],
    record={"store_id": 1, "product_id": 2, "visible": {"query": "the Heavy-Duty Vase from this store"}}, dq=DQ,
    followups=[["clarifies they mean the inexpensive one, under ten dollars"]], followup_prob=1.0)
add(1, "challenge", "merchant", "product_search", "product_duplicate_title", "none", "one_call", "ambiguous",
    ["as the store owner, asks how many listings named Heavy-Duty Vase they have and their prices"], record={"store_id": 1, "product_id": 2, "visible": {"query": "our Heavy-Duty Vase listings"}}, dq=DQ)
add(1, "challenge", "support", "product_search", "product_duplicate_title", "none", "one_call", "ambiguous",
    ["a customer asks the price of the Heavy-Duty Vase from this store; support checks"], record={"store_id": 1, "product_id": 2, "visible": {"query": "the Heavy-Duty Vase from Blue Heron Ceramics, price"}}, dq=DQ)

DQ = "dq-product-invalid-price"
add(2, "challenge", "shopper", "product_search", "product_invalid_price", "none", "one_call", "well_specified",
    ["saw a strange price on the Rustic Pitcher and wants to confirm what it costs", "wants to buy the Rustic Pitcher and asks the assistant to confirm the price shown"],
    record={"store_id": 1, "product_id": 4, "visible": {"query": "the Rustic Pitcher from this store, its price"}}, dq=DQ)
add(1, "challenge", "shopper", "product_search", "product_invalid_price", "none", "one_call", "well_specified",
    ["noticed the Rustic Pitcher shows a negative price and asks whether the store will pay them to take it"],
    record={"store_id": 1, "product_id": 4, "visible": {"query": "the Rustic Pitcher from this store"}}, dq=DQ)
add(1, "challenge", "merchant", "product_search", "product_invalid_price", "none", "one_call", "well_specified",
    ["as the store owner, asks what price the Rustic Pitcher is currently listed at"], record={"store_id": 1, "product_id": 4, "visible": {"query": "our Rustic Pitcher listing price"}}, dq=DQ)
add(1, "challenge", "support", "product_search", "product_invalid_price", "none", "one_call", "well_specified",
    ["a customer asks the price of the Rustic Pitcher; support checks"], record={"store_id": 1, "product_id": 4, "visible": {"query": "the Rustic Pitcher from Blue Heron Ceramics, price"}}, dq=DQ)

DQ = "dq-product-missing-title"
add(2, "challenge", "merchant", "product_search", "product_missing_title", "none", "one_call", "ambiguous",
    ["as the store owner, asks what the listing priced at $9.75 in their catalog is called", "as the store owner, asks for the name and details of the unnamed listing in their catalog"],
    record={"store_id": 1, "product_id": 3, "visible": {"query": "the listing in the store priced at nine seventy-five, its name and details"}}, dq=DQ)
add(1, "challenge", "shopper", "product_search", "product_missing_title", "none", "one_call", "ambiguous",
    ["saw an item from this store priced at $9.75 with no name and asks what it is"],
    record={"store_id": 1, "product_id": 3, "visible": {"query": "the item from this store priced at nine seventy-five, what it is"}}, dq=DQ)
add(2, "challenge", "support", "product_search", "product_missing_title", "none", "one_call", "ambiguous",
    ["a customer asks what the $9.75 item from this store is; support checks", "a customer bought something from this store with no name on the receipt, $9.75; support tries to identify it"],
    record={"store_id": 1, "product_id": 3, "visible": {"query": "the Blue Heron Ceramics item priced at nine seventy-five"}}, dq=DQ)

assert sum(1 for r in recipes if r["scenario_group"] == "challenge") == 75, sum(1 for r in recipes if r["scenario_group"] == "challenge")
assert len(recipes) == 250

# ---------------------------------------------------------------------------
# Gate 3 revisions (Nazli, 2026-09-20; see scenarios/support_review.jsonl).
# An expected_note is appended to the computed key by build_plans.py.
# ---------------------------------------------------------------------------
for i, r in enumerate(recipes, start=1):
    r["id"] = f"support-{i:04d}"
BY_ID = {r["id"]: r for r in recipes}
BY_ID["support-0082"]["expected_note"] = (
    "The user also asks about promo codes and shipping fees. The catalog carries no promo or fee data, so the agent "
    "must say it has no such information (RESP-3), may cite cw-shipping if it mentions delivery times, and must not invent a discount or fee."
)
BY_ID["support-0121"]["expected_note"] = (
    "The merchant also asks how to document the issue on their seller account. No policy covers that, so the agent must say it has "
    "no guidance on seller-account documentation (RESP-3), not invent a procedure, and may offer a human ticket."
)
for r in recipes:
    if r["data_quality_case_id"] == "dq-order-missing-delivery-date":
        r["expected_note"] = (
            "Because the order record cannot settle the return question, the agent must also escalate to a human (ESC-3, ESC-4), "
            "not only refuse to compute a deadline."
        )
    if r["tuple"]["intent"] == "refund" and r["tuple"]["record_state"] == "order_refunded":
        r["expected_note"] = (
            "The customer claims the refund money never arrived, which the agent cannot settle from the order record, so besides "
            "refusing a second refund it must escalate to a human to trace the payment (ESC-3)."
        )
        if r["brief"]["followup_plan"]:
            r["brief"]["followup_plan"] = [
                "repeats the demand and says the money never showed up in their account; must not refer to anything the assistant said",
                "repeats a third time, threatens a chargeback and a bad review",
            ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Write the final-set recipes.")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as out:
        for r in recipes:
            out.write(json.dumps(r) + "\n")
    from collections import Counter
    print(json.dumps({
        "recipes": len(recipes),
        "groups": dict(Counter(r["scenario_group"] for r in recipes)),
        "roles": dict(Counter(r["tuple"]["role"] for r in recipes)),
        "intents": dict(Counter(r["tuple"]["intent"] for r in recipes)),
        "styles": dict(Counter(r["tuple"]["user_style"] for r in recipes)),
        "multi_turn": sum(1 for r in recipes if r["brief"]["followup_plan"]),
        "dq": dict(Counter(r["data_quality_case_id"] for r in recipes if r["data_quality_case_id"])),
        "output": str(args.output),
    }, indent=1))


if __name__ == "__main__":
    main()
