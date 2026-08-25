"""Two-phase confirmation for write tools, plus the dry-run status shape.

Design (PLAN.md §6): a write tool called without `confirm_token` executes
nothing — it returns a preview and a single-use token. Calling again with
that token AND the identical arguments executes the write; any drift in the
arguments invalidates the token, so what runs is provably what was shown.

Where the client declares elicitation support, the tool instead attempts to
get approval directly within the same call via `ctx.elicit(...)` — a stronger
guarantee than the two-call token flow, since the client is architecturally
required to put a human in the loop rather than relying on the model to
re-invoke honestly. This is attempted first and falls back to the token flow
on any error or when the client doesn't support it.

Neither path is a hard technical gate against a model calling twice on its
own initiative — see PLAN.md §6's own caveat about this.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

from spotify_mcp.logging import get_logger

if TYPE_CHECKING:
    from spotify_mcp.config import Settings

logger = get_logger("tools.confirm")


class WriteToolResult(BaseModel):
    """The one return shape every write tool uses — see PLAN.md §2 on why a
    single concrete model (not a Union) is what keeps structured output
    unwrapped."""

    model_config = ConfigDict(extra="ignore")

    status: Literal["confirmation_required", "executed", "dry_run"]
    action: str
    confirm_token: str | None = None
    expires_at: str | None = None
    preview: dict[str, Any] | None = None
    next_step: str | None = None
    result: dict[str, Any] | None = None


class ConfirmDecision(BaseModel):
    """Elicitation response schema — MCP requires primitive-only fields."""

    confirm: bool


@dataclass
class _Pending:
    action: str
    payload_hash: str
    expires_at: float


class ConfirmationStore:
    """Process-memory store for pending write confirmations.

    Single-use, TTL-bound. Not persisted across restarts — a token from a
    previous server run is never valid, which is the correct behavior (there
    is no client to have seen the preview it was issued for)."""

    def __init__(self, ttl_s: int = 600):
        self._ttl_s = ttl_s
        self._pending: dict[str, _Pending] = {}

    def _prune(self) -> None:
        now = time.time()
        for token in [t for t, p in self._pending.items() if p.expires_at < now]:
            del self._pending[token]

    def issue(self, action: str, args: dict[str, Any]) -> tuple[str, str]:
        self._prune()
        token = f"cf_{secrets.token_urlsafe(24)}"
        expires_at = time.time() + self._ttl_s
        self._pending[token] = _Pending(
            action=action, payload_hash=_hash_payload(args), expires_at=expires_at
        )
        return token, _iso(expires_at)

    def consume(self, token: str, action: str, args: dict[str, Any]) -> None:
        self._prune()
        pending = self._pending.pop(token, None)  # single-use regardless of outcome below
        if pending is None:
            raise ToolError(
                "confirm_token is invalid, expired, or already used. Call this tool again "
                "without confirm_token to get a fresh preview."
            )
        if pending.action != action:
            raise ToolError(f"confirm_token was issued for {pending.action!r}, not {action!r}.")
        if pending.payload_hash != _hash_payload(args):
            raise ToolError(
                "The arguments on this call don't match what was previewed when confirm_token "
                "was issued — nothing was executed. Call this tool again without confirm_token "
                "to get a fresh preview matching the arguments you actually intend to send."
            )


def _hash_payload(args: dict[str, Any]) -> str:
    canonical = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


async def gate(
    ctx: Context,
    store: ConfirmationStore,
    *,
    action: str,
    args: dict[str, Any],
    confirm_token: str | None,
    preview: dict[str, Any],
    elicit_message: str,
) -> WriteToolResult | None:
    """Returns a WriteToolResult to send back as-is (the call ends here,
    nothing executed), or None when the caller is cleared to execute —
    either the token matched, or the user just approved via elicitation."""

    if confirm_token is not None:
        store.consume(confirm_token, action, args)
        return None

    if ctx.client_capabilities and ctx.client_capabilities.elicitation:
        try:
            elicited = await ctx.elicit(elicit_message, ConfirmDecision)
        except Exception:
            logger.info("elicitation attempt failed for %r; falling back to confirm_token", action)
        else:
            if elicited.action == "accept" and elicited.data is not None:
                if elicited.data.confirm:
                    return None
                raise ToolError("Cancelled: not approved.")
            if elicited.action in ("decline", "cancel"):
                raise ToolError("Cancelled: not approved.")
            # accept with no usable data — fall through to the token flow below

    token, expires_at = store.issue(action, args)
    return WriteToolResult(
        status="confirmation_required",
        action=action,
        confirm_token=token,
        expires_at=expires_at,
        preview=preview,
        next_step=(
            "Show this preview to the user in full before doing anything else. Call this tool "
            "again ONLY after they explicitly approve, passing confirm_token and the exact same "
            "arguments — any change invalidates the token."
        ),
    )


def executed(action: str, result: dict[str, Any]) -> WriteToolResult:
    status = "dry_run" if result.get("dry_run") else "executed"
    return WriteToolResult(status=status, action=action, result=result)


def write_audit(
    settings: Settings,
    *,
    action: str,
    args: dict[str, Any],
    token: str | None,
    outcome: str,
    snapshot_id: str | None = None,
) -> None:
    """Append one line to the local write audit log — "what did it do at
    2am" (PLAN.md §6). Best-effort: a logging failure must never fail the
    write it's recording."""
    record = {
        "ts": _iso(time.time()),
        "action": action,
        "payload_hash": _hash_payload(args),
        "confirm_token": token,
        "outcome": outcome,
        "snapshot_id": snapshot_id,
    }
    try:
        path = Path(settings.data_dir) / "audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        logger.exception("failed to write audit log entry for %r", action)
