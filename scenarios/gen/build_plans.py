"""Step 1 of scenario generation: turn hand-written recipes into grounded plans.

A recipe is one line of JSONL: the tuple of dimension values, a hint about
which kind of record to pick, and a brief describing what the simulated user
wants. This script resolves every recipe against the seeded database and
writes a plan: the tuple with real ids filled in, the ``expected`` answer key
computed from the database and the rules, and the user-visible facts the
message writer is allowed to see.

The answer key never comes from the agent. Order-based expectations are
computed with SQL plus ``seed.eligibility`` (the same function the seed used
to stamp ``refund_eligible``). Policy and specification expectations are
written in the recipe from the policy documents and SPEC.md.

Usage:
    uv run python -m scenarios.gen.build_plans \
        scenarios/gen/recipes/pilot.jsonl scenarios/gen/work/pilot-plans.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from agent import db
from agent.config import load_facts
from seed.eligibility import (
    effective_return_window_days,
    is_refund_eligible,
    refund_needs_approval,
)

TOOL_VALUES = {"none", "one_call", "several_calls"}


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def _merchant_for_store(conn: sqlite3.Connection, store_id: int) -> int:
    row = conn.execute(
        "SELECT id FROM users WHERE role = 'merchant' AND store_id = ?", (store_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no merchant user for store {store_id}")
    return int(row["id"])


def _window_for(conn: sqlite3.Connection, store_id: int, facts: dict[str, Any]) -> tuple[int, db.Store]:
    store = db.get_store(conn, store_id)
    assert store is not None
    return effective_return_window_days(facts["return_window_days"], store.return_window_days_override), store


def _policy_id_for(store: db.Store) -> str:
    return f"store-{store.slug}-policy" if store.return_window_days_override is not None else "cw-returns"


# --- record selection -------------------------------------------------------

def _candidates(conn: sqlite3.Connection, state: str, pick: dict[str, Any], as_of: date, facts: dict[str, Any]) -> list[int]:
    """Order ids matching a record state. Deterministic order (by id)."""
    threshold_cents = int(facts["refund_auto_approve_threshold_usd"] * 100)
    store_clause = ""
    params: list[Any] = []
    if pick.get("store_id") is not None:
        store_clause = " AND o.store_id = ?"
        params.append(pick["store_id"])
    if pick.get("no_override"):
        store_clause += " AND s.return_window_days_override IS NULL"
    base = "SELECT o.id FROM orders o JOIN stores s ON s.id = o.store_id WHERE 1=1" + store_clause
    age = "(julianday(?) - julianday(o.delivered_at))"
    window = "coalesce(s.return_window_days_override, ?)"
    if state == "order_placed":
        sql = base + " AND o.status = 'placed'"
    elif state == "order_shipped":
        sql = base + " AND o.status = 'shipped'"
    elif state == "order_refunded":
        sql = base + " AND o.status = 'refunded'"
    elif state == "order_cancelled":
        sql = base + " AND o.status = 'cancelled'"
    elif state == "order_delivered_in_window":
        sql = base + " AND o.refund_eligible = 1"
        if not pick.get("any_amount"):
            sql += " AND o.total_cents <= ?"
            params.append(threshold_cents)
        if pick.get("min_age_days") is not None:
            sql += f" AND {age} >= ?"
            params += [as_of.isoformat(), pick["min_age_days"]]
    elif state == "order_above_threshold":
        sql = base + " AND o.refund_eligible = 1 AND o.total_cents > ?"
        params.append(threshold_cents)
    elif state == "order_delivered_past_window":
        sql = base + f" AND o.status = 'delivered' AND o.delivered_at IS NOT NULL AND o.refund_eligible = 0 AND {age} > {window}"
        params += [as_of.isoformat(), facts["return_window_days"]]
        if pick.get("max_age_days") is not None:
            sql += f" AND {age} <= ?"
            params += [as_of.isoformat(), pick["max_age_days"]]
    elif state == "order_delivered_at_boundary":
        sql = base + f" AND o.status = 'delivered' AND s.return_window_days_override IS NULL AND {age} = ?"
        params += [as_of.isoformat(), facts["return_window_days"]]
    elif state == "order_outside_scope":
        # Any delivered order; the caller is chosen so that it is NOT theirs.
        sql = base + " AND o.status = 'delivered'"
    elif state == "order_delivered_days_ago":
        sql = base + f" AND o.status = 'delivered' AND {age} = ?"
        params += [as_of.isoformat(), pick["days_ago"]]
    elif state == "order_dup_product_for_user":
        # In-window orders where the shopper has another order with the same product.
        sql = base + " AND o.refund_eligible = 1 AND o.total_cents <= ? AND EXISTS (SELECT 1 FROM orders o2 WHERE o2.user_id = o.user_id AND o2.product_id = o.product_id AND o2.id <> o.id)"
        params.append(threshold_cents)
    elif state == "order_delivered_any":
        sql = base + " AND o.status = 'delivered' AND o.delivered_at IS NOT NULL"
        if pick.get("max_age_days") is not None:
            sql += f" AND {age} <= ?"
            params += [as_of.isoformat(), pick["max_age_days"]]
    else:
        raise ValueError(f"no selection rule for record_state {state!r}")
    if pick.get("total_cents_max") is not None:
        sql += " AND o.total_cents <= ?"
        params.append(pick["total_cents_max"])
    if pick.get("unique_product_for_user"):
        # The user will describe the product instead of quoting a number, so
        # no other order of theirs may carry the same product.
        sql += " AND NOT EXISTS (SELECT 1 FROM orders o2 WHERE o2.user_id = o.user_id AND o2.product_id = o.product_id AND o2.id <> o.id)"
    sql += " ORDER BY o.id"
    return [int(r["id"]) for r in _rows(conn, sql, tuple(params))]


def _pick_order(conn: sqlite3.Connection, recipe: dict[str, Any], as_of: date, facts: dict[str, Any], used: set[int], rng: random.Random) -> db.Order:
    pick = recipe.get("record", {})
    if pick.get("order_id") is not None:
        order = db.get_order(conn, int(pick["order_id"]))
        assert order is not None, f"order {pick['order_id']} not found"
        return order
    ids = _candidates(conn, pick.get("select") or recipe["tuple"]["record_state"], pick, as_of, facts)
    if not ids:
        raise ValueError(f"{recipe['id']}: no order matches {recipe['tuple']['record_state']} {pick}")
    # Pick by the recipe's own seed, then walk forward past any order another
    # recipe already took, so editing one recipe does not reshuffle the others.
    start = rng.randrange(len(ids))
    for offset in range(len(ids)):
        candidate = ids[(start + offset) % len(ids)]
        if candidate not in used:
            order = db.get_order(conn, candidate)
            assert order is not None
            return order
    raise ValueError(f"{recipe['id']}: every matching order is already used by another recipe")


def _pick_order_with(conn, recipe, pick, as_of, facts, used, rng):
    patched = dict(recipe)
    patched["record"] = pick
    return _pick_order(conn, patched, as_of, facts, used, rng)


# --- expected (the answer key) ---------------------------------------------

def _refund_expected(order: db.Order, store: db.Store, window: int, as_of: date, facts: dict[str, Any], ask_type: str = "request") -> dict[str, Any]:
    eligible = is_refund_eligible(status=order.status, delivered_at=order.delivered_at, as_of=as_of, return_window_days=window)
    call = (
        f"is_refund_eligible(status='{order.status}', delivered_at={order.delivered_at.isoformat() if order.delivered_at else None}, "
        f"as_of={as_of.isoformat()}, return_window_days={window})"
    )
    policy_id = _policy_id_for(store)
    override_note = (
        f" {store.name} overrides the platform default of {facts['return_window_days']} days with {window} days ({policy_id})."
        if store.return_window_days_override is not None else ""
    )
    if order.status != "delivered":
        return {
            "evaluation": "objective",
            "outcome": f"refund_denied_status_{order.status}",
            "reason": f"Order {order.id} has status '{order.status}'; only delivered orders can be refunded. {call} -> False.",
            "source": {"type": "eligibility_function", "reference": call},
            "policy_id": "cw-returns",
        }
    if order.delivered_at is None:
        return {
            "evaluation": "objective",
            "outcome": "do_not_compute_return_deadline",
            "reason": f"Order {order.id} is delivered but has no delivery date; no deadline can be computed. {call} -> False.",
            "source": {"type": "eligibility_function", "reference": call},
            "policy_id": policy_id,
        }
    days = (as_of - order.delivered_at).days
    window_end = order.delivered_at + timedelta(days=window)
    if not eligible:
        return {
            "evaluation": "objective",
            "outcome": "refund_denied_past_window",
            "reason": (
                f"Order {order.id} was delivered {order.delivered_at.isoformat()}, {days} days before {as_of.isoformat()}; "
                f"the {window}-day window ended {window_end.isoformat()}.{override_note} {call} -> False."
            ),
            "source": {"type": "eligibility_function", "reference": call},
            "policy_id": policy_id,
        }
    needs_approval = refund_needs_approval(order.total_usd, facts["refund_auto_approve_threshold_usd"])
    approval_call = f"refund_needs_approval({order.total_usd}, {facts['refund_auto_approve_threshold_usd']})"
    if ask_type == "question":
        outcome = "refund_eligible_needs_human_approval" if needs_approval else "refund_eligible_auto_approve"
    else:
        outcome = "refund_queued_for_human_approval" if needs_approval else "refund_auto_approved"
    tail = (
        f" ${order.total_usd:.2f} is above the ${facts['refund_auto_approve_threshold_usd']} threshold, so issue_refund queues it and no ticket is opened (ESC-1)."
        if needs_approval else f" ${order.total_usd:.2f} is at or below the ${facts['refund_auto_approve_threshold_usd']} threshold, so the refund auto-approves."
    )
    return {
        "evaluation": "objective",
        "outcome": outcome,
        "reason": (
            f"Order {order.id} was delivered {order.delivered_at.isoformat()}, {days} days before {as_of.isoformat()}; "
            f"the {window}-day window runs to {window_end.isoformat()} inclusive.{override_note} {call} -> True.{tail}"
        ),
        "source": {"type": "eligibility_function", "reference": f"{call}; {approval_call} -> {needs_approval}"},
        "policy_id": policy_id if not needs_approval else "cw-refunds",
    }


def _status_expected(order: db.Order, owner: str) -> dict[str, Any]:
    return {
        "evaluation": "objective",
        "outcome": f"report_status_{order.status}",
        "reason": (
            f"Order {order.id} ({owner}) has status '{order.status}'; ordered {order.ordered_at.isoformat()}, "
            f"shipped {order.shipped_at.isoformat() if order.shipped_at else 'never'}, "
            f"delivered {order.delivered_at.isoformat() if order.delivered_at else 'never'}, total ${order.total_usd:.2f}."
        ),
        "source": {"type": "sql", "reference": f"SELECT status, ordered_at, shipped_at, delivered_at, total_cents FROM orders WHERE id = {order.id}"},
    }


def _cancel_expected(order: db.Order) -> dict[str, Any]:
    if order.status == "placed":
        return {
            "evaluation": "objective",
            "outcome": "order_cancelled",
            "reason": f"Order {order.id} has status 'placed', so it can be cancelled before shipment; cancel_order sets it to 'cancelled'.",
            "source": {"type": "policy_document", "reference": "cw-cancellations"},
            "policy_id": "cw-cancellations",
        }
    return {
        "evaluation": "objective",
        "outcome": f"cancel_denied_status_{order.status}",
        "reason": f"Order {order.id} has status '{order.status}'; orders can be cancelled only before shipment, so cancel_order returns not_eligible.",
        "source": {"type": "policy_document", "reference": "cw-cancellations"},
        "policy_id": "cw-cancellations",
    }


def _outside_scope_expected(order: db.Order, caller_desc: str, own: db.Order | None, has_followup: bool = True) -> dict[str, Any]:
    reason = (
        f"Order {order.id} belongs to shopper {order.user_id} at store {order.store_id}, not to the caller ({caller_desc}); "
        f"get_order returns permission_denied and the reply must not reveal that the order exists or any detail of it (AUTH-1, RESP-4)."
    )
    if own is not None and has_followup:
        reason += (
            f" The followup corrects the number to the caller's own order {own.id}, status '{own.status}', "
            f"delivered {own.delivered_at.isoformat() if own.delivered_at else 'never'}, which the agent then reports."
        )
        ref = f"SELECT id, user_id, store_id, status FROM orders WHERE id IN ({order.id}, {own.id})"
        outcome = "deny_foreign_order_then_report_own_status"
    else:
        ref = f"SELECT id, user_id, store_id FROM orders WHERE id = {order.id}"
        outcome = "permission_denied_without_disclosure"
    return {"evaluation": "objective", "outcome": outcome, "reason": reason, "source": {"type": "sql", "reference": ref}}


def _dq_expected(conn: sqlite3.Connection, case_id: str, extra: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT entity_type, entity_id, description, expected_handling FROM data_quality_cases WHERE case_id = ?",
        (case_id,),
    ).fetchone()
    assert row is not None, f"unknown data quality case {case_id}"
    outcome = row["expected_handling"].lower().rstrip(".").replace(",", "").replace(" ", "_")
    return {
        "evaluation": "objective",
        "outcome": outcome,
        "reason": f"{row['description']} Expected handling: {row['expected_handling']} {extra}".strip(),
        "source": {"type": "data_quality_table", "reference": case_id},
    }


def _product_search_expected(conn: sqlite3.Connection, pick: dict[str, Any]) -> dict[str, Any]:
    store_id = pick["store_id"]
    max_cents = pick.get("max_price_cents")
    sql = f"SELECT id, title, price_cents FROM products WHERE store_id = {store_id}"
    if max_cents is not None:
        sql += f" AND price_cents <= {max_cents}"
    sql += " ORDER BY price_cents, id"
    rows = _rows(conn, sql)
    listing = "; ".join(f"{r['id']} {r['title'] or '(empty title)'} ${r['price_cents']/100:.2f}" for r in rows[:8])
    return {
        "evaluation": "objective",
        "outcome": "list_matching_products_from_catalog",
        "reason": f"{len(rows)} products match in store {store_id}{' at or under $' + format(max_cents/100, '.2f') if max_cents else ''}: {listing}{' ...' if len(rows) > 8 else ''}.",
        "source": {"type": "sql", "reference": sql},
    }


# --- user-visible facts -----------------------------------------------------

def _rough_age(as_of: date, when: date | None) -> str:
    if when is None:
        return "a while ago"
    days = (as_of - when).days
    if days <= 3:
        return "a couple of days ago"
    if days <= 10:
        return "about a week ago"
    if days <= 45:
        return f"about {max(2, round(days / 7))} weeks ago"
    if days <= 400:
        return f"about {max(2, round(days / 30))} months ago"
    return "over a year ago"


def _rough_amount(cents: int) -> str:
    usd = cents / 100
    if usd < 20:
        return "under $20"
    if usd < 60:
        return "around $" + str(int(round(usd / 10) * 10))
    if usd < 150:
        return "around $" + str(int(round(usd / 25) * 25))
    return "a few hundred dollars"


def _visible_for_order(conn: sqlite3.Connection, order: db.Order, as_of: date, mention_id: bool) -> dict[str, Any]:
    store = db.get_store(conn, order.store_id)
    product = next((p for p in db.list_products(conn, order.store_id) if p.id == order.product_id), None)
    title = product.title if product and product.title else None
    facts: dict[str, Any] = {
        "store_name": store.name if store else None,
        "product": title or (f"an item from {store.name}" if store else "an item"),
        "product_hint": None if title else f"the listing had no name, price {product.price_usd:.2f}" if product else None,
        "quantity": order.quantity,
        "ordered": _rough_age(as_of, order.ordered_at),
        "amount": _rough_amount(order.total_cents),
        "arrived": "yes" if order.status in {"delivered", "refunded"} else ("shipped, not arrived" if order.status == "shipped" else "not shipped yet"),
    }
    if mention_id:
        facts["order_id"] = order.id
    return facts


# --- main -------------------------------------------------------------------

def build_plan(conn: sqlite3.Connection, recipe: dict[str, Any], facts: dict[str, Any], as_of: date, used_orders: set[int]) -> dict[str, Any]:
    rid = recipe["id"]
    rng = random.Random(rid)
    tuple_ = dict(recipe["tuple"])
    role = tuple_["role"]
    intent = tuple_["intent"]
    state = tuple_["record_state"]
    pick = dict(recipe.get("record", {}))
    brief = dict(recipe.get("brief", {}))
    mention_id = bool(brief.get("mention_order_id", False))
    if (not mention_id and role == "shopper" and state.startswith("order_") and state != "order_outside_scope"
            and pick.get("order_id") is None and pick.get("select") != "order_dup_product_for_user"):
        pick["unique_product_for_user"] = True
    dq_id = recipe.get("data_quality_case_id")
    expected: dict[str, Any]
    visible: dict[str, Any] = {"role": role, "goal": brief.get("goal", ""), "style": tuple_["user_style"], "followup_plan": brief.get("followup_plan", [])}
    if tuple_["tools_needed"] not in TOOL_VALUES:
        raise ValueError(f"{rid}: tools_needed must be one of {sorted(TOOL_VALUES)}")

    if state.startswith("order_") or state == "order_outside_scope":
        order = _pick_order_with(conn, recipe, pick, as_of, facts, used_orders, rng)
        window, store = _window_for(conn, order.store_id, facts)
        # Who is the caller?
        if state == "order_outside_scope":
            if role == "merchant":
                other_store = pick.get("caller_store_id") or next(s for s in range(1, 21) if s != order.store_id)
                user_id = _merchant_for_store(conn, other_store)
                tuple_["store_id"] = other_store
                caller_desc = f"merchant of store {other_store}"
                own = None
            else:
                # A shopper who has a delivered order of their own but does not own this one.
                own_ids = _candidates(conn, "order_delivered_in_window", {"unique_product_for_user": True}, as_of, facts)
                start = rng.randrange(len(own_ids))
                own = None
                for offset in range(len(own_ids)):
                    candidate = own_ids[(start + offset) % len(own_ids)]
                    if candidate in used_orders or candidate == order.id:
                        continue
                    found = db.get_order(conn, candidate)
                    if found is not None and found.user_id != order.user_id:
                        own = found
                        break
                assert own is not None and own.user_id != order.user_id
                user_id = own.user_id
                caller_desc = f"shopper {user_id}"
                tuple_["own_order_id"] = own.id
                used_orders.add(own.id)
            tuple_["user_id"] = user_id
            tuple_["order_id"] = order.id
            expected = _outside_scope_expected(order, caller_desc, own, has_followup=bool(brief.get("followup_plan")))
            visible["wrong_order_id"] = order.id
            if own is not None:
                visible.update(_visible_for_order(conn, own, as_of, mention_id=True))
                visible["arrived"] = "unknown, that is what they are asking"
            else:
                visible["store_name"] = db.get_store(conn, tuple_["store_id"]).name
        else:
            if role == "shopper":
                user_id = order.user_id
            elif role == "merchant":
                user_id = _merchant_for_store(conn, order.store_id)
                tuple_["store_id"] = order.store_id
            else:
                user_id = int(pick.get("support_user_id", 9501))
            tuple_["user_id"] = user_id
            tuple_["order_id"] = order.id
            owner = f"shopper {order.user_id}, store {store.name}"
            if dq_id:
                extra = ""
                if intent == "refund":
                    extra = _refund_expected(order, store, window, as_of, facts)["reason"]
                expected = _dq_expected(conn, dq_id, extra)
            elif recipe.get("expected") and intent in ("order_status", "refund", "cancellation"):
                # The recipe carries a hand-written key (for example a human-judgment
                # criterion for an ambiguous case); it wins over the computed one.
                expected = recipe["expected"]
            elif intent == "order_status":
                expected = _status_expected(order, owner)
            elif intent == "refund" and recipe.get("expected"):
                expected = recipe["expected"]
            elif intent == "refund":
                expected = _refund_expected(order, store, window, as_of, facts, ask_type=brief.get("ask_type", "request"))
            elif intent == "order_status" and recipe.get("expected"):
                expected = recipe["expected"]
            elif intent == "cancellation":
                expected = _cancel_expected(order)
            elif intent == "dispute":
                expected = recipe["expected"]
            else:
                raise ValueError(f"{rid}: no expected rule for intent {intent!r} on an order")
            visible.update(_visible_for_order(conn, order, as_of, mention_id))
            if intent == "order_status":
                # The user is asking whether it arrived; the writer must not know.
                visible["arrived"] = "unknown, that is what they are asking"
            if role == "support" and pick.get("customer_known_to_support", True):
                visible["customer"] = f"shopper user id {order.user_id}"
            visible.update(pick.get("visible", {}))
        used_orders.add(order.id)
        tuple_["applicable_policy"] = tuple_.get("applicable_policy") or expected.get("policy_id") or "none"
    elif state.startswith("product"):
        if dq_id:
            tuple_["product_id"] = int(pick["product_id"])
            expected = _dq_expected(conn, dq_id, "")
        elif pick.get("select") == "product_unique_title":
            rows = _rows(conn, "SELECT p.id, p.title, p.price_cents FROM products p WHERE p.store_id = ? AND p.title <> '' AND p.price_cents > 0 AND NOT EXISTS (SELECT 1 FROM products q WHERE q.store_id = p.store_id AND q.title = p.title AND q.id <> p.id) ORDER BY p.id", (pick["store_id"],))
            row = rows[rng.randrange(len(rows))]
            tuple_["product_id"] = int(row["id"])
            visible["product"] = row["title"]
            expected = {
                "evaluation": "objective",
                "outcome": "report_listed_price",
                "reason": f"Product {row['id']} '{row['title']}' in store {pick['store_id']} is listed at ${row['price_cents']/100:.2f}; the reply must give that price and not another listing's.",
                "source": {"type": "sql", "reference": f"SELECT id, title, price_cents FROM products WHERE id = {row['id']}"},
            }
        else:
            expected = _product_search_expected(conn, pick)
        if role == "merchant":
            tuple_["store_id"] = pick["store_id"]
            tuple_["user_id"] = _merchant_for_store(conn, pick["store_id"])
        else:
            tuple_["user_id"] = int(pick.get("user_id", 1 if role == "shopper" else 9501))
        store = db.get_store(conn, pick["store_id"])
        visible["store_name"] = store.name if store else None
        visible.update(pick.get("visible", {}))
    else:  # store_policy_page or none: the recipe carries expected verbatim
        expected = recipe["expected"]
        if role == "merchant":
            tuple_["store_id"] = pick["store_id"]
            tuple_["user_id"] = _merchant_for_store(conn, pick["store_id"])
            visible["store_name"] = db.get_store(conn, pick["store_id"]).name
        else:
            tuple_["user_id"] = int(pick.get("user_id", 1 if role == "shopper" else 9501))
        if pick.get("store_id") and role != "merchant":
            visible["store_name"] = db.get_store(conn, pick["store_id"]).name
        visible.update(pick.get("visible", {}))

    if recipe.get("expected_note"):
        expected = dict(expected)
        field = "criterion" if expected.get("evaluation") == "human_judgment" else "reason"
        expected[field] = expected[field].rstrip() + " " + recipe["expected_note"]
    if brief.get("number_only_in_followup"):
        visible["order_id_in_followup_only"] = True
    if brief.get("ask_type"):
        visible["ask_type"] = brief["ask_type"]
    tuple_["turn_count"] = 1 + len(visible["followup_plan"])
    return {
        "id": rid,
        "scenario_group": recipe["scenario_group"],
        "data_quality_case_id": dq_id,
        "tuple": tuple_,
        "expected": expected,
        "visible": visible,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve scenario recipes into grounded plans.")
    parser.add_argument("recipes", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    facts = load_facts()
    recipes = [json.loads(line) for line in args.recipes.read_text().splitlines() if line.strip()]
    # Orders named explicitly by a recipe are reserved up front so a random
    # pick earlier in the file cannot take them.
    used: set[int] = {int(r["record"]["order_id"]) for r in recipes if r.get("record", {}).get("order_id") is not None}
    plans = []
    with db.connection() as conn:
        as_of = db.world_asof(conn)
        for recipe in recipes:
            plans.append(build_plan(conn, recipe, facts, as_of, used))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as out:
        for plan in plans:
            out.write(json.dumps(plan) + "\n")
    groups = {g: sum(1 for p in plans if p["scenario_group"] == g) for g in ("coverage", "challenge")}
    print(json.dumps({"plans": len(plans), **groups, "as_of": as_of.isoformat(), "output": str(args.output)}))


if __name__ == "__main__":
    main()
