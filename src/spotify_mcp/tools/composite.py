"""Composite tools: resolve_tracks and build_playlist_from_candidates
(PLAN.md §7 — the "best pop songs right now" pipeline).

Neither tool has any opinion on what's currently popular or trending —
Spotify removed every signal that could answer that in 2026. Freshness
comes entirely from wherever the caller's candidate list came from (a web
search, typically). What these tools verify is narrower and more concrete:
whether a given {artist, title} genuinely matches a real Spotify catalog
entry, with the artist checked explicitly rather than trusted from title
text alone.
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel

from spotify_mcp.resolver.cache import ResolutionCache
from spotify_mcp.resolver.resolve import Candidate, Resolved, Unresolved, resolve_candidates
from spotify_mcp.server import AppContext
from spotify_mcp.tools import confirm
from spotify_mcp.tools._util import chunks, guarded

_RO_OPEN = ToolAnnotations(read_only_hint=True, open_world_hint=True)
_WRITE_OPEN = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)


class ResolvedTrack(BaseModel):
    input: dict[str, Any]
    uri: str
    track_id: str
    matched_artist: str
    matched_title: str
    isrc: str | None
    confidence: str
    method: str


class UnresolvedTrack(BaseModel):
    input: dict[str, Any]
    reason: str
    rejected_candidates: list[str]


class ResolveTracksResult(BaseModel):
    resolved: list[ResolvedTrack]
    unresolved: list[UnresolvedTrack]
    stats: dict[str, Any]


def _candidate_from_dict(d: dict[str, Any]) -> Candidate:
    return Candidate(artist=d["artist"], title=d["title"], album=d.get("album"), year=d.get("year"))


def _input_dict(c: Candidate) -> dict[str, Any]:
    return {"artist": c.artist, "title": c.title, "album": c.album, "year": c.year}


def _resolved_to_model(r: Resolved) -> ResolvedTrack:
    return ResolvedTrack(
        input=_input_dict(r.input),
        uri=r.uri,
        track_id=r.track_id,
        matched_artist=r.matched_artist,
        matched_title=r.matched_title,
        isrc=r.isrc,
        confidence=r.confidence,
        method=r.method,
    )


def _unresolved_to_model(u: Unresolved) -> UnresolvedTrack:
    return UnresolvedTrack(
        input=_input_dict(u.input), reason=u.reason, rejected_candidates=u.rejected_candidates
    )


def _validate_candidates(candidates: list[dict[str, Any]]) -> None:
    if not candidates:
        raise ToolError("candidates is empty.")
    for c in candidates:
        if not c.get("artist") or not c.get("title"):
            raise ToolError(f"Each candidate needs a non-empty 'artist' and 'title': {c!r}")


async def _resolve(app: AppContext, candidates: list[dict[str, Any]], strict: bool):
    cache = ResolutionCache(app.settings.resolver_cache_db_path)
    try:
        return await resolve_candidates(
            app.spotify, cache, [_candidate_from_dict(c) for c in candidates], strict=strict
        )
    finally:
        cache.close()


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        description=(
            "Resolve {artist, title} candidates to verified Spotify track URIs. Every input "
            "ends up in exactly one of resolved/unresolved — never silently substituted or "
            "dropped. Matching requires the artist to genuinely match (not just the title), "
            "which is what catches sped-up edits, karaoke covers, tribute recordings, and live "
            "versions that a title-only search would happily return instead of the real track. "
            "strict=true (default) only accepts exact/strong title matches; set false to also "
            "accept fuzzy 'weak' matches (still flagged as such) at the cost of precision. This "
            "tool has no opinion on what is currently popular — chart freshness is only as good "
            "as whatever produced the candidate list."
        ),
        annotations=_RO_OPEN,
    )
    @guarded
    async def resolve_tracks(
        ctx: Context, candidates: list[dict[str, Any]], strict: bool = True
    ) -> ResolveTracksResult:
        _validate_candidates(candidates)
        app: AppContext = ctx.request_context.lifespan_context
        outcome = await _resolve(app, candidates, strict)
        return ResolveTracksResult(
            resolved=[_resolved_to_model(r) for r in outcome.resolved],
            unresolved=[_unresolved_to_model(u) for u in outcome.unresolved],
            stats={
                "total": len(candidates),
                "resolved": len(outcome.resolved),
                "unresolved": len(outcome.unresolved),
                "resolution_rate": round(outcome.resolution_rate, 3),
            },
        )

    @mcp.tool(
        description=(
            "Resolve {artist, title} candidates and build a new playlist from whatever "
            "resolves, in one two-phase call (resolve -> create playlist -> add tracks). Chart "
            "freshness comes entirely from wherever the candidate list came from — this tool "
            "cannot tell you what's currently popular on Spotify's own authority; say so to the "
            "user rather than implying otherwise. The preview always shows both the resolved "
            "tracks and the unresolved list before anything is created, and is marked "
            "low_confidence if fewer than ~70% of candidates resolved. skip_unresolved=false "
            "refuses to create anything unless every single candidate resolves."
        ),
        annotations=_WRITE_OPEN,
    )
    @guarded
    async def build_playlist_from_candidates(
        ctx: Context,
        name: str,
        candidates: list[dict[str, Any]],
        description: str = "",
        public: bool = False,
        skip_unresolved: bool = True,
        confirm_token: str | None = None,
    ) -> confirm.WriteToolResult:
        _validate_candidates(candidates)
        app: AppContext = ctx.request_context.lifespan_context
        args = {
            "name": name,
            "candidates": candidates,
            "description": description,
            "public": public,
            "skip_unresolved": skip_unresolved,
        }

        outcome = await _resolve(app, candidates, strict=True)
        resolved_models = [_resolved_to_model(r) for r in outcome.resolved]
        unresolved_models = [_unresolved_to_model(u) for u in outcome.unresolved]

        if outcome.unresolved and not skip_unresolved:
            return confirm.WriteToolResult(
                status="executed",
                action="build_playlist_from_candidates",
                result={
                    "created": False,
                    "reason": (
                        f"{len(outcome.unresolved)} of {len(candidates)} candidates did not "
                        "resolve, and skip_unresolved=false. Nothing was created."
                    ),
                    "resolved": [m.model_dump() for m in resolved_models],
                    "unresolved": [m.model_dump() for m in unresolved_models],
                },
            )

        if not outcome.resolved:
            return confirm.WriteToolResult(
                status="executed",
                action="build_playlist_from_candidates",
                result={
                    "created": False,
                    "reason": "None of the candidates resolved to a Spotify track.",
                    "unresolved": [m.model_dump() for m in unresolved_models],
                },
            )

        preview = {
            "will_create": {"name": name, "description": description, "public": public},
            "resolved": [m.model_dump() for m in resolved_models],
            "unresolved": [m.model_dump() for m in unresolved_models],
            "counts": {
                "total_candidates": len(candidates),
                "resolved": len(outcome.resolved),
                "unresolved": len(outcome.unresolved),
                "resolution_rate": round(outcome.resolution_rate, 3),
            },
            "low_confidence": outcome.resolution_rate < app.settings.resolver_min_confidence_rate,
            "irreversible": False,
        }

        pending = await confirm.gate(
            ctx,
            app.confirmations,
            action="build_playlist_from_candidates",
            args=args,
            confirm_token=confirm_token,
            preview=preview,
            elicit_message=(
                f"Create playlist {name!r} with {len(outcome.resolved)} of {len(candidates)} "
                "candidates resolved? Review the unresolved list first."
            ),
        )
        if pending is not None:
            return pending

        create_result = await app.spotify.post(
            "/me/playlists", json={"name": name, "description": description, "public": public}
        )
        if create_result.get("dry_run"):
            confirm.write_audit(
                app.settings,
                action="build_playlist_from_candidates",
                args=args,
                token=confirm_token,
                outcome="dry_run",
            )
            return confirm.executed("build_playlist_from_candidates", create_result)

        playlist_id = create_result["id"]
        uris = [r.uri for r in outcome.resolved]
        snapshot_ids: list[str] = []
        for chunk in chunks(uris):
            add_result = await app.spotify.post(
                f"/playlists/{playlist_id}/items", json={"uris": chunk}
            )
            if add_result.get("snapshot_id"):
                snapshot_ids.append(add_result["snapshot_id"])

        confirm.write_audit(
            app.settings,
            action="build_playlist_from_candidates",
            args=args,
            token=confirm_token,
            outcome="executed",
            snapshot_id=snapshot_ids[-1] if snapshot_ids else None,
        )
        return confirm.executed(
            "build_playlist_from_candidates",
            {
                "created": True,
                "playlist_id": playlist_id,
                "playlist_url": (create_result.get("external_urls") or {}).get("spotify"),
                "added": len(uris),
                "unresolved": [m.model_dump() for m in unresolved_models],
            },
        )
