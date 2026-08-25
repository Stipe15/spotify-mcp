"""Hit each live endpoint once and record the ACTUAL response shape.

Ground truth over guesses (PLAN.md §10 C2/C6): the reference docs still show
`popularity`/`available_markets`/`preview_url` marked "Deprecated" rather
than gone, and Spotify's blog says Dev Mode is "limited to a smaller set of
supported endpoints" without publishing which. This script asks the API
itself, against this app's own Dev Mode tier, and writes the answer to
docs/observed_shapes.md.

Read-only: every call here is a GET. Safe to re-run at any time.

Usage: uv run python scripts/probe_api.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from spotify_mcp.api.client import SpotifyClient  # noqa: E402
from spotify_mcp.auth.manager import TokenManager  # noqa: E402
from spotify_mcp.config import Settings  # noqa: E402
from spotify_mcp.errors import SpotifyMCPError  # noqa: E402

# Substrings worth flagging explicitly: the fields the Feb/Mar 2026
# changelogs disputed, one way or another.
_WATCH_FIELDS = [
    "popularity",
    "available_markets",
    "preview_url",
    "followers",
    "label",
    "album_group",
    "linked_from",
    "external_ids",
    "email",
    "country",
    "product",
    "explicit_content",
]


class Probe:
    def __init__(self, client: SpotifyClient):
        self.client = client
        self.sections: list[str] = []

    async def call(self, label: str, method: str, path: str, **kwargs) -> dict:
        self.sections.append(f"## {label}\n\n`{method} {path}`")
        try:
            fn = getattr(self.client, method.lower())
            data = await fn(path, **kwargs)
        except SpotifyMCPError as exc:
            self.sections.append(f"\n**Error:** `{exc}`\n")
            return {}

        raw = json.dumps(data, indent=2, ensure_ascii=False)
        flags = self._flag_fields(raw)
        top_keys = sorted(data.keys()) if isinstance(data, dict) else []

        self.sections.append(f"\nTop-level keys: `{top_keys}`\n")
        if flags:
            self.sections.append("Watched-field presence:\n" + "\n".join(flags) + "\n")
        body = raw if len(raw) < 4000 else raw[:4000] + "\n... (truncated)"
        self.sections.append(
            f"\n<details><summary>Raw response</summary>\n\n```json\n{body}\n```\n\n</details>\n"
        )
        return data

    def _flag_fields(self, raw_json: str) -> list[str]:
        lines = []
        for field in _WATCH_FIELDS:
            needle = f'"{field}"'
            status = "PRESENT" if needle in raw_json else "absent"
            lines.append(f"- `{field}`: {status}")
        return lines

    def render(self) -> str:
        return "\n".join(self.sections)


async def main() -> None:
    settings = Settings.load()
    tokens = TokenManager(settings)
    if not tokens.is_authorized:
        print("Not authorized. Run `spotify-mcp login` first.", file=sys.stderr)
        sys.exit(1)

    client = SpotifyClient(settings, tokens)
    probe = Probe(client)

    try:
        await probe.call("GET /me", "GET", "/me")

        top_artists = await probe.call(
            "GET /me/top/artists", "GET", "/me/top/artists", params={"limit": 1}
        )
        top_tracks = await probe.call(
            "GET /me/top/tracks", "GET", "/me/top/tracks", params={"limit": 1}
        )
        await probe.call(
            "GET /me/player/recently-played",
            "GET",
            "/me/player/recently-played",
            params={"limit": 1},
        )
        await probe.call("GET /me/tracks", "GET", "/me/tracks", params={"limit": 1})
        await probe.call("GET /me/albums", "GET", "/me/albums", params={"limit": 1})
        await probe.call(
            "GET /me/following",
            "GET",
            "/me/following",
            params={"type": "artist", "limit": 1},
        )
        my_playlists = await probe.call(
            "GET /me/playlists", "GET", "/me/playlists", params={"limit": 1}
        )
        await probe.call("GET /me/player", "GET", "/me/player")

        playlist_items = (my_playlists or {}).get("items") or []
        if playlist_items:
            playlist_id = playlist_items[0]["id"]
            await probe.call(
                "GET /playlists/{id} (raw, no fields filter — checking items vs tracks)",
                "GET",
                f"/playlists/{playlist_id}",
            )
            await probe.call(
                "GET /playlists/{id}/items",
                "GET",
                f"/playlists/{playlist_id}/items",
                params={"limit": 1},
            )
        else:
            probe.sections.append(
                "## GET /playlists/{id} and /playlists/{id}/items\n\n"
                "Skipped: no playlists in this account to probe with.\n"
            )

        search = await probe.call(
            "GET /search",
            "GET",
            "/search",
            params={
                "q": 'track:"Blinding Lights" artist:"The Weeknd"',
                "type": "track",
                "limit": 1,
            },
        )

        track_id = None
        artist_id = None
        search_tracks = ((search or {}).get("tracks") or {}).get("items") or []
        if search_tracks:
            track_id = search_tracks[0]["id"]
            artist_id = (search_tracks[0].get("artists") or [{}])[0].get("id")
        elif (top_tracks or {}).get("items"):
            track_id = top_tracks["items"][0]["id"]
            artist_id = (top_tracks["items"][0].get("artists") or [{}])[0].get("id")
        elif (top_artists or {}).get("items"):
            artist_id = top_artists["items"][0]["id"]

        if track_id:
            await probe.call("GET /tracks/{id}", "GET", f"/tracks/{track_id}")
        else:
            probe.sections.append("## GET /tracks/{id}\n\nSkipped: no track id available.\n")

        album_id = None
        if artist_id:
            artist_data = await probe.call("GET /artists/{id}", "GET", f"/artists/{artist_id}")
            albums = await probe.call(
                "GET /artists/{id}/albums",
                "GET",
                f"/artists/{artist_id}/albums",
                params={"limit": 1},
            )
            album_items = (albums or {}).get("items") or []
            if album_items:
                album_id = album_items[0]["id"]
            del artist_data
        else:
            probe.sections.append(
                "## GET /artists/{id} and /artists/{id}/albums\n\nSkipped: no artist id.\n"
            )

        if album_id:
            await probe.call(
                "GET /albums/{id}/tracks", "GET", f"/albums/{album_id}/tracks", params={"limit": 1}
            )
        else:
            probe.sections.append("## GET /albums/{id}/tracks\n\nSkipped: no album id.\n")

    finally:
        await client.aclose()
        await tokens.aclose()

    out_path = Path(__file__).resolve().parent.parent / "docs" / "observed_shapes.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Observed API response shapes\n\n"
        "Generated by `scripts/probe_api.py` against this app's actual Dev Mode tier. "
        "Ground truth, not the reference docs — see PLAN.md §10 C2/C6. "
        "Re-run any time; this file is gitignored (may contain this account's own data).\n\n"
    )
    out_path.write_text(header + probe.render(), encoding="utf-8")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
