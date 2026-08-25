# spotify-mcp — Plan (Phase 0)

A personal, single-user MCP server for playlist management and listening analytics against
the post-February-2026 Spotify Web API.

**Status:** proposal. No implementation code written yet.

---

## 0. What I verified before writing this

I read the three pages you named, plus five more that turned out to matter:

| Source | Why it mattered |
| --- | --- |
| [Changelog — February 2026](https://developer.spotify.com/documentation/web-api/references/changes/february-2026) | Confirms every removal/rename in your brief |
| [Changelog — March 2026](https://developer.spotify.com/documentation/web-api/references/changes/march-2026) | `external_ids` reverted on **Track** and **Album** only |
| [Blog — Update on developer access](https://developer.spotify.com/blog/2026-02-06-update-on-developer-access-and-platform-security) | Premium required, 5 users, Dev Mode limits |
| [**February 2026 Migration Guide**](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide) | Not in your list. Contains the authoritative before/after mappings and one fact newer than the blog post |
| [Get Playlist Items](https://developer.spotify.com/documentation/web-api/reference/get-playlists-items) | **403 for playlists you don't own** — a constraint your brief doesn't mention |
| [Search](https://developer.spotify.com/documentation/web-api/reference/search) | limit max 10, offset max 1000, `isrc:` filter supported |
| [Scopes](https://developer.spotify.com/documentation/web-api/concepts/scopes) | Scope list is stale relative to the field removals |
| [Code w/ PKCE flow](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow) | No client secret; refresh token may not rotate |
| [Rate limits](https://developer.spotify.com/documentation/web-api/concepts/rate-limits) / [API calls](https://developer.spotify.com/documentation/web-api/concepts/api-calls) | Rolling 30s window, numbers undisclosed; ETag support |

Your brief was accurate on essentially everything. The deltas are in §10.

---

## 1. Repo layout

```
spotify-mcp/
├── PLAN.md
├── README.md
├── pyproject.toml               # uv-managed, project metadata + deps + ruff config
├── uv.lock
├── .env.example                 # SPOTIFY_CLIENT_ID, redirect URI, paths
├── .gitignore                   # .env, *.duckdb, tokens/, .cache/
├── config.example.toml          # non-secret runtime config (dry_run, timezone, limits)
│
├── src/spotify_mcp/
│   ├── __init__.py
│   ├── __main__.py              # CLI: `serve`, `login`, `ingest`, `make-fixture`, `probe`
│   ├── config.py                # Settings (pydantic), .env + TOML merge, path resolution
│   ├── errors.py                # typed error hierarchy → clean MCP tool errors
│   ├── logging.py               # structured JSON logs to stderr (stdout is the MCP channel)
│   ├── server.py                # MCPServer construction, lifespan, tool registration
│   │
│   ├── auth/
│   │   ├── pkce.py              # verifier/challenge generation, authorize URL builder
│   │   ├── loopback.py          # one-shot 127.0.0.1 HTTP listener for the redirect
│   │   ├── store.py             # token file read/write + OS permission hardening
│   │   └── manager.py           # TokenManager: expiry tracking, single-flight refresh
│   │
│   ├── api/
│   │   ├── client.py            # SpotifyClient: auth injection, retries, 429, dry-run
│   │   ├── ratelimit.py         # concurrency semaphore + token bucket
│   │   ├── cache.py             # ETag / If-None-Match + on-disk GET cache
│   │   ├── models.py            # pydantic models for the *2026* response shapes
│   │   └── endpoints.py         # one thin typed function per live endpoint
│   │
│   ├── resolver/
│   │   ├── normalize.py         # artist/title normalization + variant detection
│   │   ├── match.py             # candidate scoring, artist-ID verification (pure, testable)
│   │   ├── cache.py             # on-disk resolution cache keyed by normalized artist|title
│   │   └── resolve.py           # orchestration, bounded concurrency, unresolved reporting
│   │
│   ├── analytics/
│   │   ├── schema.sql           # DDL for raw + normalized layers and views
│   │   ├── ingest.py            # export JSON → raw_plays → plays, re-runnable
│   │   ├── schema_check.py      # observed-vs-expected columns, loud failure
│   │   ├── sqlguard.py          # sqlglot-based validation for text-to-SQL
│   │   ├── queries.py           # named parameterized analytics queries
│   │   └── fixture.py           # synthetic streaming-history generator
│   │
│   └── tools/
│       ├── confirm.py           # two-phase confirmation + elicitation, dry-run gate
│       ├── system.py            # get_me, server_status
│       ├── read_live.py         # live API read tools
│       ├── read_local.py        # DuckDB analytics tools
│       ├── write_playlists.py   # playlist mutations
│       └── composite.py         # resolve_tracks, build_playlist_from_candidates
│
├── scripts/
│   └── probe_api.py             # hit each live endpoint once, record ACTUAL response keys
│
├── docs/
│   ├── observed_shapes.md       # generated by probe_api.py — ground truth, not guesses
│   └── analytics_schema.md      # the schema doc handed to the model for text-to-SQL
│
└── tests/
    ├── conftest.py
    ├── fixtures/*.json          # captured/synthetic API responses
    ├── test_pkce.py
    ├── test_token_manager.py    # refresh single-flight, expiry
    ├── test_client.py           # 429 + Retry-After, backoff, dry-run
    ├── test_resolver_match.py   # ← primary correctness target
    ├── test_sqlguard.py         # ← primary safety target
    ├── test_ingest.py           # schema tolerance + loud failure
    └── test_analytics.py        # ← primary correctness target
```

**Responsibility rule:** `api/endpoints.py` knows HTTP, `tools/*` knows MCP, and neither knows
the other's vocabulary. `resolver/match.py` and `analytics/sqlguard.py` are pure functions with
no I/O, because those are the two places bugs hide and I want them testable without mocks.

---

## 2. Tool surface

Notation: `→` names the backing endpoint or local query. All tools return structured
(pydantic-modelled) output, not prose. `RO` = `read_only_hint`, `D` = `destructive_hint`.

### 2.1 System

| Tool | Args | Returns | Backed by |
| --- | --- | --- | --- |
| `get_me` `RO` | — | `{id, display_name, uri, followers, external_urls, images}` | `GET /me` |
| `server_status` `RO` | — | `{dry_run, scopes_granted, token_expires_in_s, analytics_db: {present, rows, date_range}, cache_stats}` | local |

> `get_me` deliberately does **not** promise `email`, `country`, `product` — removed Feb 2026.
> **Phase 2 update:** `docs/observed_shapes.md` (generated by `scripts/probe_api.py` against
> the live account) shows `followers` is still present on `GET /me`, contradicting the Feb 2026
> changelog's claim that it was removed from the user object. The model was updated to include
> it — ground truth from the probe overrides the static changelog text. `email`/`country`/
> `product`/`explicit_content` are confirmed genuinely absent.

### 2.2 Live reads

| Tool | Args | Returns | Backed by |
| --- | --- | --- | --- |
| `get_playback_state` `RO` | — | `{is_playing, item, progress_ms, device, shuffle_state, repeat_state}` or `{active: false}` | `GET /me/player` |
| `get_top_artists` / `get_top_tracks` `RO` | `time_range: "short_term"\|"medium_term"\|"long_term"`, `limit≤50`, `offset` | `Paging[Artist]` / `Paging[Track]` | `GET /me/top/{type}` |
| `get_recently_played` `RO` | `limit≤50`, `after_ms?`, `before_ms?` | `{items[{track, played_at, context}], cursors}` | `GET /me/player/recently-played` |
| `get_saved_tracks` `RO` | `limit≤50`, `offset` | `{items[{added_at, track}], total}` | `GET /me/tracks` |
| `get_saved_albums` `RO` | `limit≤50`, `offset` | `{items[], total}` | `GET /me/albums` |
| `get_followed_artists` `RO` | `limit≤50`, `after?` | `{artists[], cursors, total}` | `GET /me/following?type=artist` |
| `get_my_playlists` `RO` | `limit≤50`, `offset` | `{items[{id, name, owner, item_count, snapshot_id}], total}` | `GET /me/playlists` |
| `get_playlist` `RO` | `playlist_id` | `{id, name, description, owner, snapshot_id, item_count, images}` | `GET /playlists/{id}` |
| `get_playlist_items` `RO` | `playlist_id`, `limit≤50`, `offset`, `fields?` | `{items[{added_at, item}], total}` | `GET /playlists/{id}/items` |
| `search_catalog` `RO` | `track?`, `artist?`, `album?`, `year?`, `isrc?`, `free_text?`, `type[]`, `limit≤10`, `offset≤1000` | `{results_by_type, query_sent}` | `GET /search` |
| `get_track` `RO` | `track_id` | full track object incl. `external_ids.isrc` | `GET /tracks/{id}` |
| `get_artist` `RO` | `artist_id` | artist object | `GET /artists/{id}` |
| `get_artist_albums` `RO` | `artist_id`, `include_groups?`, `limit≤50`, `offset` | `{items[], total}` | `GET /artists/{id}/albums` |
| `get_album_tracks` `RO` | `album_id`, `limit≤50`, `offset` | `{items[], total}` | `GET /albums/{id}/tracks` |

> **Phase 2 update:** `get_top_items` shipped as two tools, `get_top_artists`/`get_top_tracks`,
> not one dispatched by a `type` arg. The MCP SDK's structured-output path (`func_metadata.py`)
> wraps `Union` return types in `{"result": ...}`; a single concrete Pydantic model per tool
> returns the clean, unwrapped shape instead. `search_catalog` (§2.2 below) returns `SearchResults`
> — a single concrete model with one optional field per requested type — for the same reason.
>
> Also observed empirically (`docs/observed_shapes.md`): the same `search_catalog` query
> ("Blinding Lights"/"The Weeknd") returned a *different* track id across two separate calls —
> Spotify's search index appears non-deterministic between requests. This validates rather than
> undermines the resolver design in §7: matching must verify the returned artist id, never trust
> that identical queries return identical results.

Two things go **in the tool descriptions verbatim**, so the model reads them instead of
discovering them by failing:

- `search_catalog`: *"`limit` is capped at 10 (Spotify reduced this from 50 in Feb 2026). Prefer
  field-filtered queries (`track:`/`artist:`) over free text. There is no popularity or
  relevance score in the response — do not claim a result is 'the most popular'."*
- `get_playlist_items`: *"Only works for playlists you own or collaborate on. Any other playlist,
  including Spotify editorial playlists, returns 403. `GET /playlists/{id}` on a playlist you
  don't own returns metadata with no `items` object."*

**Not exposed, deliberately:** batch multi-ID lookups (gone), `get_artist_top_tracks` (gone),
recommendations / new-releases / categories (gone), audio features (gone), any
`GET /users/{id}*` (gone), library writes (`PUT`/`DELETE /me/library`) and follow/unfollow —
those are destructive account mutations outside the two things you asked for. Playback
*control* (`user-modify-playback-state`) is also out of v1; say the word and it's a small add.

### 2.3 Local analytics (DuckDB)

| Tool | Args | Returns | Backed by |
| --- | --- | --- | --- |
| `describe_listening_data` `RO` | — | full schema, column semantics, caveats, 6 example queries | `docs/analytics_schema.md` |
| `listening_summary` `RO` | `start?`, `end?` | totals, distinct tracks/artists, hours, date range | `queries.py` |
| `listening_by_hour` `RO` | `start?`, `end?`, `by: "hour"\|"dow"\|"hour_dow"` | play counts + ms by local hour/weekday | `queries.py` |
| `skip_stats` `RO` | `group_by: "artist"\|"track"\|"month"`, `min_plays`, `limit` | plays, skips, skip_rate, mean ms_played | `queries.py` |
| `top_local` `RO` | `entity: "artist"\|"track"\|"album"`, `start?`, `end?`, `metric: "plays"\|"ms"`, `limit` | ranked rows | `queries.py` |
| `taste_drift` `RO` | `period_a: [start,end]`, `period_b: [start,end]`, `entity`, `limit` | entities gained/lost/held with rank + share deltas | `queries.py` |
| `dropped_artists` `RO` | `active_window`, `silent_since`, `min_plays`, `limit` | artists heavily played then abandoned | `queries.py` |
| `rediscover_tracks` `RO` | `played_during: [start,end]`, `not_since`, `min_plays`, `limit` | **this is your "40 tracks from 2023" query** — returns track URIs ready for a playlist | `queries.py` |
| `query_listening_history` `RO` | `sql`, `max_rows≤5000` | `{columns, rows, row_count, truncated, sql_executed}` | guarded text-to-SQL |

### 2.4 Playlist writes — all two-phase

Every one takes an optional `confirm_token`. Called without it, the tool **executes nothing**
and returns a preview. See §6.

| Tool | Args | Preview shows | Backed by |
| --- | --- | --- | --- |
| `create_playlist` | `name`, `description?`, `public=false`, `collaborative=false`, `confirm_token?` | name, visibility, that it will be empty | `POST /me/playlists` |
| `add_playlist_items` | `playlist_id`, `uris[]`, `position?`, `confirm_token?` | target playlist name + current size, full track list to be added, resulting size, batch count | `POST /playlists/{id}/items` (chunked at 100) |
| `replace_playlist_items` | `playlist_id`, `uris[]`, `confirm_token?` | **what will be lost** (current contents) vs. what replaces it | `PUT /playlists/{id}/items` (replace mode) |
| `reorder_playlist_items` | `playlist_id`, `range_start`, `insert_before`, `range_length=1`, `snapshot_id?`, `confirm_token?` | the moved slice, before/after positions | `PUT /playlists/{id}/items` (reorder mode) |
| `update_playlist_details` | `playlist_id`, `name?`, `description?`, `public?`, `confirm_token?` | field-by-field old → new diff | `PUT /playlists/{id}` |
| `set_playlist_cover` | `playlist_id`, `image_path`, `confirm_token?` | file, dimensions, encoded size vs. the 256 KB cap | `PUT /playlists/{id}/images` |
| `remove_playlist_items` `D` | `playlist_id`, `uris[]`, `snapshot_id` (**required**), `confirm_token?` | exact items removed, positions, resulting size | `DELETE /playlists/{id}/items` |

`remove_playlist_items` is additionally gated: disabled unless `allow_removals = true` in
config, requires an explicit `snapshot_id` the model must have fetched first, and its
description states it must never be invoked from a general instruction. Per your guardrail,
there is no "clean up my library" path to it.

Description text for `add_playlist_items`: *"Spotify accepts at most 100 URIs per request; this
tool chunks automatically but each chunk is a separate call, so 250 tracks = 3 calls. Adding is
append-only and does not deduplicate — check `get_playlist_items` first if duplicates matter."*

### 2.5 Composite

| Tool | Args | Returns |
| --- | --- | --- |
| `resolve_tracks` `RO` | `candidates: [{artist, title, album?, year?}]`, `strict=true` | `{resolved: [{input, uri, track_id, matched_artist, matched_title, isrc, confidence, method}], unresolved: [{input, reason, rejected_candidates}], stats}` |
| `build_playlist_from_candidates` | `name`, `candidates[]`, `description?`, `public=false`, `skip_unresolved=true`, `confirm_token?` | preview = resolution table + unresolved list + the exact playlist to be created; on confirm → `{playlist_url, added, unresolved}` |

`build_playlist_from_candidates` **always surfaces the unresolved list in the preview**, before
anything is created. If resolution rate is below a configurable floor (default 70%), the preview
is marked `low_confidence` and says so.

---

## 3. Auth design

**Flow:** Authorization Code + PKCE. No client secret anywhere in the repo or on disk.

```
login (CLI, one time)
  1. verifier = base64url(random 64 bytes)        # 43–128 chars
     challenge = base64url(sha256(verifier))
  2. bind a one-shot HTTP listener on 127.0.0.1:<port>
  3. open browser → https://accounts.spotify.com/authorize
       ?response_type=code&client_id=…&redirect_uri=http://127.0.0.1:<port>/callback
       &code_challenge_method=S256&code_challenge=…&scope=…&state=<random>
  4. listener receives ?code&state → verify state, respond with a plain "you can close this"
     page, shut down immediately
  5. POST https://accounts.spotify.com/api/token
       grant_type=authorization_code&code&redirect_uri&client_id&code_verifier
  6. persist {access_token, refresh_token, expires_at, scope, obtained_at}
```

`redirect_uri` is `http://127.0.0.1:<port>/callback` with a **fixed port from config**, because
Spotify requires an exact registered match — an ephemeral port can't work. Default `8888`.
I use `127.0.0.1` rather than `localhost` since the PKCE tutorial's own example uses the
literal loopback IP.

**Refresh, with stampede control:**

```python
async def bearer(self) -> str:
    if not self._expiring_soon():  # 60s skew margin
        return self._access_token
    async with self._refresh_lock:  # asyncio.Lock — single-flight
        if not self._expiring_soon():  # re-check: another waiter already refreshed
            return self._access_token
        await self._do_refresh()
        return self._access_token
```

Ten concurrent tool calls hitting an expired token produce exactly one refresh request. The
docs state the refresh response *may omit* a new `refresh_token`, so `_do_refresh` retains the
existing one unless a replacement is returned. A `400 invalid_grant` clears the token file and
returns an actionable error telling you to re-run `login` — it does not retry.

**Token storage:** `<user_config_dir>/spotify-mcp/token.json` via `platformdirs`
(`%LOCALAPPDATA%\spotify-mcp\` on your machine), written atomically (temp file + replace).

Permission hardening is **OS-dependent and this is where the plan is weakest on Windows** —
see risk R5. POSIX gets `os.chmod(0o600)`. Windows gets an ACL reset via
`icacls <file> /inheritance:r /grant:r "<user>":F`, verified after write, with a loud warning
logged if it fails. Note that `os.chmod(0o600)` on Windows only toggles the read-only bit and
provides **no** access control; I will not pretend otherwise in the code or the README.

### Scopes requested, and why each

| Scope | Justification |
| --- | --- |
| `playlist-read-private` | `GET /me/playlists` private entries **and** required by `GET /playlists/{id}/items` |
| `playlist-read-collaborative` | include collaborative playlists in the above |
| `playlist-modify-private` | create + modify private playlists (the default for everything we build) |
| `playlist-modify-public` | needed only if you ever set `public=true`; requested so it isn't a re-auth later |
| `ugc-image-upload` | `PUT /playlists/{id}/images` cover art |
| `user-top-read` | `GET /me/top/{artists,tracks}` |
| `user-read-recently-played` | `GET /me/player/recently-played` |
| `user-library-read` | `GET /me/tracks`, `GET /me/albums` |
| `user-follow-read` | `GET /me/following` |
| `user-read-playback-state` | `GET /me/player` — current playback + device |

**Deliberately not requested:**

- `user-read-email`, `user-read-private` — the scopes still exist, but every field they gated
  (`email`, `product`, `country`, `explicit_content`) was removed from the user object in
  Feb 2026. Requesting them buys nothing and costs consent-screen surface area.
- `user-library-modify`, `user-follow-modify` — no library writes in scope.
- `user-modify-playback-state`, `streaming`, `app-remote-control` — no playback control in v1.

---

## 4. Data model — local analytics store

Single file, `<data_dir>/spotify-mcp/listening.duckdb`. Three layers.

### Layer 1 — raw (never mutated after ingest)

```sql
CREATE TABLE raw_plays (
  ingest_run_id  BIGINT NOT NULL,
  source_file    VARCHAR NOT NULL,
  source_index   BIGINT  NOT NULL,     -- position within the file
  payload        JSON    NOT NULL,     -- the object, verbatim, every field
  PRIMARY KEY (ingest_run_id, source_file, source_index)
);

CREATE TABLE ingest_runs (
  run_id BIGINT PRIMARY KEY, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
  file_count INT, row_count BIGINT,
  schema_fingerprint VARCHAR,          -- sorted hash of observed top-level keys
  unknown_columns VARCHAR[],           -- fields present in data, absent from expectations
  missing_columns VARCHAR[],           -- expected, absent from data
  status VARCHAR,                      -- ok | ok_with_warnings | failed
  notes VARCHAR
);
```

Keeping the verbatim payload means a schema surprise never costs you the data — re-derive
Layer 2 without re-reading the export.

### Layer 2 — normalized fact table

```sql
CREATE TABLE plays (
  play_id          BIGINT PRIMARY KEY,
  ts_utc           TIMESTAMPTZ NOT NULL,   -- from `ts`; end-of-play instant
  ts_local         TIMESTAMP   NOT NULL,   -- ts_utc AT TIME ZONE <config.local_timezone>
  ms_played        BIGINT      NOT NULL,

  track_name       VARCHAR,                -- master_metadata_track_name
  artist_name      VARCHAR,                -- master_metadata_album_artist_name
  album_name       VARCHAR,                -- master_metadata_album_album_name
  track_uri        VARCHAR,                -- spotify_track_uri
  track_id         VARCHAR,                -- tail of the URI, for joining to the API

  episode_name     VARCHAR,
  episode_show     VARCHAR,
  episode_uri      VARCHAR,
  content_type     VARCHAR NOT NULL,       -- 'music' | 'podcast' | 'unknown'

  reason_start     VARCHAR,
  reason_end       VARCHAR,
  shuffle          BOOLEAN,
  skipped_raw      BOOLEAN,                -- the export's own field; often NULL — see below
  offline          BOOLEAN,
  incognito        BOOLEAN,
  platform         VARCHAR,
  conn_country     VARCHAR,

  -- derived
  played_date      DATE,
  played_year      SMALLINT,
  played_month     DATE,                   -- month bucket
  played_hour      TINYINT,                -- local hour 0–23
  played_dow       TINYINT,                -- local ISO weekday 1–7
  is_skip          BOOLEAN,                -- derived; see below
  is_substantial   BOOLEAN                 -- ms_played >= config.substantial_ms (default 30000)
);
```

`ip_addr` and `user_agent` exist in the export (Spotify's own field list mentions both) and are
**dropped at Layer 2** — they're in `raw_plays.payload` if ever needed, but no analytics query
should touch them.

### Layer 3 — dimensions + views

```sql
CREATE TABLE dim_track  (track_uri PK, track_name, artist_name, album_name,
                         first_played, last_played, play_count, total_ms,
                         duration_ms /* NULL until API-enriched — see R6 */);
CREATE TABLE dim_artist (artist_name PK, first_played, last_played,
                         play_count, total_ms, distinct_tracks);

CREATE VIEW v_monthly_artist  AS ...;   -- artist × month plays/ms/share  → taste_drift
CREATE VIEW v_hourly          AS ...;   -- local hour × dow              → listening_by_hour
CREATE VIEW v_skip            AS ...;   -- per track/artist skip aggregates
```

### The skip definition — the one modelling decision that matters

The export's `skipped` field is unreliable and frequently null. `duration_ms` is **not in the
export at all**, so a true completion ratio isn't computable from local data alone. So:

```sql
is_skip = (reason_end = 'fwdbtn' AND ms_played < config.skip_ms_threshold /* 30000 */)
```

`skip_rate` returns *both* the derived rate and the raw-field rate side by side, with the
definition in the output, so a number is never reported without its definition attached.
Optional API enrichment to fill `duration_ms` is described in R6.

### Ingest properties

Re-runnable and idempotent: each run gets a `run_id`; Layer 2/3 are fully rebuilt from
`raw_plays` at the end of a run. Re-ingesting the same export twice is a no-op on the fact
table because `(track_uri, ts_utc, ms_played)` de-duplicates. Schema tolerance means unknown
keys are preserved in `payload` and reported, never silently dropped; **missing** expected keys
or a type mismatch fail the run loudly with the offending file, index, and value — it will not
half-load.

### Synthetic fixture (Phase 4, before your export arrives)

`make-fixture` generates ~8,000 plays over 26 months with realistic structure: a stable
core of ~40 artists, ~10 artists that fade out mid-timeline (so `dropped_artists` has real
answers), ~8 that appear late (so `taste_drift` does), a diurnal listening curve with a
weekday/weekend split, plausible `reason_start`/`reason_end` distributions, a ~25% skip
population concentrated in a few artists, some offline/incognito rows, and a handful of
podcast rows. Deterministic under a seed so tests can assert exact numbers.

### Local query interface — both options, and my recommendation

**Option A — constrained parameterized builder.** Tools take enumerated dimensions, measures,
filters, and the server assembles SQL from a fixed template set. No user SQL ever reaches
DuckDB.
*Pros:* injection is structurally impossible; every query is pre-tested; output shape is stable.
*Cons:* "how has my taste drifted" and "artists I dropped" each need bespoke support, and the
next question you think of needs a code change. You will hit the wall within a week.

**Option B — text-to-SQL against a documented fixed schema, with guardrails.**
*Pros:* covers the open-ended half of your brief, which is the actual point of the analytics store.
*Cons:* needs real defensive engineering, and DuckDB can read files and load extensions, so a
naive allowlist is not enough.

**Recommendation: B, with A's named tools kept as a first-class layer on top.** The nine named
tools in §2.3 cover the questions you actually listed, so the model reaches for a tested query
first; `query_listening_history` is the escape hatch for everything else. This is the right
call *specifically because* of the threat model: the database is a local, single-user, derived
copy of data you already own, containing nothing the model can't already read via
`describe_listening_data`. The risk isn't exfiltration, it's a runaway query or a write. Both
are containable:

1. **Separate read-only connection** — `duckdb.connect(path, read_only=True)`, opened once at
   startup and never shared with the ingest path.
2. **Parse, don't regex** — `sqlglot.parse(sql, dialect="duckdb")`. Require exactly one
   statement; require the root to be `Select` (a leading `WITH` is fine). Reject any
   `Insert/Update/Delete/Create/Drop/Alter/Copy/Attach/Detach/Pragma/Set/Export/Call` node
   anywhere in the tree.
3. **Function denylist on the parsed AST** — `read_csv*`, `read_json*`, `read_parquet`, `glob`,
   `install`, `load`, and anything matching `*_auto`, blocking DuckDB's file-reading escape.
4. **Session lockdown** — `SET enable_external_access = false;` `SET allow_unsigned_extensions = false;`
   `SET threads = 2;` `SET memory_limit = '1GB';` applied at connect.
5. **Forced row cap** — the validated query is wrapped: `SELECT * FROM (<sql>) LIMIT <max_rows+1>`;
   returning `max_rows+1` rows sets `truncated: true`.
6. **Timeout** — execute on a worker thread with a watchdog calling `con.interrupt()` after
   `config.query_timeout_s` (default 10).
7. **Echo `sql_executed`** in the response so what ran is always visible.

Layers 2–4 are pure functions over a string and get their own test file with an explicit
attack corpus (stacked statements, comment-smuggled DDL, CTE-wrapped `read_csv`, `PRAGMA`,
`ATTACH` to a second database).

---

## 5. HTTP client design

One `httpx2.AsyncClient` for the process lifetime, `base_url="https://api.spotify.com/v1"`.

- **Concurrency:** `asyncio.Semaphore(config.max_concurrency, default 4)`. Not optional — the
  resolver's fan-out is the whole reason.
- **Rate limiting:** a token bucket sized to a rolling 30-second window
  (`config.calls_per_30s`, default a conservative 90). Spotify doesn't publish the number and
  Dev Mode is lower than Extended Quota, so this is a self-imposed ceiling, tunable.
- **429:** read `Retry-After` (seconds), sleep that plus jitter, retry. Max
  `config.max_retries` (default 3), then surface a typed `RateLimited` error naming the wait.
  A 429 also trips a short circuit-breaker that pauses the bucket for every in-flight caller,
  so parallel resolver tasks don't each independently discover the limit.
- **5xx / transport errors:** exponential backoff with full jitter, same retry ceiling.
  4xx other than 429 never retries.
- **401:** one forced refresh + single replay; a second 401 is a hard error.
- **Caching:** ETag capture and `If-None-Match` replay on GETs (the API docs explicitly support
  conditional requests), backed by an on-disk store keyed by URL+params. A 304 serves the
  cached body and costs no quota. Separate long-lived cache for immutable-ish metadata
  (`GET /tracks/{id}`, `GET /artists/{id}`) with a configurable TTL, default 30 days.
- **Dry run:** `config.dry_run = true` makes the client log the method, URL, and full body of
  any non-GET at INFO and return a synthetic success without sending. GETs still execute, so
  previews remain accurate. `server_status` always reports the flag, and every write tool's
  response is prefixed `[DRY RUN]` so it can't be mistaken for a real result.

### Response models

`api/models.py` models the **2026** shapes, not the historical ones:

- `Playlist.items` (not `.tracks`), `PlaylistItemsPage.items[].item` (not `.track`) — with a
  fallback that reads the deprecated `track` key if `item` is absent, since the reference docs
  still show both.
- `Playlist.items` is `Optional` and documented as absent for non-owned playlists.
- No `popularity`, `followers`, `available_markets`, `label`, `album_group`, `linked_from` on
  any model. `external_ids` **is** modelled on Track and Album (reverted March 2026) — `isrc`
  is load-bearing for the resolver.
- Unknown fields are ignored rather than rejected, but logged once per field name at DEBUG so
  API drift is visible without breaking anything.

---

## 6. Guardrails

### Two-phase confirmation

Call 1 — no `confirm_token`:

```json
{ "status": "confirmation_required",
  "confirm_token": "cf_9f2a…",
  "expires_at": "2026-08-25T18:41:00Z",
  "action": "add_playlist_items",
  "preview": {
    "playlist": {"id": "3c…", "name": "Late 2023 Rediscovery", "current_items": 0},
    "will_add": [{"pos": 1, "uri": "spotify:track:…", "artist": "…", "title": "…"}, "…"],
    "counts": {"adding": 40, "resulting_size": 40, "api_calls": 1},
    "irreversible": false
  },
  "next_step": "Show this preview to the user. Call again with confirm_token only after they approve." }
```

Call 2 supplies `confirm_token` **plus the identical arguments**. The server recomputes a
canonical hash of the payload and compares it to the hash bound to the token at issue time; any
drift rejects the call. Tokens are single-use, 10-minute TTL, held in process memory.

**Where I want to be straight with you:** MCP gives a server no way to *force* a human into the
loop — a model can call twice in succession on its own. What this design actually guarantees is
that (a) no write ever happens on a single call, (b) the exact payload is rendered before
anything is sent, (c) the executed payload provably equals the previewed one, and (d) the
client shows both calls as separate approvals. That is meaningful but it is not a hard gate.

The hard gate, where the client supports it, is **elicitation** — the MCP SDK's multi-round-trip
user-input mechanism, which prompts the *user* directly mid-tool-call. Plan: attempt elicitation
first; if the client declines or doesn't support it, fall back to the confirm-token flow and say
so in the response. I'll verify elicitation's exact API in the v2 SDK during Phase 3 and report
back before relying on it.

### Other guardrails

- `remove_playlist_items` is the only `destructive_hint=True` tool, is config-disabled by
  default, requires a caller-supplied `snapshot_id`, and its description forbids invocation
  from broad instructions.
- No tool deletes a playlist, unfollows anything, or writes to the library. Those endpoints are
  simply not in `endpoints.py`.
- Every write tool logs a structured audit record (timestamp, tool, payload hash, token,
  outcome, snapshot_id) to a local JSONL file, so "what did it do at 2am" is answerable.
- `open_world_hint=False` on all local analytics tools; `True` on search and resolve.

---

## 7. The "best pop songs right now" pipeline

Spotify exposes **no** popularity signal to a Dev Mode app: no `/recommendations`, no
`/browse/new-releases`, no `popularity` field, no editorial playlists, no artist top-tracks. So
freshness comes entirely from outside and the design says so out loud.

```
web search (Claude's own tool, not this server)
   → chart page → model extracts [{artist, title}]
   → resolve_tracks              ← this server
   → preview (resolved + unresolved)
   → confirm
   → create_playlist → add_playlist_items (chunks of 100)
   → return URL + unresolved list
```

`build_playlist_from_candidates`'s description states plainly: *"This tool does not know what is
popular. It converts a caller-supplied list of {artist, title} into a Spotify playlist. Chart
freshness and accuracy come entirely from whatever source produced the candidate list — cite
that source to the user, and do not describe the result as 'currently trending' on Spotify's
authority."*

### Resolver algorithm

For each candidate, in order, stopping at the first accepted match:

1. **Cache** — lookup by `normalize(artist)|normalize(title)`. Hit → return, zero HTTP.
2. **Filtered search** — `q=track:"<title>" artist:"<artist>"`, `type=track`, `limit=10`.
   Never bare text.
3. **Verify before accepting.** A result is accepted only if:
   - an artist on the track matches the input artist by **normalized name**, and
   - the title matches after normalization, and
   - the result does not trip a variant filter.
   The verified artist's **Spotify artist ID** is recorded on the resolution. Where the input
   set contains several tracks by one artist, the first confirmed artist ID is reused to
   verify subsequent tracks by ID rather than by string.
4. **Page once** — if none of the first 10 qualify, retry at `offset=10` (offset caps at 1000;
   one extra page is the cost/benefit sweet spot).
5. **Relaxed retry** — drop the `track:` filter, keep `artist:`, re-verify at the same
   strictness. Artist verification is never relaxed.
6. **Give up** — append to `unresolved` with the reason and the rejected candidates, so you can
   see *why* it failed rather than just that it did.

**Normalization** (`normalize.py`): casefold, Unicode NFKD, strip diacritics, collapse
whitespace, unify quote/dash characters, drop bracketed suffixes (`(Remastered 2011)`,
`- Radio Edit`, `(feat. X)` — with the featured artist retained separately as a secondary
match signal), and strip a leading `The `.

**Variant rejection** (`match.py`) — the substitutions that silently ruin a playlist. Reject
when the candidate title or album contains, and the input does not: `karaoke`, `tribute`,
`made famous by`, `in the style of`, `originally performed by`, `sped up`, `slowed`,
`nightcore`, `8d audio`, `instrumental`, `cover`, `remix`, `live at`, `live from`, `- live`.
Prefer a non-`explicit`-flag-mismatched, earlier-`album.release_date` result among ties.

**Confidence** is reported per resolution: `exact` (artist ID verified + exact normalized title),
`strong` (artist verified, title normalized-equal after suffix stripping), `weak` (artist
verified, fuzzy title within threshold — surfaced in the preview for review), and nothing below
that is ever accepted. **No near-match is ever substituted and no track is ever silently
dropped**: every input appears in exactly one of `resolved` or `unresolved`.

**Cache** — DuckDB table (same file, separate schema) `resolution_cache(norm_key PK, track_uri,
track_id, artist_id, isrc, confidence, method, resolved_at, search_calls)`. Negative results are
cached too, with a shorter TTL (default 7 days), so a re-run doesn't re-pay for known failures.
`isrc` is stored because it's the one globally stable identifier the API still returns, making
it a reliable cross-check and a future re-resolution key.

**Cost:** ~1 call per track on a cache miss, 2–3 worst case, 0 on a hit. 40 tracks cold ≈ 45
calls at concurrency 4 ≈ a few seconds. Tested entirely against a mocked transport.

---

## 8. Dependencies

| Package | Why |
| --- | --- |
| `mcp>=2.1.1` | Official MCP Python SDK. v2.1.1 (Jan 2026) is current; `MCPServer` + `@mcp.tool()` with type hints as schema. Requires Python ≥3.10. |
| `httpx2>=2.12` | The HTTP client. **See conflict C1** — `httpx2` is the maintained continuation of `httpx` (now stewarded by Pydantic), and `mcp` 2.1.1 already depends on it. Async, HTTP/2, `MockTransport` for tests. |
| `duckdb>=1.5.5` | Analytics store. Embedded, no server, reads JSON natively, `read_only` connections, fast enough that "a couple of years of plays" is instant. |
| `pydantic>=2.12` | Tool arg validation and response models. Already required by `mcp`; declared explicitly since our models depend on it directly. |
| `sqlglot>=27` | Parses the text-to-SQL input into an AST with a `duckdb` dialect. Justification is specific: a regex allowlist is bypassable via comments, stacked statements, and CTEs; an AST walk is not. This dependency is the difference between a safe escape hatch and a liability. |
| `platformdirs>=4` | Correct config/data/cache directories on Windows. Hardcoding `~/.spotify-mcp` is wrong on your machine. |
| `python-dotenv>=1` | `.env` loading. (Also available via `mcp[cli]`; declared directly to avoid depending on an extra.) |
| **dev** | |
| `pytest`, `pytest-asyncio` | Test runner + async support. |
| `ruff>=0.6` | Lint + format, as specified. |
| `mypy` *(optional)* | Type checking. Type hints are required either way; mypy in CI is your call. |

**Not used, deliberately:** `spotipy` (targets removed endpoints — your instinct is right),
`respx` (its httpx2 compatibility is unverified; `httpx2.MockTransport` is built in and
sufficient, so this is a dependency risk I'd rather not take), `pandas` (DuckDB returns what we
need; adding pandas for formatting is 60 MB for nothing), any web-scraping library (chart
fetching is Claude's `WebSearch`/`WebFetch`, not this server's job).

---

## 9. Build order

Each phase ends in something you run and verify. I stop after each and wait.

### Phase 1 — Auth + client skeleton
Deliverable: `uv run spotify-mcp login` completes a browser consent and writes a token;
`uv run spotify-mcp serve` connects in Claude Desktop; `get_me` and `server_status` return real
data. Includes token persistence, single-flight refresh, 429/backoff handling, the dry-run flag
(cheap to add now, awkward to retrofit), structured logging, and `pyproject.toml` + ruff config.
**Verify:** you see your own Spotify ID through Claude; deleting the token file and re-running
`login` recovers; `test_pkce.py` / `test_token_manager.py` / `test_client.py` pass.
*Adjustment to your ordering: dry-run moves from Phase 3 to here — it's a client-level switch.*

### Phase 2 — Live read tools + shape probe
All of §2.2. Plus `scripts/probe_api.py`, which calls each endpoint once and writes
`docs/observed_shapes.md` recording the **actual** keys returned to your Dev Mode app. That file
settles conflict C2 empirically instead of by argument.
**Verify:** ask Claude for your top artists, recent plays, and a search; confirm
`observed_shapes.md` matches the changelog; confirm `get_playlist_items` 403s on a playlist you
don't own (expected, documented).

### Phase 3 — Playlist writes + confirmation layer
All of §2.4, the confirm-token machinery, the elicitation attempt, the audit log.
**Verify:** with `dry_run = true`, ask Claude to create a playlist and watch it log without
sending. Flip to false, create a real playlist end to end. Confirm a mutated payload on call 2
is rejected. Confirm `remove_playlist_items` refuses while config-disabled.

### Phase 4 — DuckDB ingest + fixture + analytics
Schema, ingest CLI, schema-tolerance checks, synthetic fixture, the nine analytics tools,
`sqlguard`, `docs/analytics_schema.md`.
**Verify:** `uv run spotify-mcp make-fixture && uv run spotify-mcp ingest ./fixture` builds the
DB; ask Claude "when do I listen most?" and "which artists did I drop?" against synthetic data
and get sensible answers; `test_sqlguard.py`'s attack corpus is fully blocked;
`test_analytics.py` asserts exact numbers against the seeded fixture. Re-running ingest is a
no-op. When your real export lands, the same command ingests it — and if the field names differ
from what §4 assumes, it fails loudly and we fix a mapping, not a rewrite.

### Phase 5 — Resolver + chart pipeline
`resolve_tracks`, `build_playlist_from_candidates`, the resolution cache.
**Verify:** feed a known-tricky list (a track with a famous karaoke version, one with a sped-up
edit, one live album, one artist with a tribute band, one deliberately nonexistent) and confirm
every substitution is rejected and the fake appears in `unresolved`. Then the real thing: "make
me a playlist of the best pop songs right now," end to end.

### Phase 6 — Polish
Cache tuning, log levels, `README.md` written from scratch (create the Spotify app, register
`http://127.0.0.1:8888/callback`, `login`, request the export, `ingest`, wire into Claude
Desktop with a working config JSON), full ruff clean, final test pass.

Commits at each phase boundary with real messages. I have not committed `PLAN.md` yet since
we're about to iterate on it — I'll commit once you've signed off.

---

## 10. Open questions, risks, and conflicts with your brief

### Conflicts — where the docs disagree with your instructions

**C1 — `httpx` vs `httpx2`.** You specified `httpx`. As of Aug 18 2026, `httpx2` (2.12.0) is the
maintained continuation of the httpx project under Pydantic's stewardship, and **`mcp` 2.1.1
already depends on `httpx2>=2.5.0`**. Using plain `httpx` would pull two HTTP stacks into one
process for no benefit. The API is broadly compatible so the "thin client, not spotipy" spirit
is unchanged. **Recommendation: `httpx2`.** Your call — say the word and I'll use `httpx`.

**C2 — "removed" vs "deprecated" on `popularity`, `available_markets`, `preview_url`.** The
changelog and migration guide list these as removed. The endpoint reference pages still
*document* them, marked "Deprecated" — e.g. `GET /tracks/{id}` still shows `popularity`,
`available_markets`, and `preview_url`. The most likely reading is that the reference describes
all quota modes while the Feb 2026 removals bite Development Mode specifically. I am
**designing as though they are absent** (which is what your brief assumes and the safe choice),
and Phase 2's probe script will record what your app actually receives. If `popularity` turns
out to be present for your app, that changes the §7 pipeline meaningfully and we should revisit.

**C3 — reading playlist contents.** Your tool surface lists "playlist contents" as a live read.
`GET /playlists/{id}/items` is *"only accessible for playlists owned by the current user or
playlists the user is a collaborator of"* and **403s otherwise**; `GET /playlists/{id}` on a
non-owned playlist returns metadata with no `items` object at all. So: your own and
collaborative playlists, yes; anyone else's, including every editorial playlist, no. This isn't
a workaround situation — there is no path to it. Flagging it because it forecloses "analyze
this public playlist" as a future ask.

**C4 — one Client ID per developer.** The Feb 6 blog says one. The migration guide, which is
newer, says *"Maximum 1 Client ID per developer (increased to 25 as of July 2026)."* Doesn't
change the design; correcting the record. The 5-users-per-app and Premium requirements stand.

**C5 — `external_ids` state (you asked me to verify).** Reverted in March 2026 and confirmed
available on **Track** and **Album**. Not restored for other object types. This is a small win:
`isrc` is the only stable global identifier the API still returns, and `search` supports an
`isrc:` filter, so it does double duty in the resolver cache.

**C6 — Dev Mode endpoint restrictions.** The blog says Dev Mode access is *"limited to a smaller
set of supported endpoints,"* but neither it nor the migration guide publishes that list, and
the March 9 update **postponed** endpoint restrictions for existing integrations. So there may
be a restriction beyond the documented removals that only shows up at runtime. Phase 2's probe
is the mitigation: we find out on a cheap dedicated script rather than mid-feature.

### Risks

**R1 — Search cost.** limit 10, no batch lookups. A 40-track playlist is ~45 calls cold. Mitigated
by the resolution cache, ETag caching, concurrency 4, and the token bucket — but a large
cold resolve will take seconds and may hit 429. Acceptable; noting it so the latency isn't a
surprise.

**R2 — Rate-limit numbers are undisclosed.** Rolling 30-second window, thresholds unpublished,
Dev Mode lower than Extended Quota. My default (90/30s) is a guess. Phase 2 may need tuning
from observed `Retry-After` behaviour.

**R3 — `mcp` v2 is young.** Released Jan 2026, and the v2 API (`MCPServer`) differs from every
v1 tutorial in circulation. I've confirmed the decorator signature, `ToolAnnotations`, and
type-hints-as-schema from the current docs, but I have **not** yet confirmed the elicitation
API or the lifespan-state pattern in detail — both are Phase 1/3 verification items. If
elicitation isn't usable, §6 falls back to confirm-tokens only and I'll tell you rather than
quietly downgrading the guarantee.

**R4 — Export schema is unverified.** You don't have the export yet, and Spotify's support page
lists 21 fields *descriptively* without JSON key names, pointing at the "Read Me First" file
that ships inside the download. Your field list matches what community parsers report, and
§4 assumes it — but the support page also implies `username`, `ip_addr`, `user_agent`,
`offline_timestamp`, and `incognito_mode` exist, which your list omits. The loader therefore
preserves every field verbatim in `raw_plays.payload`, reports unknown keys, and **fails loudly**
rather than guessing on missing ones. When the export lands, expect a mapping fix, not a
rewrite.

**R5 — Token file permissions on Windows.** You asked for restrictive file permissions.
`os.chmod(0o600)` is effectively a no-op for access control on Windows — it toggles a read-only
bit, nothing more. Plan is an `icacls`-based ACL reset with post-write verification and a loud
warning on failure. If you want something stronger, DPAPI encryption at rest (via `pywin32`)
would genuinely protect the file against other user accounts; it's an extra dependency and I
left it out of the default plan. Tell me if you want it.

**R6 — Skip rate has no ground truth locally.** `duration_ms` is absent from the export, so
"skipped 40% of the way through" isn't computable from local data. The `reason_end = 'fwdbtn'`
heuristic in §4 is defensible and I report the definition alongside every number. The optional
fix is enriching `dim_track.duration_ms` via `GET /tracks/{id}` — but with batch lookups gone
that's **one call per distinct track**, likely thousands, so it'd be an opt-in background CLI
command with resumable progress, not something a tool call triggers. Proposing we defer it past
Phase 6 and decide once we see how many distinct tracks your export actually contains.

**R7 — Timezone.** `ts` is UTC. "Listening by hour of day" is meaningless in UTC if you've
travelled. `conn_country` is a coarse proxy but isn't a timezone. Plan: a single
`local_timezone` config value (default: system zone) applied at ingest. Per-play historical
timezone reconstruction from `conn_country` is possible but I don't think it's worth the
complexity — flagging in case you disagree.

**R8 — Chart source quality and provenance.** "Best pop songs right now" is only as good as the
page the model finds, and with `popularity` gone there's nothing in the Spotify API to
cross-check it against. Some chart sites also restrict automated access in their terms. My
recommendation is that candidate lists come from Claude's own web search with the source cited
back to you in the result, and that this server never fetches chart pages itself — which also
keeps the server's dependency surface and its legal surface small.

**R9 — `duckdb.connect(read_only=True)`.** I'm confident this parameter exists but the current
Python overview page I fetched didn't state it explicitly (it documents the `config` dict). I'll
verify it in Phase 4 before relying on it; if it's not available, the fallback is a
config-enforced read-only session plus the sqlguard AST checks, which is where the real
protection lives anyway.

### Two things I'd flag as genuinely not doable

- **Anything requiring audio characteristics** — tempo, key, energy, danceability, valence. Gone
  since Nov 2024 with no access path. "Make me a high-energy workout playlist" cannot be done
  from Spotify data. It can be approximated from a web-sourced candidate list, or from your own
  local history (what you actually play while exercising, if the timestamps show a pattern),
  but not from the API. I'd rather say that now than build something that half-works.
- **True listening history beyond the export.** `recently-played` returns the last 50 items and
  nothing else. The DuckDB store is the *only* real history source, and it's a snapshot as of
  whenever you requested the export — it does not update. Keeping it current means re-requesting
  the export periodically, or running a poller that captures `recently-played` every ~30 minutes
  into the same table. The latter is a real option and would compose cleanly with §4's schema,
  but it's a background daemon and outside your stated scope. Say if you want it planned in.

---

## 11. What I need from you before Phase 1

1. **C1** — `httpx2` (recommended) or `httpx` as specified?
2. **§4 Option B** — confirm you're happy with guarded text-to-SQL as the escape hatch alongside
   the nine named tools.
3. **R5** — is `icacls` ACL hardening sufficient, or do you want DPAPI encryption at rest?
4. **§2.2** — playback *control* tools (play/pause/skip/queue/volume) in or out? Currently out,
   which keeps `user-modify-playback-state` off the consent screen.
5. Confirm you have Premium on the account (Dev Mode requires it as of Mar 9 2026) and that the
   Spotify app either doesn't exist yet or I should assume a specific Client ID.
