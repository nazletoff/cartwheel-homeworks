"""Step 4 of scenario generation: join plans and reviewed messages into a scenario file.

Each output line is one scenario in the format ``scenarios/validate.py``
checks: id, scenario_group, data_quality_case_id, tuple, opening_message,
followups, expected. The generation-only ``visible`` block is left out, and
the file is validated before it is written.

Usage:
    uv run python -m scenarios.gen.assemble \
        scenarios/gen/work/pilot-plans.jsonl scenarios/gen/work/pilot-critic.jsonl \
        scenarios/pilot_scenarios.jsonl [--final]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scenarios.gen.write_messages import DASHES, disallowed_numbers
from scenarios.validate import validate_scenarios


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble scenarios from plans and messages.")
    parser.add_argument("plans", type=Path)
    parser.add_argument("messages", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--final", action="store_true", help="also enforce the 250-record final contract")
    args = parser.parse_args()
    plans = [json.loads(l) for l in args.plans.read_text().splitlines() if l.strip()]
    messages = {m["id"]: m for m in (json.loads(l) for l in args.messages.read_text().splitlines() if l.strip())}
    missing = [p["id"] for p in plans if p["id"] not in messages]
    if missing:
        raise SystemExit(f"no messages for: {', '.join(missing)}")
    scenarios = []
    problems = []
    for plan in plans:
        msg = messages[plan["id"]]
        texts = [msg["opening_message"], *msg["followups"]]
        bad = disallowed_numbers(texts, plan["visible"])
        if bad:
            problems.append(f"{plan['id']}: disallowed numbers {bad}")
        if any(DASHES.search(t) for t in texts):
            problems.append(f"{plan['id']}: em or en dash")
        tuple_ = dict(plan["tuple"])
        tuple_["turn_count"] = 1 + len(msg["followups"])
        scenarios.append(
            {
                "id": plan["id"],
                "scenario_group": plan["scenario_group"],
                "data_quality_case_id": plan.get("data_quality_case_id"),
                "tuple": tuple_,
                "opening_message": msg["opening_message"],
                "followups": msg["followups"],
                "expected": plan["expected"],
            }
        )
    if problems:
        raise SystemExit("assembly refused:\n- " + "\n- ".join(problems))
    summary = validate_scenarios(scenarios, final=args.final)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as out:
        for scenario in scenarios:
            out.write(json.dumps(scenario) + "\n")
    print(json.dumps({**summary, "output": str(args.output)}, sort_keys=True))


if __name__ == "__main__":
    main()
