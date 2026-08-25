# spotify-mcp

A personal MCP server for managing your Spotify playlists and querying your
local listening-history analytics from Claude. See [PLAN.md](PLAN.md) for the
full design. This README currently covers **Phase 1** (auth + API client
skeleton), **Phase 2** (live read tools), and **Phase 3** (playlist writes) —
it will grow as later phases land.

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

Restart Claude Desktop. You should see 24 tools: `get_me`, `server_status`,
15 read-only tools (playback state, top artists/tracks, recently played,
saved tracks/albums, followed artists, playlists, catalog search,
track/artist/album metadata), and 7 playlist write tools.

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
workflow (Phase 5's chart-to-playlist pipeline will use it).

A local audit log of every executed (or dry-run) write is kept at
`<user data dir>/spotify-mcp/audit.jsonl` — one JSON line per write, with a
timestamp, the action, a hash of its arguments, and the resulting
`snapshot_id` where relevant.

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
- **Analytics DB** (Phase 4+): `<user data dir>/spotify-mcp/listening.duckdb`.
- **Logs**: stderr, JSON lines. stdout is reserved for the MCP protocol.

## Development

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
```

## Requesting your Extended Streaming History

For Phase 4 (local analytics), you'll need Spotify's Extended Streaming
History export: **Spotify → Settings → Account → Privacy settings → Request
data → Extended streaming history**. This can take several weeks to arrive —
worth requesting now.
