"""Step 3 of scenario generation: an independent critic pass over each conversation.

For every conversation, one model call checks the generated messages against
the plan's user-visible facts and the scenario skill's rules:

- invented identifiers, amounts, dates or names that do not match the facts
- followups that assume a specific assistant reply
- openings that share a template with other conversations
- language a real user would not produce (policy names, rule numbers)
- references to tools, traces, prompts or the simulation

Two deterministic checks run first and are handed to the critic as known
problems: any number of three or more digits that is not an allowed order
number, and an opening whose first four words appear in other conversations.
The critic may rewrite wording but must keep the goal, the facts, the number
of followups and the assigned style. It never sees the expected field.

Usage:
    uv run python -m scenarios.gen.critic \
        scenarios/gen/work/pilot-plans.jsonl scenarios/gen/work/pilot-messages.jsonl \
        scenarios/gen/work/pilot-critic.jsonl [--model gpt-5-mini] [--workers 8]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from openai import OpenAI

from observability.instrument import load_env
from scenarios.gen.write_messages import ROLE_NOTES, STYLE_NOTES, allowed_numbers, disallowed_numbers, strip_dashes

SYSTEM = (
    "You are reviewing messages written to simulate a user of Cartwheel, a multi-store online "
    "marketplace, talking to its support assistant. You check the messages against the facts the "
    "simulated user is allowed to know and against these rules: "
    "(1) no invented order numbers, dates, prices, product or store names; the only numbers allowed are the "
    "listed order numbers, copied exactly; rough amounts like 'around $75' are fine; "
    "(2) each followup must read naturally whatever the assistant replied, so it must not assume the assistant "
    "offered, confirmed or asked anything specific; "
    "(3) no policy names, rule numbers, or internal terms a real user would not know; "
    "(4) no mention of tools, traces, prompts, or a simulation; "
    "(5) the assigned language style must hold in every message; "
    "(6) a real person does not recite every fact; if the message lists the store, product, quantity, date, price "
    "and order number all at once, trim it to what this person would type; "
    "(7) no em dashes or en dashes; "
    "(8) the opening must not share a template with other conversations. "
    "If everything is fine, return the messages unchanged with ok=true. Otherwise rewrite only what is needed, "
    "keep the goal, the allowed facts, the exact number of followups and the style, and list the problems you fixed. "
    "Return JSON with keys ok (boolean), problems (list of strings), opening_message (string), followups (list of strings)."
)

SCHEMA = {
    "name": "critique",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "problems": {"type": "array", "items": {"type": "string"}},
            "opening_message": {"type": "string"},
            "followups": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["ok", "problems", "opening_message", "followups"],
        "additionalProperties": False,
    },
}

DASHES = re.compile(r"[\u2013\u2014]")


def deterministic_problems(text_parts: list[str], visible: dict[str, Any], opening_prefix_count: int) -> list[str]:
    problems = []
    for number in disallowed_numbers(text_parts, visible):
        problems.append(f"number {number} is not an allowed order number (allowed: {sorted(allowed_numbers(visible)) or 'none'})")
    if any(DASHES.search(part) for part in text_parts):
        problems.append("contains an em dash or en dash")
    if opening_prefix_count > 1:
        problems.append(f"the first four words of the opening are shared with {opening_prefix_count - 1} other conversation(s)")
    return problems


def critique_one(client: OpenAI, model: str, plan: dict[str, Any], msg: dict[str, Any], known: list[str]) -> dict[str, Any]:
    v = plan["visible"]
    facts = {
        "role": f"{v['role']} ({ROLE_NOTES[v['role']]})",
        "goal": v.get("goal"),
        "style": f"{v['style']} ({STYLE_NOTES[v['style']]})",
        "store": v.get("store_name"),
        "product": v.get("product"),
        "product note": v.get("product_hint"),
        "quantity": v.get("quantity"),
        "when ordered": v.get("ordered"),
        "amount paid": v.get("amount"),
        "arrived": v.get("arrived"),
        "allowed order number": v.get("order_id"),
        "wrong order number quoted first (belongs to someone else)": v.get("wrong_order_id"),
        "what they look for": v.get("query"),
        "customer being helped": v.get("customer"),
        "followup plan": v.get("followup_plan", []),
    }
    facts = {k: val for k, val in facts.items() if val not in (None, "", [])}
    prompt = (
        "Facts the simulated user knows:\n" + json.dumps(facts, indent=1) + "\n\n"
        "Conversation to review:\n" + json.dumps({"opening_message": msg["opening_message"], "followups": msg["followups"]}, indent=1) + "\n\n"
        + ("Known problems from automatic checks:\n- " + "\n- ".join(known) + "\n" if known else "Automatic checks found nothing.\n")
    )
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        response_format={"type": "json_schema", "json_schema": SCHEMA},
        reasoning_effort="low",
    )
    content = json.loads(response.choices[0].message.content or "{}")
    followups = [strip_dashes(f) for f in content.get("followups", [])]
    if len(followups) != len(msg["followups"]):
        followups = msg["followups"]
        content.setdefault("problems", []).append("critic changed the followup count; original followups kept")
    opening = strip_dashes(content.get("opening_message") or msg["opening_message"])
    leftover = disallowed_numbers([opening, *followups], plan["visible"])
    if leftover:
        raise RuntimeError(f"{plan['id']}: critic left disallowed numbers {leftover}")
    usage = response.usage
    return {
        "id": plan["id"],
        "ok": bool(content.get("ok")) and not known,
        "problems": known + list(content.get("problems", [])),
        "opening_message": opening,
        "followups": followups,
        "changed": (opening != msg["opening_message"]) or (followups != msg["followups"]),
        "critic_model": response.model,
        "usage": {"input": usage.prompt_tokens, "output": usage.completion_tokens} if usage else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Critic pass over generated conversations.")
    parser.add_argument("plans", type=Path)
    parser.add_argument("messages", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--model", default="gpt-5-mini")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--ids", default=None, help="comma separated ids to re-review; others are kept from the existing output")
    args = parser.parse_args()
    load_env()
    client = OpenAI()
    plans = {p["id"]: p for p in (json.loads(l) for l in args.plans.read_text().splitlines() if l.strip())}
    messages = [json.loads(l) for l in args.messages.read_text().splitlines() if l.strip()]
    prefixes = Counter(" ".join(m["opening_message"].lower().split()[:4]) for m in messages)
    results: dict[str, dict[str, Any]] = {}
    wanted = {i.strip() for i in args.ids.split(",")} if args.ids else None
    if wanted and args.output.exists():
        results = {r["id"]: r for r in (json.loads(l) for l in args.output.read_text().splitlines() if l.strip()) if r["id"] not in wanted}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {}
        for m in messages:
            if wanted is not None and m["id"] not in wanted:
                continue
            plan = plans[m["id"]]
            known = deterministic_problems(
                [m["opening_message"], *m["followups"]],
                plan["visible"],
                prefixes[" ".join(m["opening_message"].lower().split()[:4])],
            )
            futures[pool.submit(critique_one, client, args.model, plan, m, known)] = m["id"]
        for future in as_completed(futures):
            pid = futures[future]
            try:
                results[pid] = future.result()
                print(f"{pid}: {'ok' if results[pid]['ok'] else 'fixed'}", file=sys.stderr)
            except Exception as exc:
                print(f"{pid}: ERROR {exc}", file=sys.stderr)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as out:
        for m in messages:
            if m["id"] in results:
                out.write(json.dumps(results[m["id"]]) + "\n")
    changed = sum(1 for r in results.values() if r["changed"])
    tokens_in = sum((r.get("usage") or {}).get("input", 0) for r in results.values())
    tokens_out = sum((r.get("usage") or {}).get("output", 0) for r in results.values())
    print(json.dumps({"reviewed": len(results), "changed": changed, "input_tokens": tokens_in, "output_tokens": tokens_out, "output": str(args.output)}))


if __name__ == "__main__":
    main()
