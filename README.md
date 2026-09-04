# spotify-mcp

A personal MCP server that lets Claude manage your Spotify playlists and
answer questions about your actual listening history. Built for one user,
running on your own machine — see [PLAN.md](PLAN.md) for the full design,
including the places where Spotify's 2026 API changes make something
outright impossible rather than just harder.

35 tools across four areas:

- **Reading your account** — playback state, top artists/tracks, recently
  played, saved tracks/albums, followed artists, playlists, catalog search,
  track/artist/album metadata.
- **Writing playlists** — create, add/replace/reorder items, edit details,
  set a cover image, remove items — every one previewed before it executes.
- **Local listening-history analytics** — summaries, hour-of-day/day-of-week
  breakdowns, skip rates, top artists/tracks/albums, taste drift between two
  periods, artists you've dropped, tracks worth rediscovering, and a guarded
  free-form SQL tool for anything the named ones don't cover. Built from
  your Spotify data export, not the API — the API has no analytics
  endpoints left.
- **Resolving a list into a playlist** — turn `{artist, title}` pairs (e.g.
  from a web search for "today's top songs") into verified Spotify tracks
  and, optionally, a finished playlist.

## Setup

1. **Create a Spotify app.** Go to the
   [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
   and create an app — Development Mode is fine, this is a personal
   project. Note its **Client ID**; no client secret is used (this server
   authorizes via PKCE).

   Two Spotify-side requirements as of 2026, not something this project
   controls: the account that owns the app needs **Spotify Premium**, and
   each Development Mode app is capped at **5 authorized users**. Every
   person running this server needs to create their own app and use their
   own Client ID — this repo never bakes one in.

2. **Register the redirect URI.** In the app's settings, add exactly:

   ```
   http://127.0.0.1:8888/callback
   ```

   (If you change `SPOTIFY_REDIRECT_PORT` in step 4, update this to match.)

3. **Install dependencies** (requires [uv](https://docs.astral.sh/uv/)):

   ```bash
   uv sync
   ```

4. **Configure.** Copy `.env.example` to `.env` and paste in your Client
   ID. The rest of the file is commented — the defaults are sensible for
   personal use, but at minimum consider setting `SPOTIFY_MCP_TIMEZONE`
   (see [Local analytics](#local-listening-history-analytics) below).

   ```bash
   cp .env.example .env
   ```

5. **Authorize.** Opens your browser for a one-time Spotify login and
   saves a refresh token locally:

   ```bash
   uv run spotify-mcp login
   ```

6. **Wire it into Claude Desktop.** Add to `claude_desktop_config.json`:

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

   Restart Claude Desktop. You should see all 35 tools available.

   To run it standalone instead (e.g. for the smoke tests below):

   ```bash
   uv run spotify-mcp serve
   ```

**A config change to `.env` only takes effect on the next restart of the
server process** — it reads the file once at startup and doesn't watch it.
If you change something and nothing seems different, restart the
connection (quit/reopen Claude Desktop, or reconnect the MCP server).

## Playlist writes and confirmation

Every write tool (`create_playlist`, `add_playlist_items`,
`replace_playlist_items`, `reorder_playlist_items`,
`update_playlist_details`, `set_playlist_cover`, `remove_playlist_items`,
`build_playlist_from_candidates`) is two-phase: call it once and it returns
a preview plus a one-time `confirm_token` — nothing is sent to Spotify yet.
Call it again with that token and the *exact same arguments* to actually
execute; changing anything invalidates the token, and a token can only be
used once regardless of whether that attempt succeeds. Claude should always
show you the preview before calling back with the token.

With `SPOTIFY_MCP_DRY_RUN=true` (the `.env.example` default), the second
call still runs the full confirmation flow but never reaches Spotify —
you'll get `"status": "dry_run"` back showing exactly what would have been
sent. Flip it to `false` once you trust what you're seeing.

`remove_playlist_items` is disabled by default — it's the one genuinely
destructive tool here. Set `SPOTIFY_MCP_ALLOW_REMOVALS=true` if you want it
available. `replace_playlist_items` also discards existing content and
always shows what would be lost in its preview, but isn't gated behind that
flag — rebuilding a playlist wholesale (e.g. refreshing a chart playlist
periodically) is a normal, intentional workflow.

A local audit log of every executed (or dry-run) write is kept at
`<user data dir>/spotify-mcp/audit.jsonl` — one JSON line per write, with a
timestamp, the action, a hash of its arguments, and the resulting
`snapshot_id` where relevant.

## Local listening-history analytics

Nine tools query a local DuckDB database built from your own data — none
of this comes from the Spotify API, which has no analytics endpoints left
as of 2026.

**Try it now with synthetic data**, before your real export arrives:

```bash
uv run spotify-mcp make-fixture ./fixture
uv run spotify-mcp ingest ./fixture
```

This generates ~8000 realistic plays across 26 months (artists that fade
out, artists that show up recently, a diurnal listening curve, a believable
skip pattern) into `<user data dir>/spotify-mcp/listening.duckdb`. Ask
Claude "when do I listen the most?" or "which artists have I dropped?" and
it'll answer from this synthetic data — good for seeing the tools work, not
for learning anything about your actual listening.

**Request your real data**: Spotify → Settings → Account → Privacy
settings → Request data → **Extended streaming history**. This can take
several weeks to arrive. It downloads as one or more `endsong_N.json` (or
similarly named) files — ingest reads every `.json` file in the directory
you point it at, so just extract the download and run:

```bash
uv run spotify-mcp ingest /path/to/your/spotify-export
```

This **replaces** whatever was ingested before (fixture or an earlier real
export) — the analytics tables are always fully rebuilt from everything
ever ingested. Re-running `ingest` on files you've already loaded is a
no-op (tracked by content hash), so it's safe to point it at the same
directory repeatedly, e.g. after Spotify sends you a newer export.

**Set your timezone** or hour-of-day/day-of-week results mean nothing —
without it, everything is bucketed in UTC:

```
SPOTIFY_MCP_TIMEZONE=Europe/Zagreb
```

(any IANA zone name). Re-run `ingest` after changing it — local-time
columns are computed at ingest time, not on the fly.

`query_listening_history` is a guarded free-form SQL tool for anything the
eight named tools don't cover — read `docs/analytics_schema.md` (or ask
Claude to call `describe_listening_data`) for the schema. It only accepts a
single read-only `SELECT`/`WITH` statement; stacked statements and
file-reading functions are rejected before they ever reach the database.

## Resolving tracks and building playlists from a list

`resolve_tracks` turns `{artist, title}` pairs into verified Spotify track
URIs. It never accepts a match on title text alone — the artist has to
genuinely match too, which is what stops a search for a popular song from
quietly handing back a karaoke cover, a sped-up edit, a tribute-band
recording, or a live version instead of the real track. Every input ends
up in exactly one of `resolved` or `unresolved`; nothing is silently
substituted or dropped.

`build_playlist_from_candidates` chains that resolution into creating a
playlist: give it a name and a candidate list, and it previews what would
resolve and what wouldn't before creating anything, then follows the same
confirm-token flow as every other write tool.

**Neither tool knows what's popular or trending** — Spotify removed every
signal that could answer that in 2026. For "make me a playlist of the best
pop songs right now," the chart itself has to come from somewhere else — a
web search, most naturally — and you hand the resulting list to
`build_playlist_from_candidates`. Claude should be upfront that the
freshness claim rests on whatever it found on the web, not on anything
Spotify itself is asserting.

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

| What | Path |
| --- | --- |
| OAuth token | `<user config dir>/spotify-mcp/token.json` (Windows: `%LOCALAPPDATA%\spotify-mcp\token.json`) |
| Listening-history DB | `<user data dir>/spotify-mcp/listening.duckdb` |
| Resolver cache | `<user data dir>/spotify-mcp/resolver_cache.duckdb` |
| Write audit log | `<user data dir>/spotify-mcp/audit.jsonl` |
| HTTP ETag cache | `<user cache dir>/spotify-mcp/http_etag_cache.sqlite3` |
| Logs | stderr, JSON lines (stdout is reserved for the MCP protocol) |

Never commit the token file, the databases, or `.env` — all gitignored
already.

## Development

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
```

All settings in `.env.example` map 1:1 to fields on `Settings` in
`src/spotify_mcp/config.py` — that file is the source of truth if the
example ever drifts.
