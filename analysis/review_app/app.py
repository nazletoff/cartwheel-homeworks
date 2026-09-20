"""HW4 review interface: FastAPI app serving one static page and a JSON API.

Run from the repo root:

    uv run uvicorn analysis.review_app.app:app --port 8030

Traces come from the local cache (``python -m analysis.review_app.loader
--refresh`` pulls them from Langfuse). Set ``CARTWHEEL_REVIEW_TRACE_SOURCE`` to
an export path to serve that instead; the page shows an "offline export"
banner when the live cache could not be used. State lives under
``analysis/state/`` (or ``CARTWHEEL_ANALYSIS_STATE``). Label saves also write
Langfuse scores unless ``CARTWHEEL_REVIEW_OFFLINE_SCORES=1``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from analysis.helpers import _state
from analysis.review_app import loader
from analysis.review_app.state import StateStore

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
SPEC_FILE = loader.REPO / "SPEC.md"


# ---------------------------------------------------------------------------
# boot: traces, state store, score writer
# ---------------------------------------------------------------------------


def _spec_legend() -> list[dict[str, str]]:
    """The requirement ids from SPEC.md sections 5 and 6, read at startup."""
    if not SPEC_FILE.exists():
        return []
    text = SPEC_FILE.read_text(encoding="utf-8")
    start = text.find("## 5.")
    section = text[start:] if start >= 0 else text
    rows = []
    for m in re.finditer(r"\*\*([A-Z]+-\d+)\.\*\*\s*(.+?)(?=\n- \*\*|\n\n|\Z)", section, re.S):
        rows.append({"id": m.group(1), "text": " ".join(m.group(2).split())})
    return rows


def _pick_source() -> tuple[str, str, str | None]:
    """Return (source_path_or_cache, label, warning)."""
    forced = os.environ.get("CARTWHEEL_REVIEW_TRACE_SOURCE")
    if forced:
        label = "cache" if forced == "cache" else f"file:{Path(forced).name}"
        return forced, label, None
    if loader.CACHE_FILE.exists():
        return "cache", "cache", None
    try:
        loader.load_env()
        from analysis.helpers import langfuse_io

        if langfuse_io.is_configured():
            loader.fetch_raw_traces()
            return "cache", "cache", None
    except Exception as exc:  # network / auth problems: fall back, but say so
        warning = f"Langfuse pull failed ({type(exc).__name__}); serving the committed export"
        return str(loader.EXPORT_FILE), "offline export", warning
    return str(loader.EXPORT_FILE), "offline export", "Langfuse is not configured; serving the committed export"


def _score_writer():
    if os.environ.get("CARTWHEEL_REVIEW_OFFLINE_SCORES") == "1":
        return None
    loader.load_env()
    from analysis.helpers import langfuse_io

    if not langfuse_io.is_configured():
        return None
    configured: set[str] = set()

    def write(trace_id: str, mode: str, label: int, comment: str | None) -> None:
        client = langfuse_io._client()
        if mode not in configured:
            langfuse_io.ensure_score_config(mode, client=client)
            configured.add(mode)
        langfuse_io.write_label_score(trace_id, mode, label, comment=comment, client=client)

    return write


SOURCE, SOURCE_LABEL, SOURCE_WARNING = _pick_source()
CONVERSATIONS = loader.load_conversations(SOURCE)
STATE_ROOT = _state.state_root()
WRITER = _score_writer()
STORE = StateStore(STATE_ROOT, CONVERSATIONS, score_writer=WRITER)
SPEC_LEGEND = _spec_legend()

app = FastAPI(title="Cartwheel HW4 review app")
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------


class AnnotationIn(BaseModel):
    session_id: str | None = None
    trace_id: str | None = None
    block: str = "r"
    quote: str = ""
    note: str = ""
    kind: str = "note"
    mode: str | None = None


class NotePatch(BaseModel):
    note: str


class NoFailureIn(BaseModel):
    session_id: str
    trace_id: str | None = None


class DecisionIn(BaseModel):
    status: str
    reason: str | None = None


class MergeIn(BaseModel):
    keep: str
    fold: str
    reason: str | None = None


class SplitIn(BaseModel):
    source: str
    new_name: str
    annotation_ids: list[str]
    reason: str | None = None


class LabelIn(BaseModel):
    session_id: str
    mode: str
    label: int | None
    evidence: dict[str, Any] | None = None


class BatchIn(BaseModel):
    name: str
    method: str
    items: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@app.get("/")
def index() -> Any:
    page = STATIC / "index.html"
    if page.exists():
        return FileResponse(str(page))
    return PlainTextResponse("review app: static/index.html not built yet", status_code=200)


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    multi = sum(1 for c in CONVERSATIONS if c["turn_count"] > 1)
    return {
        "trace_source": SOURCE_LABEL,
        "warning": SOURCE_WARNING,
        "scores_to_langfuse": WRITER is not None,
        "state_root": str(STATE_ROOT),
        "conversation_count": len(CONVERSATIONS),
        "trace_count": sum(c["turn_count"] for c in CONVERSATIONS),
        "multi_turn_count": multi,
        "spec_legend": SPEC_LEGEND,
    }


@app.get("/api/queues")
def queues() -> list[dict[str, Any]]:
    return STORE.queues()


def _summary(c: dict[str, Any], status: str, pending: set[str]) -> dict[str, Any]:
    return {
        "session_id": c["session_id"],
        "scenario_id": c["scenario_id"],
        "source": c["source"],
        "role": c["role"],
        "scenario_group": c.get("scenario_group"),
        "turn_count": c["turn_count"],
        "tool_call_count": c["tool_call_count"],
        "status": status,
        "flags": sorted({f for t in c["turns"] for f in t["flags"]}),
        "has_pending_suggestion": c["session_id"] in pending,
    }


@app.get("/api/conversations")
def conversations(queue: str = "all") -> list[dict[str, Any]]:
    try:
        order = STORE.queue(queue)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    statuses = STORE.statuses()
    pending = {s["session_id"] for s in STORE.suggestions() if s.get("status") == "pending"}
    return [_summary(STORE.by_session[sid], statuses.get(sid, "unreviewed"), pending) for sid in order]


@app.get("/api/conversation/{session_id}")
def conversation(session_id: str) -> dict[str, Any]:
    c = STORE.by_session.get(session_id)
    if c is None:
        raise HTTPException(404, f"unknown session: {session_id}")
    return {
        **c,
        "status": STORE.status(session_id),
        "annotations": [a for a in STORE.annotations() if a["session_id"] == session_id],
        "suggestions": [s for s in STORE.suggestions() if s.get("session_id") == session_id],
        "labels": STORE.labels(session_id),
    }


# notes


@app.get("/api/annotations")
def get_annotations() -> dict[str, Any]:
    return {"annotations": STORE.annotations()}


@app.post("/api/annotations")
def post_annotations(body: Any = Body(default=None)) -> dict[str, Any]:
    items = STORE.replace_annotations(body or [])
    return {"ok": True, "count": len(items)}


@app.post("/api/annotation")
def post_annotation(body: AnnotationIn) -> dict[str, Any]:
    try:
        return STORE.add_annotation(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.patch("/api/annotation/{annotation_id}")
def patch_annotation(annotation_id: str, body: NotePatch) -> dict[str, Any]:
    try:
        return STORE.update_annotation(annotation_id, body.note)
    except KeyError:
        raise HTTPException(404, annotation_id)


@app.delete("/api/annotation/{annotation_id}")
def delete_annotation(annotation_id: str) -> dict[str, Any]:
    STORE.delete_annotation(annotation_id)
    return {"ok": True}


@app.post("/api/no_failure")
def no_failure(body: NoFailureIn) -> dict[str, Any]:
    if body.session_id not in STORE.by_session:
        raise HTTPException(404, body.session_id)
    return STORE.mark_no_failure(body.session_id, body.trace_id)


# suggestions


@app.get("/api/suggestions")
def get_suggestions() -> list[dict[str, Any]]:
    return STORE.suggestions()


@app.post("/api/suggestions")
def post_suggestions(body: Any = Body(default=None)) -> dict[str, Any]:
    items = body.get("suggestions", []) if isinstance(body, dict) else (body or [])
    saved = STORE.set_suggestions(items)
    return {"ok": True, "count": len(saved), "pending": sum(1 for s in saved if s["status"] == "pending")}


@app.post("/api/suggestion/{suggestion_id}/decision")
def decide(suggestion_id: str, body: DecisionIn) -> dict[str, Any]:
    try:
        return STORE.decide_suggestion(suggestion_id, body.status, body.reason)
    except KeyError:
        raise HTTPException(404, suggestion_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# taxonomy


@app.get("/api/patterns")
def get_patterns() -> dict[str, Any]:
    return STORE.patterns()


@app.post("/api/patterns")
def post_patterns(body: Any = Body(default=None)) -> dict[str, Any]:
    return STORE.set_patterns(body or {"modes": []})


@app.post("/api/patterns/merge")
def merge(body: MergeIn) -> dict[str, Any]:
    try:
        return STORE.merge_modes(body.keep, body.fold, body.reason)
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/patterns/split")
def split(body: SplitIn) -> dict[str, Any]:
    try:
        return STORE.split_mode(body.source, body.new_name, body.annotation_ids, body.reason)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# labels


@app.get("/api/labels")
def get_labels(session_id: str | None = None) -> dict[str, Any]:
    return STORE.labels(session_id)


@app.post("/api/label")
def post_label(body: LabelIn) -> Any:
    try:
        records = STORE.set_label(body.session_id, body.mode, body.label, body.evidence)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # the file write happened; Langfuse did not
        return JSONResponse(
            {"records": STORE.labels(body.session_id).get(body.mode, []), "langfuse_error": f"{type(exc).__name__}: {exc}"},
            status_code=200,
        )
    return {"records": records, "langfuse_error": None}


# batches and progress


@app.get("/api/samples")
def get_samples() -> dict[str, Any]:
    return STORE.manifest()


@app.post("/api/samples")
def post_samples(body: BatchIn) -> dict[str, Any]:
    try:
        return STORE.add_batch(body.name, body.method, body.items)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/progress")
def progress() -> dict[str, Any]:
    return STORE.progress()
