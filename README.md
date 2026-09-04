# spotify-mcp

A personal MCP server for managing your Spotify playlists and querying your
local listening-history analytics from Claude. See [PLAN.md](PLAN.md) for the
full design. This README currently covers **Phase 1** (auth + API client
skeleton), **Phase 2** (live read tools), **Phase 3** (playlist writes),
**Phase 4** (local listening-history analytics), and **Phase 5** (the
artist/title resolver and chart-to-playlist pipeline) — it will grow as
later phases land.

## Setup

1. **Create a Spotify app.** Go to the
   [Spotify Developer Dashboard](https://developer.spotify.com/dashboard),
   create an app (Development Mode is fine — this is a personal project), and
   note its **Client ID**. No client secret is needed; this server uses the
   PKCE flow.

2. **Register the redirect URI.** In the app's settings, add exactly:

   ```
   http://127.0.0.1:8888/callback
   ```

   (If you change `SPOTIFY_REDIRECT_PORT` below, update this to match.)

3. **Install dependencies** (requires [uv](https://docs.astral.sh/uv/)):

   ```bash
   uv sync
   ```

4. **Configure.** Copy `.env.example` to `.env` and paste in your Client ID:

   ```bash
   cp .env.example .env
   ```

5. **Authorize.** This opens your browser for a one-time Spotify login and
   saves a refresh token locally:

   ```bash
   uv run spotify-mcp login
   ```

6. **Run the server** over stdio:

   ```bash
   uv run spotify-mcp serve
   ```

   With `SPOTIFY_MCP_DRY_RUN=true` (the `.env.example` default), no write
   ever reaches Spotify — writes are logged instead, and every write tool's
   response says `"status": "dry_run"` so it's unmistakable. Flip it to
   `false` once you've verified previews look right.

## Wiring into Claude Desktop

Add to your Claude Desktop MCP config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "spotify": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/spotify-mcp", "spotify-mcp", "serve"]
    }
  }
}
```

Restart Claude Desktop. You should see 35 tools: `get_me`, `server_status`,
15 read-only Spotify tools (playback state, top artists/tracks, recently
played, saved tracks/albums, followed artists, playlists, catalog search,
track/artist/album metadata), 7 playlist write tools, 9 local-analytics
tools over your listening history, and 2 composite tools (`resolve_tracks`,
`build_playlist_from_candidates`) that turn a list of {artist, title} pairs
into verified Spotify tracks and, optionally, a finished playlist.

## Playlist writes and confirmation

Every write tool (`create_playlist`, `add_playlist_items`,
`replace_playlist_items`, `reorder_playlist_items`,
`update_playlist_details`, `set_playlist_cover`, `remove_playlist_items`) is
two-phase: call it once and it returns a preview plus a one-time
`confirm_token` — nothing is sent to Spotify yet. Call it again with that
token and the *exact same arguments* to actually execute; changing anything
invalidates the token, and a token can only be used once regardless of
whether that attempt succeeds. Claude should always show you the preview
before calling back with the token.

`remove_playlist_items` is disabled by default — it's the one genuinely
destructive tool here (deleting items from a playlist). Set
`SPOTIFY_MCP_ALLOW_REMOVALS=true` in `.env` if you want it available.
`replace_playlist_items` also discards existing content and always shows
what would be lost in its preview, but isn't gated behind that flag —
rebuilding a playlist's contents wholesale is an intentional, ordinary
workflow (e.g. refreshing a chart playlist you rebuild periodically).

A local audit log of every executed (or dry-run) write is kept at
`<user data dir>/spotify-mcp/audit.jsonl` — one JSON line per write, with a
timestamp, the action, a hash of its arguments, and the resulting
`snapshot_id` where relevant.

## Local listening-history analytics

Nine tools query a local DuckDB database built from your Spotify data —
listening summaries, hour-of-day/day-of-week breakdowns, skip rates, top
artists/tracks/albums, taste drift between two periods, artists you've
dropped, tracks worth rediscovering, plus a guarded free-form SQL escape
hatch (`query_listening_history`) for anything the named tools don't cover.
None of this comes from the Spotify API — it's entirely local, built from
your own export.

**Try it now with synthetic data**, before your real export arrives:

```bash
uv run spotify-mcp make-fixture ./fixture
uv run spotify-mcp ingest ./fixture
```

This generates ~8000 realistic plays across 26 months (with artists that
fade out, artists that show up recently, a diurnal listening curve, and a
believable skip pattern) and loads them into
`<user data dir>/spotify-mcp/listening.duckdb`. Ask Claude things like "when
do I listen the most?" or "which artists have I dropped?" and it'll answer
from this synthetic data — useful for seeing the tools work, not for
learning anything about your actual listening.

**When your real export arrives** (see below), ingest it the same way:

```bash
uv run spotify-mcp ingest /path/to/your/spotify-export
```

This **replaces** whatever was ingested before (fixture or a previous real
export) — the analytics tables are always fully rebuilt from everything
ever ingested. Re-running `ingest` on files you've already loaded is a
no-op (tracked by content hash), so it's safe to point it at the same
directory repeatedly, e.g. after adding newer export files.

**Set your timezone** for hour-of-day/day-of-week results to mean anything —
without it, everything is bucketed in UTC:

```
SPOTIFY_MCP_TIMEZONE=Europe/Zagreb
```

(any IANA zone name, e.g. `America/New_York`). Re-run `ingest` after
changing it — the local-time columns are computed at ingest time, not on
the fly.

## Resolving tracks and building playlists from a list

`resolve_tracks` turns a list of `{artist, title}` pairs into verified
Spotify track URIs. It never accepts a match on title text alone — the
artist has to genuinely match too, which is what stops a search for a
popular song from quietly handing back a karaoke cover, a sped-up TikTok
edit, a tribute-band recording, or a live version instead of the real
track. Every input ends up in exactly one of `resolved` or `unresolved`;
nothing is ever silently substituted or dropped.

`build_playlist_from_candidates` chains that resolution straight into
creating a playlist: give it a name and a candidate list, and it previews
what would resolve and what wouldn't before creating anything, then follows
the same confirm-token flow as every other write tool.

**Neither tool knows what's popular or trending** — Spotify removed every
signal that could answer that in 2026 (no `/recommendations`, no
`/browse/new-releases`, no `popularity` field). For "make me a playlist of
the best pop songs right now," the actual chart has to come from somewhere
else — a web search, most naturally — and you hand the resulting
`{artist, title}` list to `build_playlist_from_candidates`. Ask Claude to
do this and it should be upfront that the freshness claim rests on whatever
it found on the web, not on anything Spotify itself is asserting.

Resolutions are cached (by normalized artist|title) in
`<user data dir>/spotify-mcp/resolver_cache.duckdb`, so re-running the same
candidate list — e.g. after fixing a couple of unresolved entries — doesn't
re-search everything from scratch.

## Checking what the API actually returns

Spotify's Web API changed substantially in 2026, and the reference docs
still show some fields (`popularity`, `preview_url`, ...) marked
"Deprecated" rather than confirming they're gone. Rather than guess, this
repo checks empirically:

```bash
uv run python scripts/probe_api.py
```

This hits each endpoint once with your authorized account (read-only) and
writes `docs/observed_shapes.md` — the actual response shapes this app's
Dev Mode tier receives. That file is gitignored (it can contain your own
playlist/listening data); re-run it any time you want current ground truth
instead of relying on this repo's models being right.

## Where things live

- **Token**: `<user config dir>/spotify-mcp/token.json` (Windows:
  `%LOCALAPPDATA%\spotify-mcp\token.json`). Never commit this.
- **Analytics DB**: `<user data dir>/spotify-mcp/listening.duckdb` (Windows:
  same `%LOCALAPPDATA%\spotify-mcp\` folder).
- **Resolver cache**: `<user data dir>/spotify-mcp/resolver_cache.duckdb`.
- **Write audit log**: `<user data dir>/spotify-mcp/audit.jsonl`.
- **Logs**: stderr, JSON lines. stdout is reserved for the MCP protocol.

## Development

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
```

## Requesting your Extended Streaming History

For real (non-synthetic) analytics, you'll need Spotify's Extended Streaming
History export: **Spotify → Settings → Account → Privacy settings → Request
data → Extended streaming history**. This can take several weeks to arrive.
It downloads as one or more `endsong_N.json` files (or similarly named) — the
ingest command reads every `.json` file in the directory you point it at, so
just extract the download and run `spotify-mcp ingest` on that folder.
