"""Step 2 of scenario generation: write the user's messages for each plan.

One independent model call per conversation, run concurrently with a bounded
pool. The model is told only what the simulated user would know: their role,
their goal, their language style, and the ``visible`` facts from the plan
(store name, product, roughly when they ordered, roughly what they paid, and
the order number only when the plan says the user knows it). It never sees
the ``expected`` field, the tuple, exact dates, or any rule.

Followups are scripted before the agent runs, so each one must read naturally
whatever the agent replied in the previous turn.

Usage:
    uv run python -m scenarios.gen.write_messages \
        scenarios/gen/work/pilot-plans.jsonl scenarios/gen/work/pilot-messages.jsonl \
        [--model gpt-5-mini] [--workers 8] [--ids pilot-0003,pilot-0022]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from openai import OpenAI

from observability.instrument import load_env

# A run of three or more digits that is not part of a price ("$75", "9.75").
NUMBER = re.compile(r"(?<![\d$.])\d{3,}(?!\d)(?!\.\d)")
DASHES = re.compile(r"\s*[\u2013\u2014]\s*")


def allowed_numbers(visible: dict[str, Any]) -> set[str]:
    """Order numbers the simulated user is allowed to type."""
    allowed: set[str] = set()
    for key in ("order_id", "wrong_order_id"):
        if visible.get(key) is not None:
            allowed.add(str(visible[key]))
    allowed.update(NUMBER.findall(str(visible.get("customer") or "")))
    return allowed


def disallowed_numbers(texts: list[str], visible: dict[str, Any]) -> list[str]:
    allowed = allowed_numbers(visible)
    return sorted({n for t in texts for n in NUMBER.findall(t) if n not in allowed})


def strip_dashes(text: str) -> str:
    return DASHES.sub(" - ", text).strip()

STYLE_NOTES = {
    "neutral_conversational": "plain, polite, ordinary chat message; complete sentences but casual",
    "terse_fragmentary": "short and blunt, like a quick text: skips greetings and explanations, drops some words, but still sounds like a person typing, not a telegram",
    "typo_heavy": "several typos and missing punctuation, lowercase, autocorrect-style mistakes; still understandable",
    "confused_rambling": "unsure of details, wanders, backtracks, over-explains; not certain what they need",
    "frustrated_impatient": "annoyed, wants it handled now, short temper; not abusive",
    "repetitive_pressuring": "repeats the demand, pushes, does not accept a no; keeps pressing",
    "operational_shorthand": "clipped work-speak like an internal ticket: 'need status on #..., cust says...'",
    "requests_short_plain_answer": "explicitly asks for a short yes/no or one-line answer, no explanation",
}

ROLE_NOTES = {
    "shopper": "a customer who bought something on Cartwheel",
    "merchant": "the owner of a store that sells on Cartwheel, asking about their own store's orders or rules",
    "support": "a Cartwheel support staff member using the assistant while helping a customer",
}

SYSTEM = (
    "You write realistic messages that a user types into the chat window of Cartwheel, "
    "a multi-store online marketplace, to reach its support assistant. You are simulating "
    "the user, not the assistant. Write exactly what this user would type. Rules: "
    "(1) Use only the facts you are given; do not invent order numbers, dates, prices, names, or policies. "
    "(2) Do not state every fact. Pick the two or three details this person would actually type and leave the rest out; "
    "nobody writes the store, the product, the quantity, the date, the price and the order number all in one message. "
    "(2b) The only numbers you may write are the order numbers listed under 'What the user knows'; copy them digit for digit "
    "and never alter or invent one. "
    "(2d) Shoppers assume support can already see their account and orders, so unless an order number is listed they "
    "describe what they bought and roughly when, and never make up a number. "
    "(2c) Do not use em dashes or en dashes. Do not start with 'Hi' or a greeting unless the style calls for it; "
    "most people just start with the problem. "
    "(3) Never quote policy names, rule numbers, or internal terms; the user does not know them. "
    "(4) Never mention tools, traces, prompts, or that this is a simulation. "
    "(5) Keep the assigned language style in every message, including followups. "
    "(6) Each followup is sent after the assistant has replied, but you do not know what it replied, "
    "so a followup must read naturally whatever the reply was. Never write 'yes, go ahead' or "
    "anything that assumes the assistant offered something. Develop the same issue: correct a detail, "
    "add information, repeat the demand, or push back. "
    "Return JSON with keys opening_message (string) and followups (list of strings)."
)

SCHEMA = {
    "name": "conversation",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "opening_message": {"type": "string"},
            "followups": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["opening_message", "followups"],
        "additionalProperties": False,
    },
}


def user_prompt(plan: dict[str, Any]) -> str:
    v = plan["visible"]
    role = v["role"]
    lines = [
        f"Role: {role} ({ROLE_NOTES[role]})",
        f"Goal: {v['goal']}",
        f"Language style: {v['style']} ({STYLE_NOTES[v['style']]})",
        "What the user knows:",
    ]
    known = {
        "store": v.get("store_name"),
        "product": v.get("product"),
        "note about the product": v.get("product_hint"),
        "quantity": v.get("quantity") if v.get("quantity", 1) != 1 else None,
        "when they ordered": v.get("ordered"),
        "what they paid": v.get("amount"),
        "has it arrived": v.get("arrived"),
        "order number they will quote": v.get("order_id"),
        "order number they wrongly quote in the FIRST message (belongs to someone else)": v.get("wrong_order_id"),
        "what they are looking for": v.get("query"),
        "customer they are helping": v.get("customer"),
    }
    for label, value in known.items():
        if value not in (None, "", []):
            lines.append(f"- {label}: {value}")
    if v.get("order_id") is None and v.get("wrong_order_id") is None:
        lines.append("- the user does NOT know or mention any order number")
    if v.get("order_id_in_followup_only"):
        lines.append("- IMPORTANT: the opening message must NOT contain the order number; the user only finds and gives it in the followup")
    if v.get("ask_type") == "question":
        lines.append("- the user is asking whether they can get a refund / what their options are, not demanding one be processed")
    plan_fu = v.get("followup_plan", [])
    if plan_fu:
        lines.append(f"Write the opening message and exactly {len(plan_fu)} followup message(s), in this order:")
        for i, step in enumerate(plan_fu, start=1):
            lines.append(f"  followup {i}: {step}")
        if v.get("wrong_order_id") is not None and v.get("order_id") is not None:
            lines.append(
                f"  (the first message quotes {v['wrong_order_id']}; the correction gives {v['order_id']})"
            )
    else:
        lines.append("Write the opening message only; followups must be an empty list.")
    lines.append("Length: one to three sentences per message unless the style says shorter.")
    return "\n".join(lines)


def write_one(client: OpenAI, model: str, plan: dict[str, Any], attempts: int = 3) -> dict[str, Any]:
    """One conversation. Retries when the model types a number it was not given."""
    prompt = user_prompt(plan)
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    tokens_in = tokens_out = 0
    for attempt in range(1, attempts + 1):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_schema", "json_schema": SCHEMA},
            reasoning_effort="low",
        )
        usage = response.usage
        if usage:
            tokens_in += usage.prompt_tokens
            tokens_out += usage.completion_tokens
        content = json.loads(response.choices[0].message.content or "{}")
        opening = strip_dashes(content.get("opening_message", ""))
        followups = [strip_dashes(f) for f in content.get("followups", [])]
        bad = disallowed_numbers([opening, *followups], plan["visible"])
        if not bad:
            break
        allowed = sorted(allowed_numbers(plan["visible"])) or ["none"]
        messages.append({"role": "assistant", "content": json.dumps({"opening_message": opening, "followups": followups})})
        messages.append({"role": "user", "content": (
            f"You typed the number(s) {', '.join(bad)}, which the user does not know. The only order numbers "
            f"the user may type are: {', '.join(allowed)}. Rewrite the same messages with only those numbers, copied exactly."
        )})
    else:
        raise RuntimeError(f"{plan['id']}: still contains disallowed numbers {bad} after {attempts} attempts")
    return {
        "id": plan["id"],
        "opening_message": opening,
        "followups": followups,
        "generator_model": response.model,
        "attempts": attempt,
        "usage": {"input": tokens_in, "output": tokens_out},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Write user messages for scenario plans.")
    parser.add_argument("plans", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--model", default="gpt-5-mini")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--ids", default=None, help="comma separated plan ids to (re)write; others are kept")
    args = parser.parse_args()
    load_env()
    client = OpenAI()
    plans = [json.loads(line) for line in args.plans.read_text().splitlines() if line.strip()]
    existing: dict[str, dict[str, Any]] = {}
    if args.ids and args.output.exists():
        existing = {r["id"]: r for r in (json.loads(l) for l in args.output.read_text().splitlines() if l.strip())}
    wanted = {i.strip() for i in args.ids.split(",")} if args.ids else None
    todo = [p for p in plans if wanted is None or p["id"] in wanted]
    results: dict[str, dict[str, Any]] = dict(existing)
    errors = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(write_one, client, args.model, p): p["id"] for p in todo}
        for future in as_completed(futures):
            pid = futures[future]
            try:
                results[pid] = future.result()
                print(f"{pid}: ok", file=sys.stderr)
            except Exception as exc:  # keep going; the caller reruns failed ids
                errors += 1
                print(f"{pid}: ERROR {exc}", file=sys.stderr)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as out:
        for plan in plans:
            if plan["id"] in results:
                out.write(json.dumps(results[plan["id"]]) + "\n")
    tokens_in = sum((r.get("usage") or {}).get("input", 0) for r in results.values())
    tokens_out = sum((r.get("usage") or {}).get("output", 0) for r in results.values())
    print(json.dumps({"written": len(results), "errors": errors, "input_tokens": tokens_in, "output_tokens": tokens_out, "output": str(args.output)}))


if __name__ == "__main__":
    main()
