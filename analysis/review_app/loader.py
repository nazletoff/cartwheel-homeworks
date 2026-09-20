"""Turn raw Langfuse traces into review-ready conversations.

One Langfuse trace is one user turn. A conversation is every trace that shares
a ``cartwheel.session_id`` (read out of ``metadata.attributes``, a JSON string;
Langfuse's own session column is empty for these traces). Inside a turn the
agent's steps are ordered by walking each model call's own output: the tool
calls it requested are matched to the tool observations that ran right after
it, so parallel calls stay under the reasoning line that produced them instead
of shuffling by the clock.

Sources: the local cache (``analysis/review_app/cache/traces.json``, pulled
from Langfuse with ``python -m analysis.review_app.loader --refresh``) or any
export in the ``{"traces": [...]}`` shape (``traces/support_traces.json``).
Both camelCase (export) and snake_case (SDK ``.dict()``) field names are read.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CACHE_DIR = HERE / "cache"
CACHE_FILE = CACHE_DIR / "traces.json"
EXPORT_FILE = REPO / "traces" / "support_traces.json"

SCENARIO_FILES = [
    REPO / "scenarios" / "support_scenarios.jsonl",
    REPO / "scenarios" / "pilot_scenarios.jsonl",
]
RESULT_FILES = [
    REPO / "scenarios" / "final-results.jsonl",
    REPO / "scenarios" / "pilot-results.jsonl",
]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """Read the first present key among camelCase / snake_case spellings."""
    if not isinstance(obj, dict):
        return default
    for name in names:
        if name in obj and obj[name] is not None:
            return obj[name]
    return default


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _seconds(start: Any, end: Any) -> float | None:
    a, b = _dt(start), _dt(end)
    if a is None or b is None:
        return None
    return round(max(0.0, (b - a).total_seconds()), 3)


def _maybe_json(value: Any) -> Any:
    """Langfuse sometimes hands JSON back as a string; unwrap it once."""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "[{":
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return value
    return value


def _messages(value: Any) -> list[dict[str, Any]]:
    value = _maybe_json(value)
    if isinstance(value, dict):
        if isinstance(value.get("messages"), list):
            return [m for m in value["messages"] if isinstance(m, dict)]
        return [value]
    if isinstance(value, list):
        return [m for m in value if isinstance(m, dict)]
    return []


def _text_parts(message: dict[str, Any]) -> str:
    parts = message.get("parts")
    if isinstance(parts, list):
        chunks = [
            str(p.get("content", ""))
            for p in parts
            if isinstance(p, dict) and p.get("type", "text") == "text" and p.get("content")
        ]
        return "\n".join(chunks).strip()
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("text")
        ).strip()
    return ""


def _tool_call_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    parts = message.get("parts")
    out: list[dict[str, Any]] = []
    if isinstance(parts, list):
        for p in parts:
            if isinstance(p, dict) and p.get("type") == "tool_call":
                out.append(
                    {
                        "name": p.get("name"),
                        "arguments": _maybe_json(p.get("arguments")),
                        "call_id": p.get("id"),
                    }
                )
    for tc in message.get("tool_calls") or []:
        if isinstance(tc, dict):
            fn = tc.get("function") or {}
            out.append(
                {
                    "name": fn.get("name") or tc.get("name"),
                    "arguments": _maybe_json(fn.get("arguments") or tc.get("arguments")),
                    "call_id": tc.get("id"),
                }
            )
    return out


def _canon(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except TypeError:
        return str(value)


def parse_attributes(trace: dict[str, Any]) -> dict[str, Any]:
    """Return the ``cartwheel.*`` attribute map stored on the trace."""
    meta = _get(trace, "metadata", default={}) or {}
    meta = _maybe_json(meta)
    if not isinstance(meta, dict):
        return {}
    attrs = _maybe_json(meta.get("attributes", {}))
    return attrs if isinstance(attrs, dict) else {}


# ---------------------------------------------------------------------------
# one trace -> one turn
# ---------------------------------------------------------------------------


def _observations(trace: dict[str, Any]) -> list[dict[str, Any]]:
    obs = _get(trace, "observations", default=[]) or []
    out = []
    for o in obs:
        if not isinstance(o, dict):
            continue
        o = dict(o)
        o["_start"] = _get(o, "startTime", "start_time")
        o["_end"] = _get(o, "endTime", "end_time")
        o["_parent"] = _get(o, "parentObservationId", "parent_observation_id")
        out.append(o)
    out.sort(key=lambda o: (_dt(o["_start"]) or datetime.min.replace(tzinfo=timezone.utc)))
    return out


def _user_text(root: dict[str, Any] | None, trace: dict[str, Any]) -> str:
    source = root.get("input") if root else None
    if source is None:
        source = trace.get("input")
    msgs = _messages(source)
    users = [m for m in msgs if m.get("role") == "user"]
    if users:
        return _text_parts(users[-1])
    if msgs:
        return _text_parts(msgs[-1])
    return str(source or "")


def _root_reply(root: dict[str, Any] | None, trace: dict[str, Any]) -> str:
    source = root.get("output") if root else None
    if source is None:
        source = trace.get("output")
    msgs = _messages(source)
    assistants = [m for m in msgs if m.get("role", "assistant") == "assistant"]
    if assistants:
        return _text_parts(assistants[-1])
    return str(source or "")


def build_turn(trace: dict[str, Any]) -> dict[str, Any]:
    """Build one turn record from one raw Langfuse trace."""
    attrs = parse_attributes(trace)
    obs = _observations(trace)
    root = next((o for o in obs if o["_parent"] is None), None)
    gens = [o for o in obs if str(o.get("type", "")).upper() == "GENERATION"]
    tools = [o for o in obs if str(o.get("type", "")).upper() == "TOOL"]
    claimed: set[str] = set()

    steps: list[dict[str, Any]] = []
    reply_text: str | None = None
    for i, gen in enumerate(gens):
        out_msgs = _messages(gen.get("output"))
        reasoning = "\n".join(t for t in (_text_parts(m) for m in out_msgs) if t).strip() or None
        requested = [tc for m in out_msgs for tc in _tool_call_parts(m)]
        window_start = _dt(gen["_end"]) or _dt(gen["_start"])
        window_end = _dt(gens[i + 1]["_start"]) if i + 1 < len(gens) else None
        candidates = []
        for t in tools:
            if t["id"] in claimed:
                continue
            ts = _dt(t["_start"])
            if ts is None:
                continue
            if window_start and ts < window_start:
                continue
            if window_end and ts >= window_end:
                continue
            candidates.append(t)

        calls: list[dict[str, Any]] = []
        for req in requested:
            match = None
            for t in candidates:
                if t["id"] in claimed or t.get("name") != req["name"]:
                    continue
                if _canon(_maybe_json(t.get("input"))) == _canon(req["arguments"]):
                    match = t
                    break
            if match is None:
                match = next(
                    (t for t in candidates if t["id"] not in claimed and t.get("name") == req["name"]),
                    None,
                )
            if match is not None:
                claimed.add(match["id"])
            calls.append(_tool_call_record(req, match))
        for t in candidates:
            if t["id"] not in claimed:
                claimed.add(t["id"])
                calls.append(
                    _tool_call_record(
                        {"name": t.get("name"), "arguments": _maybe_json(t.get("input")), "call_id": None},
                        t,
                    )
                )

        if not requested and not calls and i == len(gens) - 1:
            reply_text = reasoning
            continue
        steps.append(
            {
                "obs_id": gen.get("id"),
                "started_at": _iso(gen["_start"]),
                "duration_s": _seconds(gen["_start"], gen["_end"]),
                "reasoning": reasoning,
                "tool_calls": calls,
            }
        )

    # Tools no generation claimed (clock skew): keep them, last step.
    stray = [t for t in tools if t["id"] not in claimed]
    if stray:
        steps.append(
            {
                "obs_id": None,
                "started_at": _iso(stray[0]["_start"]),
                "duration_s": None,
                "reasoning": None,
                "tool_calls": [
                    _tool_call_record(
                        {"name": t.get("name"), "arguments": _maybe_json(t.get("input")), "call_id": None}, t
                    )
                    for t in stray
                ],
            }
        )

    if reply_text is None and not gens:
        reply_text = _root_reply(root, trace) or None

    started = _iso(root["_start"]) if root else _iso(_get(trace, "timestamp"))
    ended = root["_end"] if root else None
    tool_call_count = sum(len(s["tool_calls"]) for s in steps)
    return {
        "trace_id": str(trace.get("id")),
        "scenario_id": attrs.get("cartwheel.scenario_id"),
        "session_id": attrs.get("cartwheel.session_id"),
        "role": attrs.get("cartwheel.user_role"),
        "user_id": attrs.get("cartwheel.user_id"),
        "prompt_version": attrs.get("cartwheel.prompt_version"),
        "started_at": started,
        "duration_s": _seconds(root["_start"], ended) if root else _get(trace, "latency"),
        "user": {"text": _user_text(root, trace), "ts": started},
        "steps": steps,
        "reply": {"text": reply_text, "ts": _iso(ended) if root else None},
        "tool_call_count": tool_call_count,
        "flags": [],
        "permalink": _get(trace, "htmlPath", "html_path"),
    }


def _tool_call_record(req: dict[str, Any], obs: dict[str, Any] | None) -> dict[str, Any]:
    result = _maybe_json(obs.get("output")) if obs else None
    ok = result.get("ok") if isinstance(result, dict) else None
    error = result.get("error") if isinstance(result, dict) else None
    denied = False
    if obs:
        meta = _maybe_json(obs.get("metadata") or {})
        oattrs = _maybe_json(meta.get("attributes", {})) if isinstance(meta, dict) else {}
        denied = str((oattrs or {}).get("cartwheel.permission_denied", "false")).lower() == "true"
        level = str(obs.get("level", "DEFAULT")).upper()
    else:
        level = "MISSING"
    return {
        "obs_id": obs.get("id") if obs else None,
        "name": req.get("name") or (obs.get("name") if obs else None),
        "arguments": req.get("arguments"),
        "result": result,
        "ok": ok,
        "error": error,
        "level": level,
        "duration_s": _seconds(obs["_start"], obs["_end"]) if obs else None,
        "permission_denied": denied or error == "permission_denied",
    }


# ---------------------------------------------------------------------------
# turns -> conversations
# ---------------------------------------------------------------------------


def _source(scenario_id: str | None) -> str:
    if not scenario_id:
        return "other"
    if scenario_id.startswith("pilot-"):
        return "pilot"
    if scenario_id.startswith("support-"):
        return "final"
    return "other"


def group_conversations(traces: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group raw traces by session id into conversations with ordered turns."""
    by_session: dict[str, list[dict[str, Any]]] = {}
    for trace in traces:
        turn = build_turn(trace)
        if not turn["scenario_id"]:
            continue  # HW2 hand runs carry no scenario id; they are not review material
        key = turn["session_id"] or f"trace:{turn['trace_id']}"
        by_session.setdefault(key, []).append(turn)

    conversations: list[dict[str, Any]] = []
    for session_id, turns in by_session.items():
        turns.sort(key=lambda t: (t["started_at"] or "", t["trace_id"]))
        for i, t in enumerate(turns, start=1):
            t["turn_index"] = i
            t["turn_count"] = len(turns)
        first = turns[0]
        conversations.append(
            {
                "session_id": session_id,
                "scenario_id": first["scenario_id"],
                "source": _source(first["scenario_id"]),
                "role": first["role"],
                "user_id": first["user_id"],
                "prompt_version": first["prompt_version"],
                "scenario_group": None,
                "data_quality_case_id": None,
                "turn_count": len(turns),
                "started_at": first["started_at"],
                "total_duration_s": round(sum(t["duration_s"] or 0 for t in turns), 3),
                "tool_call_count": sum(t["tool_call_count"] for t in turns),
                "turns": turns,
                "answer_key": None,
            }
        )
    conversations.sort(key=lambda c: (c["source"] != "final", c["scenario_id"] or "", c["started_at"] or ""))
    return conversations


# ---------------------------------------------------------------------------
# answer keys and flags
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def attach_answer_keys(
    conversations: list[dict[str, Any]],
    scenarios_paths: Iterable[Path],
    results_paths: Iterable[Path],
) -> None:
    scenarios: dict[str, dict[str, Any]] = {}
    for p in scenarios_paths:
        for rec in _read_jsonl(Path(p)):
            sid = rec.get("id") or rec.get("scenario_id")
            if sid:
                scenarios[sid] = rec
    results: dict[str, dict[str, Any]] = {}
    for p in results_paths:
        for rec in _read_jsonl(Path(p)):
            sid = rec.get("scenario_id")
            if sid:
                results[sid] = rec  # later files (reruns) win

    for conv in conversations:
        scen = scenarios.get(conv["scenario_id"] or "")
        res = results.get(conv["scenario_id"] or "")
        if scen is None and res is None:
            continue
        expected = (scen or {}).get("expected") or {}
        source = expected.get("source") or {}
        key = {
            "evaluation": expected.get("evaluation"),
            "outcome": expected.get("outcome"),
            "criterion": expected.get("criterion"),
            "reason": expected.get("reason"),
            "policy_id": expected.get("policy_id"),
            "source_type": source.get("type"),
            "source_reference": source.get("reference"),
            "tuple": (scen or {}).get("tuple") or {},
            "scenario_group": (scen or {}).get("scenario_group"),
            "data_quality_case_id": (scen or {}).get("data_quality_case_id"),
            "opening_message": (scen or {}).get("opening_message"),
            "followups": (scen or {}).get("followups") or [],
            "runner_status": (res or {}).get("status"),
            "runner_duration_s": (res or {}).get("duration_s"),
            "runner_error": (res or {}).get("error"),
        }
        conv["answer_key"] = key
        conv["scenario_group"] = key["scenario_group"]
        conv["data_quality_case_id"] = key["data_quality_case_id"]
        for t in conv["turns"]:
            t["runner_status"] = key["runner_status"]


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return math.inf
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[idx]


def compute_flags(conversations: list[dict[str, Any]]) -> None:
    turns = [t for c in conversations for t in c["turns"]]
    p95_calls = _percentile([t["tool_call_count"] for t in turns], 0.95)
    p95_dur = _percentile([t["duration_s"] or 0 for t in turns], 0.95)
    for t in turns:
        flags: list[str] = []
        if turns and t["tool_call_count"] >= max(p95_calls, 1) and t["tool_call_count"] > 2:
            flags.append(f"{t['tool_call_count']} tool calls (top 5%)")
        if turns and (t["duration_s"] or 0) >= p95_dur and (t["duration_s"] or 0) > 0 and len(turns) > 1:
            flags.append("slow turn (top 5%)")
        durs = [c["duration_s"] for s in t["steps"] for c in s["tool_calls"] if c["duration_s"] is not None]
        if len(durs) >= 3:
            med = statistics.median(durs)
            if med > 0 and max(durs) / med >= 3:
                flags.append(f"slowest tool call {max(durs) / med:.0f}x the median")
        if any(c["ok"] is False for s in t["steps"] for c in s["tool_calls"]):
            flags.append("tool error")
        if any(c["permission_denied"] for s in t["steps"] for c in s["tool_calls"]):
            flags.append("permission denied")
        if not t["reply"]["text"]:
            flags.append("no reply")
        t["flags"] = flags


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------


def read_raw(path: Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("traces", [])
    return [t for t in data if isinstance(t, dict)]


def load_conversations(source: str | Path = "cache") -> list[dict[str, Any]]:
    """Load, group, key and flag conversations from the cache or an export path."""
    path = CACHE_FILE if str(source) == "cache" else Path(source)
    conversations = group_conversations(read_raw(path))
    attach_answer_keys(conversations, SCENARIO_FILES, RESULT_FILES)
    compute_flags(conversations)
    return conversations


def _to_dict(model: Any) -> dict[str, Any]:
    for attr in ("model_dump", "dict"):
        fn = getattr(model, attr, None)
        if callable(fn):
            try:
                return json.loads(json.dumps(fn(by_alias=True), default=str))
            except TypeError:
                return json.loads(json.dumps(fn(), default=str))
    if hasattr(model, "json"):
        return json.loads(model.json())
    return dict(model)


def fetch_raw_traces(limit: int = 2000) -> list[dict[str, Any]]:
    """Pull every trace carrying a scenario id from Langfuse and cache it."""
    from analysis.helpers import langfuse_io

    lf = langfuse_io._client()
    summaries: list[Any] = []
    page = 1
    while len(summaries) < limit:
        resp = lf.api.trace.list(page=page, limit=min(100, limit - len(summaries)))
        batch = list(resp.data or [])
        summaries.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    raw: list[dict[str, Any]] = []
    seen: set[str] = set()
    for s in summaries:
        if s.id in seen:
            continue
        seen.add(s.id)
        full = _to_dict(lf.api.trace.get(s.id))
        if parse_attributes(full).get("cartwheel.scenario_id"):
            raw.append(full)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "traces": raw})
    )
    return raw


def load_env() -> None:
    """Load ``.env`` (Langfuse keys by name only) when python-dotenv is present."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(REPO / ".env")


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser(description="Review-app trace loader")
    ap.add_argument("--refresh", action="store_true", help="pull traces from Langfuse into the cache")
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--source", default="cache", help="'cache' or a path to a {traces: []} export")
    args = ap.parse_args()
    if args.refresh:
        raw = fetch_raw_traces(args.limit)
        print(f"cached {len(raw)} traces -> {CACHE_FILE}")
    convs = load_conversations(args.source)
    multi = sum(1 for c in convs if c["turn_count"] > 1)
    by_source: dict[str, int] = {}
    for c in convs:
        by_source[c["source"]] = by_source.get(c["source"], 0) + 1
    print(
        f"conversations {len(convs)} (by source {by_source}), traces {sum(c['turn_count'] for c in convs)}, "
        f"multi-turn {multi}, with answer key {sum(1 for c in convs if c['answer_key'])}"
    )


if __name__ == "__main__":
    main()
