# HW4 review app

The review interface for Homework 4: one conversation per screen (every turn of a `cartwheel.session_id`), tool calls under the reasoning line that made them, free-form notes in the margin, a taxonomy panel with present/absent labels, and a progress tab. Labels are written as Langfuse scores and mirrored under `analysis/state/`.

Run from the repo root:

```
uv run --env-file .env python -m analysis.review_app.loader --refresh   # pull traces from Langfuse into analysis/review_app/cache/
uv run --env-file .env uvicorn analysis.review_app.app:app --port 8030  # serve at http://localhost:8030
```

State files: `analysis/state/annotations.json`, `suggestions.json`, `patterns.json`, `sample_manifest.json`, `labels/<mode>.jsonl`. Tests: `uv run pytest tests/test_review_app.py`.

Design and decisions: the Part A proposal in the course notes (`learning/courses/evals-102/hw4-review-ui-proposal.md`, v2).
