"""Unit tests for the two-phase confirmation primitives — the correctness
target that matters most in Phase 3: a mutated payload on the confirm call
must never execute, and a token must never be usable twice.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from spotify_mcp.tools.confirm import ConfirmationStore


def test_issue_then_consume_with_identical_args_succeeds():
    store = ConfirmationStore(ttl_s=600)
    token, _ = store.issue("create_playlist", {"name": "x"})
    store.consume(token, "create_playlist", {"name": "x"})  # should not raise


def test_consume_rejects_mutated_args():
    store = ConfirmationStore(ttl_s=600)
    token, _ = store.issue("create_playlist", {"name": "x"})
    with pytest.raises(ToolError, match="don't match"):
        store.consume(token, "create_playlist", {"name": "y"})


def test_consume_rejects_wrong_action():
    store = ConfirmationStore(ttl_s=600)
    token, _ = store.issue("create_playlist", {"name": "x"})
    with pytest.raises(ToolError, match="was issued for"):
        store.consume(token, "add_playlist_items", {"name": "x"})


def test_token_is_single_use():
    store = ConfirmationStore(ttl_s=600)
    token, _ = store.issue("create_playlist", {"name": "x"})
    store.consume(token, "create_playlist", {"name": "x"})
    with pytest.raises(ToolError, match="invalid, expired, or already used"):
        store.consume(token, "create_playlist", {"name": "x"})


def test_unknown_token_is_rejected():
    store = ConfirmationStore(ttl_s=600)
    with pytest.raises(ToolError, match="invalid, expired, or already used"):
        store.consume("cf_nonexistent", "create_playlist", {"name": "x"})


def test_expired_token_is_rejected(monkeypatch: pytest.MonkeyPatch):
    import time as time_module

    state = {"now": 1000.0}
    monkeypatch.setattr(time_module, "time", lambda: state["now"])

    store = ConfirmationStore(ttl_s=10)
    token, _ = store.issue("create_playlist", {"name": "x"})

    state["now"] += 11  # past the 10s TTL
    with pytest.raises(ToolError, match="invalid, expired, or already used"):
        store.consume(token, "create_playlist", {"name": "x"})


def test_argument_order_does_not_affect_hash():
    """dict key order must not matter — the model may re-serialize arguments
    differently between the preview call and the confirm call."""
    store = ConfirmationStore(ttl_s=600)
    token, _ = store.issue("add_playlist_items", {"uris": ["a", "b"], "playlist_id": "p1"})
    store.consume(token, "add_playlist_items", {"playlist_id": "p1", "uris": ["a", "b"]})


def test_reordered_list_contents_do_change_the_hash():
    """Unlike dict key order, the actual list contents/order are semantically
    meaningful (e.g. track order) and must be part of the bound payload."""
    store = ConfirmationStore(ttl_s=600)
    token, _ = store.issue("add_playlist_items", {"uris": ["a", "b"]})
    with pytest.raises(ToolError, match="don't match"):
        store.consume(token, "add_playlist_items", {"uris": ["b", "a"]})
