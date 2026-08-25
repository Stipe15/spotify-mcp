"""Pydantic models for the post-February-2026 Spotify response shapes.

Only fields confirmed to still exist are modelled — no `popularity`,
`followers`, `available_markets`, `label`, `album_group`, or `linked_from`
anywhere, and no `email`/`country`/`product`/`explicit_content` on the user.
`external_ids` (isrc/ean/upc) IS modelled on Track and Album: it was reverted
in March 2026. See PLAN.md §5 and §10 (conflict C2) — the reference docs
still show the removed fields marked "Deprecated" rather than gone, so
`scripts/probe_api.py` checks empirically what this app's Dev Mode tier
actually returns; these models describe what we build tools against either
way. Unknown fields are ignored, not rejected, so API drift doesn't crash a
tool — see `docs/observed_shapes.md` once the probe has run.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ExternalUrls(_Base):
    spotify: str | None = None


class ExternalIds(_Base):
    isrc: str | None = None
    ean: str | None = None
    upc: str | None = None


class Image(_Base):
    url: str
    height: int | None = None
    width: int | None = None


class Owner(_Base):
    id: str | None = None
    display_name: str | None = None
    uri: str | None = None
    external_urls: ExternalUrls = ExternalUrls()


class Followers(_Base):
    total: int | None = None


class Me(_Base):
    """GET /me. No `email`, `country`, `product`, or `explicit_content` —
    confirmed removed by `scripts/probe_api.py` against this app's own Dev
    Mode tier. `followers`, however, IS still present empirically, despite
    the February 2026 changelog listing it as removed from the user object —
    see docs/observed_shapes.md (gitignored, contains this account's data)
    and PLAN.md §10. Ground truth from the probe overrides the static
    changelog text."""

    id: str
    display_name: str | None = None
    uri: str
    followers: Followers = Followers()
    external_urls: ExternalUrls = ExternalUrls()
    images: list[Image] = []


class SimplifiedArtist(_Base):
    id: str
    name: str
    uri: str
    external_urls: ExternalUrls = ExternalUrls()


class Artist(_Base):
    """GET /artists/{id}. No `followers`, no `popularity`."""

    id: str
    name: str
    uri: str
    genres: list[str] = []
    images: list[Image] = []
    external_urls: ExternalUrls = ExternalUrls()


class SimplifiedAlbum(_Base):
    id: str
    name: str
    uri: str
    album_type: str | None = None
    release_date: str | None = None
    total_tracks: int | None = None
    images: list[Image] = []
    artists: list[SimplifiedArtist] = []
    external_urls: ExternalUrls = ExternalUrls()


class Album(SimplifiedAlbum):
    """GET /albums/{id}. No `label`, `album_group`, `available_markets`,
    `popularity`. `external_ids` restored March 2026."""

    external_ids: ExternalIds = ExternalIds()


class SimplifiedTrack(_Base):
    """As nested under /albums/{id}/tracks — no embedded `album` (redundant)."""

    id: str
    name: str
    uri: str
    duration_ms: int
    explicit: bool = False
    disc_number: int | None = None
    track_number: int | None = None
    artists: list[SimplifiedArtist] = []
    external_urls: ExternalUrls = ExternalUrls()


class Track(_Base):
    """GET /tracks/{id}. No `popularity`, `available_markets`, `linked_from`,
    `preview_url`. `external_ids.isrc` restored March 2026 — the resolver's
    most reliable cross-check (PLAN.md §7)."""

    id: str
    name: str
    uri: str
    duration_ms: int
    explicit: bool = False
    disc_number: int | None = None
    track_number: int | None = None
    is_local: bool = False
    artists: list[SimplifiedArtist] = []
    album: SimplifiedAlbum | None = None
    external_ids: ExternalIds = ExternalIds()
    external_urls: ExternalUrls = ExternalUrls()


class Paging(_Base, Generic[T]):
    href: str | None = None
    limit: int | None = None
    next: str | None = None
    previous: str | None = None
    total: int | None = None
    offset: int | None = None
    items: list[T] = []


class CursorPaging(_Base, Generic[T]):
    href: str | None = None
    limit: int | None = None
    next: str | None = None
    cursors: dict | None = None
    total: int | None = None
    items: list[T] = []


class SimplifiedPlaylist(_Base):
    id: str
    name: str
    uri: str
    description: str | None = None
    public: bool | None = None
    collaborative: bool = False
    owner: Owner = Owner()
    snapshot_id: str | None = None
    images: list[Image] = []
    external_urls: ExternalUrls = ExternalUrls()


class PlaylistItemEntry(_Base):
    """`item` is a track or episode object, passed through unmodelled — this
    server has no episode/podcast support. Response field is `item`, not the
    pre-Feb-2026 `track` (PLAN.md's single most likely thing to break)."""

    added_at: str | None = None
    is_local: bool = False
    item: dict | None = None


class PlaylistItemsPage(Paging[PlaylistItemEntry]):
    pass


class Playlist(SimplifiedPlaylist):
    """GET /playlists/{id}. `items` is only populated for playlists you own
    or collaborate on — absent (metadata only) otherwise. See PLAN.md C3."""

    items: PlaylistItemsPage | None = None


class PlaylistSummary(_Base):
    """The lean shape `get_playlist` returns — metadata plus item count, not
    the items themselves (use `get_playlist_items` for those, paginated)."""

    id: str
    name: str
    uri: str
    description: str | None = None
    public: bool | None = None
    collaborative: bool = False
    owner: Owner = Owner()
    snapshot_id: str | None = None
    item_count: int | None = None
    images: list[Image] = []
    external_urls: ExternalUrls = ExternalUrls()


class SavedTrackEntry(_Base):
    added_at: str | None = None
    track: Track


class SavedAlbumEntry(_Base):
    added_at: str | None = None
    album: Album


class RecentlyPlayedEntry(_Base):
    played_at: str
    track: Track
    context: dict | None = None


class FollowedArtistsPage(_Base):
    """GET /me/following?type=artist wraps the cursor page under `artists`."""

    artists: CursorPaging[Artist]


class SearchResults(_Base):
    tracks: Paging[Track] | None = None
    artists: Paging[Artist] | None = None
    albums: Paging[SimplifiedAlbum] | None = None
    playlists: Paging[SimplifiedPlaylist] | None = None


class PlaybackDevice(_Base):
    id: str | None = None
    name: str | None = None
    type: str | None = None
    is_active: bool = False
    volume_percent: int | None = None


class PlaybackState(_Base):
    """GET /me/player. `active=False` (nothing else populated) when Spotify
    returns 204 — no active device/session, not an error."""

    active: bool = True
    is_playing: bool = False
    progress_ms: int | None = None
    item: dict | None = None
    device: PlaybackDevice | None = None
    shuffle_state: bool | None = None
    repeat_state: str | None = None
