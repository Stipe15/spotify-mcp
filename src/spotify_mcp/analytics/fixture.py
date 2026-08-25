"""Synthetic Extended Streaming History generator.

Lets Phase 4+ be built, tested, and demoed before the real export arrives —
Spotify's data requests take weeks (PLAN.md). Produces the same JSON shape
`ingest.py` expects, deterministic under a fixed seed so tests can assert
exact numbers against it, with enough real structure to make every analytics
tool return a non-trivial answer:

- ~40 "core" artists active across the whole 26-month span
- ~10 "fading" artists heavily played in the first half, then silent — for
  dropped_artists / rediscover_tracks
- ~8 "late" artists mostly silent until the last few months — for
  taste_drift's "gained" side
- a diurnal listening curve with a weekday/weekend shape difference
- a handful of skip-prone artists driving the overall skip rate, plus
  otherwise-plausible reason_start/reason_end variety
- a small fraction of offline/incognito plays and a few podcast episodes
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEED = 20260101
TOTAL_PLAYS = 8_000
SPAN_MONTHS = 26
END_DATE = datetime(2026, 8, 1, tzinfo=timezone.utc)
START_DATE = END_DATE - timedelta(days=30 * SPAN_MONTHS)

N_CORE = 40
N_FADING = 10
N_LATE = 8
N_SKIP_PRONE = 5

_ADJ = [
    "Velvet",
    "Neon",
    "Crimson",
    "Electric",
    "Silent",
    "Golden",
    "Broken",
    "Wild",
    "Lunar",
    "Iron",
    "Hollow",
    "Amber",
    "Frozen",
    "Radiant",
    "Distant",
    "Static",
    "Violet",
    "Feral",
    "Quiet",
    "Bright",
    "Rusted",
    "Faded",
    "Endless",
    "Paper",
]
_NOUN = [
    "Foxes",
    "Horizon",
    "Machine",
    "Garden",
    "Echoes",
    "Wolves",
    "Signal",
    "River",
    "Ashes",
    "Static",
    "Harbor",
    "Ghosts",
    "Engine",
    "Season",
    "Radio",
    "Compass",
    "Mirror",
    "Ember",
    "Current",
    "Paper Moon",
    "Skyline",
    "Tideline",
    "Lantern",
]
_TITLE_A = [
    "Falling",
    "Chasing",
    "Waiting for",
    "Losing",
    "Finding",
    "Breaking",
    "Holding",
    "Dancing in",
    "Running from",
    "Dreaming of",
    "Burning",
    "Fading",
]
_TITLE_B = [
    "Stars",
    "the Rain",
    "Ghosts",
    "Tomorrow",
    "the Light",
    "Silence",
    "Static",
    "Home",
    "the Tide",
    "You",
    "Nothing",
    "the Fire",
    "Blue",
    "the Dark",
]

REASON_START_CHOICES = ["trackdone", "clickrow", "fwdbtn", "backbtn", "remote", "playbtn"]
REASON_START_WEIGHTS = [45, 20, 10, 5, 10, 10]
REASON_END_NORMAL_CHOICES = ["trackdone", "fwdbtn", "backbtn", "endplay", "logout"]
REASON_END_NORMAL_WEIGHTS = [70, 12, 8, 5, 5]

PLATFORMS = ["ios", "android", "windows", "osx", "web_player"]
PLATFORM_WEIGHTS = [35, 30, 15, 10, 10]


def _make_names(rng: random.Random, n: int, used: set[str]) -> list[str]:
    names: list[str] = []
    while len(names) < n:
        prefix = "The " if rng.random() < 0.3 else ""
        name = f"{prefix}{rng.choice(_ADJ)} {rng.choice(_NOUN)}"
        if name not in used:
            used.add(name)
            names.append(name)
    return names


def _make_title(rng: random.Random) -> str:
    return f"{rng.choice(_TITLE_A)} {rng.choice(_TITLE_B)}"


def _hour_weight(hour: int, is_weekend: bool) -> float:
    """A rough commute/lunch/evening diurnal curve, flatter and later on weekends."""
    if is_weekend:
        peaks = {13: 3.0, 14: 3.5, 15: 2.5, 20: 3.0, 21: 3.5, 22: 2.5}
    else:
        peaks = {8: 3.0, 9: 2.0, 12: 2.5, 13: 2.0, 18: 3.0, 19: 3.5, 20: 2.5, 21: 1.5}
    return peaks.get(hour, 0.3)


def _random_timestamp(rng: random.Random) -> datetime:
    span_days = (END_DATE - START_DATE).days
    day_offset = rng.randint(0, span_days - 1)
    day = START_DATE + timedelta(days=day_offset)
    is_weekend = day.weekday() >= 5
    hours = list(range(24))
    weights = [_hour_weight(h, is_weekend) for h in hours]
    hour = rng.choices(hours, weights=weights, k=1)[0]
    minute = rng.randint(0, 59)
    second = rng.randint(0, 59)
    return day.replace(hour=hour, minute=minute, second=second, microsecond=0)


def _artist_activity_weight(artist_index: int, kind: str, ts: datetime) -> float:
    """How likely this artist is to be chosen for a play at time `ts`."""
    progress = (ts - START_DATE) / (END_DATE - START_DATE)  # 0.0 -> 1.0
    if kind == "core":
        return 1.0
    if kind == "fading":
        # Heavily played in the first half, tapering to ~silent by 60% through.
        return max(0.0, 1.0 - progress / 0.6) * 1.5
    if kind == "late":
        # Silent until 70% through, then ramps up.
        return 0.0 if progress < 0.7 else (progress - 0.7) / 0.3 * 2.0
    raise ValueError(kind)


def generate(seed: int = SEED, total_plays: int = TOTAL_PLAYS) -> list[dict]:
    rng = random.Random(seed)
    used_names: set[str] = set()

    core = _make_names(rng, N_CORE, used_names)
    fading = _make_names(rng, N_FADING, used_names)
    late = _make_names(rng, N_LATE, used_names)
    all_artists = (
        [(a, "core") for a in core] + [(a, "fading") for a in fading] + [(a, "late") for a in late]
    )
    skip_prone = set(rng.sample(core, N_SKIP_PRONE))

    # A small, fixed catalog of tracks per artist, reused across plays —
    # real listening repeats tracks constantly, and dim_track/skip_stats
    # need repeats to aggregate over.
    catalog: dict[str, list[tuple[str, str]]] = {}
    for artist, _kind in all_artists:
        n_tracks = rng.randint(6, 14)
        catalog[artist] = [
            (_make_title(rng), f"Album {rng.randint(1, 4)}") for _ in range(n_tracks)
        ]

    def track_uri_for(artist: str, title: str) -> str:
        # Deterministic pseudo-id from content, not random, so re-generation
        # with the same seed is byte-identical.
        digest = hashlib.sha1(f"{artist}|{title}".encode(), usedforsecurity=False).hexdigest()[:22]
        return f"spotify:track:{digest}"

    records: list[dict] = []
    n_podcast = max(20, total_plays // 150)
    n_music = total_plays - n_podcast

    for _ in range(n_music):
        ts = _random_timestamp(rng)
        weights = [
            _artist_activity_weight(i, kind, ts) * (4.0 if artist in skip_prone else 1.0)
            for i, (artist, kind) in enumerate(all_artists)
        ]
        if sum(weights) <= 0:
            continue
        artist, _kind = rng.choices(all_artists, weights=weights, k=1)[0]
        title, album = rng.choice(catalog[artist])
        uri = track_uri_for(artist, title)

        duration_ms = rng.randint(150_000, 260_000)
        is_skip_candidate = artist in skip_prone and rng.random() < 0.85
        if is_skip_candidate:
            ms_played = rng.randint(2_000, 25_000)
            reason_end = "fwdbtn"
        else:
            ms_played = rng.choices(
                [duration_ms, rng.randint(30_000, duration_ms)], weights=[70, 30], k=1
            )[0]
            reason_end = rng.choices(
                REASON_END_NORMAL_CHOICES, weights=REASON_END_NORMAL_WEIGHTS, k=1
            )[0]

        records.append(
            {
                "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "username": "fixture_user",
                "platform": rng.choices(PLATFORMS, weights=PLATFORM_WEIGHTS, k=1)[0],
                "ms_played": ms_played,
                "conn_country": "HR",
                "ip_addr": "0.0.0.0",
                "master_metadata_track_name": title,
                "master_metadata_album_artist_name": artist,
                "master_metadata_album_album_name": album,
                "spotify_track_uri": uri,
                "episode_name": None,
                "episode_show_name": None,
                "spotify_episode_uri": None,
                "reason_start": rng.choices(
                    REASON_START_CHOICES, weights=REASON_START_WEIGHTS, k=1
                )[0],
                "reason_end": reason_end,
                "shuffle": rng.random() < 0.55,
                "skipped": is_skip_candidate,
                "offline": rng.random() < 0.08,
                "offline_timestamp": None,
                "incognito_mode": rng.random() < 0.02,
            }
        )

    shows = _make_names(rng, 4, used_names)
    for _ in range(n_podcast):
        ts = _random_timestamp(rng)
        show = rng.choice(shows)
        ep_num = rng.randint(1, 200)
        records.append(
            {
                "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "username": "fixture_user",
                "platform": rng.choices(PLATFORMS, weights=PLATFORM_WEIGHTS, k=1)[0],
                "ms_played": rng.randint(60_000, 2_400_000),
                "conn_country": "HR",
                "ip_addr": "0.0.0.0",
                "master_metadata_track_name": None,
                "master_metadata_album_artist_name": None,
                "master_metadata_album_album_name": None,
                "spotify_track_uri": None,
                "episode_name": f"Episode {ep_num}",
                "episode_show_name": show,
                "spotify_episode_uri": f"spotify:episode:{show[:4]}{ep_num:04d}",
                "reason_start": "trackdone",
                "reason_end": rng.choices(
                    ["trackdone", "fwdbtn", "endplay"], weights=[70, 10, 20], k=1
                )[0],
                "shuffle": False,
                "skipped": False,
                "offline": rng.random() < 0.1,
                "offline_timestamp": None,
                "incognito_mode": False,
            }
        )

    records.sort(key=lambda r: r["ts"])
    return records


def write_fixture(out_dir: Path, seed: int = SEED, total_plays: int = TOTAL_PLAYS) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    records = generate(seed=seed, total_plays=total_plays)
    out_path = out_dir / "endsong_0.json"
    out_path.write_text(json.dumps(records, indent=None), encoding="utf-8")
    return out_path
