"""Homework 2, Part D: authentication tests for the session endpoints.

Both tests run offline: no Langfuse, no Docker, no model provider key. They
call the FastAPI route functions directly and never reach the agent, because
reception (create_session) and the bouncer (_authorize) refuse first.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from server import app as server_app


def _fresh_sessions() -> None:
    server_app._SESSIONS.clear()


def test_create_session_rejects_role_mismatch(world: dict) -> None:
    """User 1 is a shopper in the database; claiming merchant must be refused."""
    _fresh_sessions()
    with pytest.raises(HTTPException) as refused:
        server_app.create_session(server_app.SessionCreate(user_id=1, role="merchant"))
    assert refused.value.status_code == 403
    assert not server_app._SESSIONS  # nothing was filed in the drawer


def test_token_cannot_authorize_another_session(world: dict) -> None:
    """A token issued for session A must not open session B."""
    _fresh_sessions()
    first = server_app.create_session(server_app.SessionCreate(user_id=1, role="shopper"))
    second = server_app.create_session(server_app.SessionCreate(user_id=2, role="shopper"))
    borrowed = f"Bearer {first['token']}"

    with pytest.raises(HTTPException) as refused:
        asyncio.run(
            server_app.post_message(
                second["session_id"],
                server_app.MessageIn(message="Show my recent orders."),
                authorization=borrowed,
            )
        )
    assert refused.value.status_code == 403

    # The same token still opens its own session at the bouncer.
    ctx = server_app._authorize(first["session_id"], borrowed)
    assert ctx.user_id == 1 and ctx.role == "shopper"
