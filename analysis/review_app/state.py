"""Every read and write of ``analysis/state/`` for the review app.

The files keep the reference interface's names so the review-loop watcher
(``analysis/skill/review-loop.md``) works unchanged:

- ``annotations.json``   free-form notes and "no failure observed" marks
- ``suggestions.json``   agent suggestions with their accept / reject decision
- ``patterns.json``      the taxonomy, ``{"modes": [...]}``
- ``labels/<mode>.jsonl`` one line per trace: present (1) or absent (0)
- ``sample_manifest.json`` the review batches (plus the helpers' pick list)

Labels follow the rule agreed on 2026-09-20: the review unit is the
conversation; a mode marked present goes as 1 on the turn that holds the
evidence quote and 0 on the conversation's other turns (source
``default_absent``), and the Part E pass may override any cell. Langfuse is
the canonical label store: ``score_writer`` is called once per changed
record; the app wires it to ``analysis.helpers.langfuse_io.write_label_score``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from analysis.helpers import _state

ScoreWriter = Callable[[str, str, int, str | None], None]

ANNOTATIONS = "annotations.json"
SUGGESTIONS = "suggestions.json"
PATTERNS = "patterns.json"
MANIFEST = "sample_manifest.json"

MODE_DEFAULTS: dict[str, Any] = {
    "definition": "",
    "status": "candidate",
    "spec_source": None,
    "evaluator_type": None,
    "boundary": None,
    "nearest_mode": None,
    "created_from": [],
    "example_trace_ids": [],
    "first_failure_count": 0,
    "any_instance_count": 0,
    "revisions": [],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:10]


class StateStore:
    def __init__(
        self,
        root: Path | str,
        conversations: list[dict[str, Any]],
        score_writer: ScoreWriter | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "labels").mkdir(exist_ok=True)
        self.conversations = conversations
        self.by_session = {c["session_id"]: c for c in conversations}
        self.session_of_trace = {
            t["trace_id"]: c["session_id"] for c in conversations for t in c["turns"]
        }
        self.score_writer = score_writer

    # -- files ------------------------------------------------------------

    def _path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def _read(self, name: str, default: Any) -> Any:
        return _state.read_json(self._path(name), default=default)

    def _write(self, name: str, data: Any) -> None:
        _state.write_json(self._path(name), data)

    # -- annotations ------------------------------------------------------

    def annotations(self) -> list[dict[str, Any]]:
        raw = self._read(ANNOTATIONS, {"annotations": []})
        if isinstance(raw, dict):
            raw = raw.get("annotations", [])
        return [a for a in raw if isinstance(a, dict)]

    def _write_annotations(self, items: list[dict[str, Any]]) -> None:
        self._write(ANNOTATIONS, {"annotations": items})

    def add_annotation(self, a: dict[str, Any]) -> dict[str, Any]:
        session_id = a.get("session_id") or self.session_of_trace.get(a.get("trace_id", ""))
        if not session_id:
            raise ValueError("annotation needs a session_id or a known trace_id")
        rec = {
            "id": a.get("id") or _new_id("a"),
            "session_id": session_id,
            "trace_id": a.get("trace_id"),
            "block": a.get("block", "r"),
            "quote": a.get("quote", ""),
            "note": a.get("note", ""),
            "kind": a.get("kind", "note"),
            "source": a.get("source", "human"),
            "mode": a.get("mode"),
            "ts": a.get("ts") or _now(),
        }
        items = self.annotations()
        items.append(rec)
        self._write_annotations(items)
        return rec

    def update_annotation(self, annotation_id: str, note: str) -> dict[str, Any]:
        items = self.annotations()
        for a in items:
            if a["id"] == annotation_id:
                a["note"] = note
                a["edited_ts"] = _now()
                self._write_annotations(items)
                return a
        raise KeyError(annotation_id)

    def delete_annotation(self, annotation_id: str) -> None:
        items = [a for a in self.annotations() if a["id"] != annotation_id]
        self._write_annotations(items)

    def mark_no_failure(self, session_id: str, trace_id: str | None = None) -> dict[str, Any]:
        for a in self.annotations():
            if a["session_id"] == session_id and a.get("kind") == "no_failure":
                return a
        conv = self.by_session.get(session_id)
        if trace_id is None and conv:
            trace_id = conv["turns"][-1]["trace_id"]
        return self.add_annotation(
            {
                "session_id": session_id,
                "trace_id": trace_id,
                "block": "r",
                "quote": "",
                "note": "no failure observed",
                "kind": "no_failure",
            }
        )

    def replace_annotations(self, data: Any) -> list[dict[str, Any]]:
        """Reference-compatible whole-document POST."""
        items = data.get("annotations", []) if isinstance(data, dict) else data
        items = [a for a in items if isinstance(a, dict)]
        for a in items:
            a.setdefault("id", _new_id("a"))
            a.setdefault("kind", "note")
            a.setdefault("source", "human")
            a.setdefault("ts", _now())
            if not a.get("session_id"):
                a["session_id"] = self.session_of_trace.get(a.get("trace_id", ""))
        self._write_annotations(items)
        return items

    # -- suggestions ------------------------------------------------------

    def suggestions(self) -> list[dict[str, Any]]:
        raw = self._read(SUGGESTIONS, [])
        if isinstance(raw, dict):
            raw = raw.get("suggestions", [])
        return [s for s in raw if isinstance(s, dict)]

    def _write_suggestions(self, items: list[dict[str, Any]]) -> None:
        self._write(SUGGESTIONS, items)

    def set_suggestions(self, new: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace the pending set; decided suggestions are never dropped."""
        decided = [s for s in self.suggestions() if s.get("status") in ("accepted", "rejected")]
        decided_ids = {s["id"] for s in decided}
        pending: list[dict[str, Any]] = []
        for s in new:
            if not isinstance(s, dict):
                continue
            sid = s.get("id") or _new_id("s")
            if sid in decided_ids:
                continue
            rec = {
                "id": sid,
                "session_id": s.get("session_id") or self.session_of_trace.get(s.get("trace_id", "")),
                "trace_id": s.get("trace_id"),
                "block": s.get("block", "r"),
                "quote": s.get("quote", ""),
                "mode": s.get("mode"),
                "method": s.get("method"),
                "note": s.get("note"),
                "status": "pending",
                "created_ts": s.get("created_ts") or _now(),
                "decision_ts": None,
                "reason": None,
            }
            pending.append(rec)
        items = decided + pending
        self._write_suggestions(items)
        return items

    def decide_suggestion(self, suggestion_id: str, status: str, reason: str | None = None) -> dict[str, Any]:
        if status not in ("accepted", "rejected"):
            raise ValueError("status must be accepted or rejected")
        items = self.suggestions()
        target = next((s for s in items if s["id"] == suggestion_id), None)
        if target is None:
            raise KeyError(suggestion_id)
        target["status"] = status
        target["decision_ts"] = _now()
        target["reason"] = reason
        self._write_suggestions(items)
        if status == "accepted":
            note = f"[{target.get('mode') or 'suggestion'}] {reason or target.get('note') or ''}".strip()
            self.add_annotation(
                {
                    "session_id": target["session_id"],
                    "trace_id": target["trace_id"],
                    "block": target.get("block", "r"),
                    "quote": target.get("quote", ""),
                    "note": note,
                    "kind": "note",
                    "source": "accepted_suggestion",
                    "mode": target.get("mode"),
                }
            )
            mode = self._mode(target.get("mode"))
            if mode and mode.get("status") == "final" and target.get("trace_id"):
                self.set_label(
                    target["session_id"],
                    mode["name"],
                    1,
                    {"trace_id": target["trace_id"], "quote": target.get("quote", "")},
                    source="accepted_suggestion",
                )
        return target

    # -- patterns ---------------------------------------------------------

    def patterns(self) -> dict[str, Any]:
        raw = self._read(PATTERNS, {"modes": []})
        modes: list[dict[str, Any]]
        if isinstance(raw, dict) and isinstance(raw.get("modes"), list):
            modes = [m for m in raw["modes"] if isinstance(m, dict) and m.get("name")]
        elif isinstance(raw, dict):
            modes = [{"name": k, **(v or {})} for k, v in raw.items() if isinstance(v, dict)]
        else:
            modes = []
        return {"modes": [{**MODE_DEFAULTS, **m} for m in modes]}

    def _mode(self, name: str | None) -> dict[str, Any] | None:
        if not name:
            return None
        return next((m for m in self.patterns()["modes"] if m["name"] == name), None)

    def final_modes(self) -> list[dict[str, Any]]:
        return [m for m in self.patterns()["modes"] if m.get("status") == "final"]

    def set_patterns(self, data: Any) -> dict[str, Any]:
        existing = {m["name"]: m for m in self.patterns()["modes"]}
        if isinstance(data, dict) and isinstance(data.get("modes"), list):
            incoming = data["modes"]
        elif isinstance(data, dict):
            incoming = [{"name": k, **(v or {})} for k, v in data.items() if isinstance(v, dict)]
        else:
            incoming = []
        modes = []
        for m in incoming:
            if not isinstance(m, dict) or not m.get("name"):
                continue
            prev = existing.get(m["name"], {})
            merged = {**MODE_DEFAULTS, **prev, **m}
            if not m.get("revisions") and prev.get("revisions"):
                merged["revisions"] = prev["revisions"]
            modes.append(merged)
        doc = {"modes": modes, "updated_at": _now()}
        self._write(PATTERNS, doc)
        return doc

    def merge_modes(self, keep: str, fold: str, reason: str | None = None) -> dict[str, Any]:
        doc = self.patterns()
        modes = doc["modes"]
        k = next((m for m in modes if m["name"] == keep), None)
        f = next((m for m in modes if m["name"] == fold), None)
        if k is None or f is None:
            raise KeyError(f"unknown mode: {keep if k is None else fold}")
        k["created_from"] = list(dict.fromkeys([*k["created_from"], *f["created_from"]]))
        k["example_trace_ids"] = list(dict.fromkeys([*k["example_trace_ids"], *f["example_trace_ids"]]))
        k["revisions"] = [*k["revisions"], {"ts": _now(), "change": f"merged {fold} into {keep}", "reason": reason}]
        modes = [m for m in modes if m["name"] != fold]
        self._write(PATTERNS, {"modes": modes, "updated_at": _now()})
        # notes that carried the folded mode now carry the kept one
        items = self.annotations()
        changed = False
        for a in items:
            if a.get("mode") == fold:
                a["mode"] = keep
                changed = True
        if changed:
            self._write_annotations(items)
        # label files: 1 wins per trace
        fold_recs = self._label_records(fold)
        if fold_recs:
            keep_recs = self._label_records(keep)
            for tid, rec in fold_recs.items():
                cur = keep_recs.get(tid)
                if cur is None or (rec["label"] == 1 and cur["label"] == 0):
                    keep_recs[tid] = {**rec, "mode": keep}
            self._write_label_records(keep, keep_recs)
            self._path("labels", f"{fold}.jsonl").unlink(missing_ok=True)
        return self.patterns()

    def split_mode(
        self, source: str, new_name: str, annotation_ids: list[str], reason: str | None = None
    ) -> dict[str, Any]:
        doc = self.patterns()
        modes = doc["modes"]
        s = next((m for m in modes if m["name"] == source), None)
        if s is None:
            raise KeyError(source)
        if any(m["name"] == new_name for m in modes):
            raise ValueError(f"mode exists: {new_name}")
        moved = [aid for aid in s["created_from"] if aid in set(annotation_ids)]
        s["created_from"] = [aid for aid in s["created_from"] if aid not in set(annotation_ids)]
        s["revisions"] = [*s["revisions"], {"ts": _now(), "change": f"split {new_name} out", "reason": reason}]
        new_mode = {
            **MODE_DEFAULTS,
            "name": new_name,
            "created_from": moved or list(annotation_ids),
            "revisions": [{"ts": _now(), "change": f"split from {source}", "reason": reason}],
        }
        modes.append(new_mode)
        self._write(PATTERNS, {"modes": modes, "updated_at": _now()})
        return self.patterns()

    # -- labels -----------------------------------------------------------

    def _label_path(self, mode: str) -> Path:
        return self._path("labels", f"{mode}.jsonl")

    def _label_records(self, mode: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for rec in _state.read_jsonl(self._label_path(mode)):
            if isinstance(rec, dict) and rec.get("trace_id"):
                out[rec["trace_id"]] = rec
        return out

    def _write_label_records(self, mode: str, recs: dict[str, dict[str, Any]]) -> None:
        ordered = [recs[k] for k in sorted(recs)]
        _state.write_jsonl(self._label_path(mode), ordered)

    def labels(self, session_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for m in self.patterns()["modes"]:
            recs = list(self._label_records(m["name"]).values())
            if session_id is not None:
                recs = [r for r in recs if r.get("session_id") == session_id]
            if recs:
                out[m["name"]] = recs
        return out

    def set_label(
        self,
        session_id: str,
        mode: str,
        label: int | None,
        evidence: dict[str, Any] | None,
        source: str = "human",
    ) -> list[dict[str, Any]]:
        if self._mode(mode) is None:
            raise KeyError(f"unknown mode: {mode}")
        conv = self.by_session.get(session_id)
        if conv is None:
            raise KeyError(f"unknown session: {session_id}")
        turn_ids = [t["trace_id"] for t in conv["turns"]]
        recs = self._label_records(mode)
        before = {tid: dict(recs[tid]) for tid in turn_ids if tid in recs}

        if label is None:
            for tid in turn_ids:
                recs.pop(tid, None)
            self._write_label_records(mode, recs)
            return []

        if label not in (0, 1):
            raise ValueError("label must be 0, 1 or null")
        ts = _now()
        if label == 1:
            if not evidence or evidence.get("trace_id") not in turn_ids:
                raise ValueError("a present label needs evidence on one of the conversation's turns")
            for tid in turn_ids:
                if tid == evidence["trace_id"]:
                    recs[tid] = self._label_record(tid, session_id, mode, 1, source, evidence.get("quote"), ts)
                else:
                    cur = recs.get(tid)
                    if cur and cur.get("label") == 1 and cur.get("source") in ("human", "accepted_suggestion"):
                        continue
                    recs[tid] = self._label_record(tid, session_id, mode, 0, "default_absent", None, ts)
        else:
            for tid in turn_ids:
                recs[tid] = self._label_record(tid, session_id, mode, 0, source, None, ts)

        self._write_label_records(mode, recs)
        out = [recs[tid] for tid in turn_ids if tid in recs]
        if self.score_writer is not None:
            for rec in out:
                prev = before.get(rec["trace_id"])
                if prev and prev.get("label") == rec["label"] and prev.get("source") == rec["source"]:
                    continue
                self.score_writer(rec["trace_id"], mode, int(rec["label"]), rec.get("evidence_quote"))
        return out

    @staticmethod
    def _label_record(
        trace_id: str, session_id: str, mode: str, label: int, source: str, quote: str | None, ts: str
    ) -> dict[str, Any]:
        return {
            "label_id": f"{trace_id}#{mode}",
            "trace_id": trace_id,
            "session_id": session_id,
            "mode": mode,
            "label": int(label),
            "source": source,
            "evidence_quote": quote,
            "ts": ts,
        }

    # -- batches and queues ----------------------------------------------

    def manifest(self) -> dict[str, Any]:
        raw = self._read(MANIFEST, {})
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("source", "langfuse")
        raw.setdefault("batches", [])
        return raw

    def sample_sessions(self) -> list[str]:
        seen: list[str] = []
        for b in self.manifest()["batches"]:
            for item in b.get("items", []):
                if item.get("session_id") not in seen:
                    seen.append(item["session_id"])
        return seen

    def add_batch(self, name: str, method: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        man = self.manifest()
        taken = {item["session_id"]: b["name"] for b in man["batches"] for item in b.get("items", [])}
        clean: list[dict[str, Any]] = []
        for item in items:
            sid = item.get("session_id")
            if sid not in self.by_session:
                raise ValueError(f"unknown session: {sid}")
            if sid in taken and taken[sid] != name:
                raise ValueError(f"session {sid} already in {taken[sid]}")
            if any(c["session_id"] == sid for c in clean):
                continue
            clean.append({"session_id": sid, "reason": item.get("reason", "")})
        batch = {"name": name, "method": method, "created_at": _now(), "items": clean}
        man["batches"] = [b for b in man["batches"] if b["name"] != name] + [batch]
        picks = []
        for b in man["batches"]:
            for item in b["items"]:
                conv = self.by_session[item["session_id"]]
                for t in conv["turns"]:
                    picks.append(
                        {
                            "trace_id": t["trace_id"],
                            "session_id": item["session_id"],
                            "scenario_id": conv["scenario_id"],
                            "reason": item["reason"],
                            "batch": b["name"],
                        }
                    )
        man.update(
            {
                "source": "langfuse",
                "k": len(picks),
                "strategy": "batches",
                "selected_at": _now(),
                "picks": picks,
            }
        )
        self._write(MANIFEST, man)
        return man

    def status(self, session_id: str) -> str:
        finals = self.final_modes()
        conv = self.by_session.get(session_id)
        if finals and conv:
            turn_ids = [t["trace_id"] for t in conv["turns"]]
            complete = all(
                all(tid in self._label_records(m["name"]) for tid in turn_ids) for m in finals
            )
            if complete:
                return "labelled"
        if any(a["session_id"] == session_id for a in self.annotations()):
            return "noted"
        return "unreviewed"

    def statuses(self) -> dict[str, str]:
        """Status for every conversation in one pass (the list view calls this)."""
        finals = self.final_modes()
        label_sets = {m["name"]: set(self._label_records(m["name"])) for m in finals}
        noted = {a["session_id"] for a in self.annotations()}
        out: dict[str, str] = {}
        for c in self.conversations:
            sid = c["session_id"]
            if finals and all(
                all(t["trace_id"] in label_sets[m["name"]] for t in c["turns"]) for m in finals
            ):
                out[sid] = "labelled"
            elif sid in noted:
                out[sid] = "noted"
            else:
                out[sid] = "unreviewed"
        return out

    def queue(self, name: str) -> list[str]:
        order = [c["session_id"] for c in self.conversations]
        sample = self.sample_sessions()
        if name == "all":
            return order
        if name == "final":
            return [c["session_id"] for c in self.conversations if c["source"] == "final"]
        if name == "suggestions":
            pend = {s["session_id"] for s in self.suggestions() if s.get("status") == "pending"}
            return [sid for sid in order if sid in pend]
        if name == "unreviewed":
            st = self.statuses()
            pool = sample or order
            return [sid for sid in pool if st.get(sid) == "unreviewed"]
        if name.startswith("mode:"):
            mode = name[5:]
            hits = {r["session_id"] for r in self._label_records(mode).values() if r.get("label") == 1}
            hits |= {a["session_id"] for a in self.annotations() if a.get("mode") == mode}
            return [sid for sid in order if sid in hits]
        if name.startswith("role:"):
            role = name[5:]
            pool = sample or order
            return [sid for sid in pool if self.by_session[sid]["role"] == role]
        for b in self.manifest()["batches"]:
            if b["name"] == name:
                return [item["session_id"] for item in b["items"]]
        raise KeyError(f"unknown queue: {name}")

    def queues(self) -> list[dict[str, Any]]:
        names = [b["name"] for b in self.manifest()["batches"]]
        names += ["suggestions", "unreviewed"]
        names += [f"mode:{m['name']}" for m in self.patterns()["modes"]]
        names += [f"role:{r}" for r in ("shopper", "merchant", "support")]
        names += ["final", "all"]
        return [{"name": n, "count": len(self.queue(n))} for n in names]

    # -- progress ---------------------------------------------------------

    def progress(self) -> dict[str, Any]:
        sample = set(self.sample_sessions())
        st = self.statuses()
        anns = self.annotations()
        noted = {a["session_id"] for a in anns if a.get("kind") == "note"}
        clean = {a["session_id"] for a in anns if a.get("kind") == "no_failure"}

        def row() -> dict[str, int]:
            return {"total": 0, "in_sample": 0, "reviewed": 0, "noted": 0, "no_failure": 0}

        coverage: dict[str, dict[str, dict[str, int]]] = {"role": {}, "intent": {}, "scenario_group": {}}
        for c in self.conversations:
            sid = c["session_id"]
            keys = {
                "role": c.get("role") or "unknown",
                "intent": ((c.get("answer_key") or {}).get("tuple") or {}).get("intent") or "unknown",
                "scenario_group": c.get("scenario_group") or "unknown",
            }
            for dim, val in keys.items():
                r = coverage[dim].setdefault(val, row())
                r["total"] += 1
                if sid in sample:
                    r["in_sample"] += 1
                if st.get(sid) != "unreviewed":
                    r["reviewed"] += 1
                if sid in noted:
                    r["noted"] += 1
                if sid in clean:
                    r["no_failure"] += 1

        modes = self.patterns()["modes"]
        label_recs = {m["name"]: self._label_records(m["name"]) for m in modes}
        grid = []
        for sid in self.sample_sessions():
            c = self.by_session[sid]
            grid.append(
                {
                    "session_id": sid,
                    "scenario_id": c["scenario_id"],
                    "role": c["role"],
                    "status": st.get(sid),
                    "turns": [
                        {
                            "trace_id": t["trace_id"],
                            "labels": {
                                m["name"]: (label_recs[m["name"]].get(t["trace_id"]) or {}).get("label")
                                for m in modes
                            },
                        }
                        for t in c["turns"]
                    ],
                }
            )

        sample_n = len(sample) or 0
        mode_rows = []
        for m in modes:
            recs = label_recs[m["name"]]
            present_sessions = {r["session_id"] for r in recs.values() if r.get("label") == 1}
            first_failure = sum(
                1 for a in anns if a.get("mode") == m["name"] and a.get("source") == "human"
            ) + len(m.get("created_from") or [])
            mode_rows.append(
                {
                    "name": m["name"],
                    "status": m.get("status"),
                    "spec_source": m.get("spec_source"),
                    "first_failure_count": first_failure,
                    "any_instance_count": len(present_sessions),
                    "labelled_traces": len(recs),
                    "sample_fraction": (len(present_sessions & sample) / sample_n) if sample_n else None,
                }
            )

        ann_by_id = {a["id"]: a for a in anns}
        batch_of = {item["session_id"]: b["name"] for b in self.manifest()["batches"] for item in b["items"]}
        discovery = []
        for b in self.manifest()["batches"]:
            sids = [item["session_id"] for item in b["items"]]
            new_modes = []
            for m in modes:
                origins = [ann_by_id[aid] for aid in m.get("created_from") or [] if aid in ann_by_id]
                if not origins:
                    continue
                earliest = min(origins, key=lambda a: a.get("ts") or "")
                if batch_of.get(earliest["session_id"]) == b["name"]:
                    new_modes.append(m["name"])
            discovery.append(
                {
                    "batch": b["name"],
                    "method": b["method"],
                    "size": len(sids),
                    "reviewed": sum(1 for s in sids if st.get(s) != "unreviewed"),
                    "new_modes": new_modes,
                }
            )

        return {
            "sample_size": sample_n,
            "reviewed": sum(1 for s in sample if st.get(s) != "unreviewed"),
            "coverage": coverage,
            "grid": grid,
            "modes": mode_rows,
            "discovery": discovery,
            "suggestions_pending": sum(1 for s in self.suggestions() if s.get("status") == "pending"),
        }
