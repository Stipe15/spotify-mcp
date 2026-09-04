"""Artist/title normalization and variant-recording detection.

Pure functions, no I/O — see PLAN.md §7. The resolver fails silently exactly
here if this is careless: title-only matching reliably returns sped-up
edits, live versions, karaoke covers, tribute-band recordings, and regional
re-releases instead of the real thing.
"""

from __future__ import annotations

import re
import unicodedata

_QUOTE_CHARS = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "−": "-",
    }
)

_LEADING_THE_RE = re.compile(r"^the\s+", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")

# Matches a trailing/leading parenthetical or " - suffix" segment, e.g.
# "(Remastered 2011)", "- Radio Edit", "(feat. Drake)". Applied repeatedly
# so multiple suffixes ("Song (Live) - Remastered") are all stripped.
_BRACKETED_SUFFIX_RE = re.compile(
    r"""
    \s*\([^()]*\)\s*$      # trailing (...)
    |\s*\[[^\[\]]*\]\s*$   # trailing [...]
    |\s+-\s+[^-]+$         # trailing " - Something"
    """,
    re.VERBOSE,
)

_FEAT_RE = re.compile(
    r"""\(?\b(?:feat\.?|featuring|ft\.?)\s+([^()\[\]]+?)\)?\s*$""",
    re.IGNORECASE,
)

# (marker phrase, root keyword). If the marker phrase is present in a
# *candidate* result, it's a different recording than a bare title/artist
# search implies — unless the *input* itself already signals that root
# keyword (e.g. the user asked for "Song (Live)"; several live-ish phrasings
# share the root "live" so any of them suppresses any other). Never accepted
# as a match unless explicitly asked for. Checked against title and album.
VARIANT_MARKERS = [
    ("karaoke", "karaoke"),
    ("tribute", "tribute"),
    ("made famous by", "made famous by"),
    ("in the style of", "in the style of"),
    ("originally performed by", "originally performed by"),
    ("sped up", "sped up"),
    ("slowed", "slowed"),
    ("nightcore", "nightcore"),
    ("8d audio", "8d audio"),
    ("instrumental", "instrumental"),
    ("cover", "cover"),
    ("remix", "remix"),
    ("live at", "live"),
    ("live from", "live"),
    ("- live", "live"),
    ("(live)", "live"),
]


def normalize_text(s: str | None) -> str:
    """Casefold, strip diacritics, unify punctuation, collapse whitespace,
    drop a leading 'The '. The baseline comparison form for everything else
    in this module."""
    if not s:
        return ""
    s = s.translate(_QUOTE_CHARS)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold().strip()
    s = _LEADING_THE_RE.sub("", s)
    s = _WHITESPACE_RE.sub(" ", s)
    return s.strip()


def extract_featured_artists(title: str) -> tuple[str, list[str]]:
    """Split a trailing '(feat. X)'/'featuring X'/'ft. X' off a title.
    Returns (title_without_feat, [featured_artist_names]) — the featured
    artist is a secondary match signal, not required to match."""
    match = _FEAT_RE.search(title)
    if not match:
        return title, []
    names = [n.strip() for n in re.split(r",|&|\band\b", match.group(1)) if n.strip()]
    stripped = title[: match.start()].rstrip()
    return stripped, names


def strip_bracketed_suffixes(title: str) -> str:
    """Repeatedly strip trailing '(...)'/'[...]'/' - suffix' segments, e.g.
    'Blinding Lights - Single Version' -> 'Blinding Lights'. Applied to
    *candidate* titles only when checking for a strong (not exact) match —
    never mutates what the input actually asked for."""
    previous = None
    current = title
    while previous != current:
        previous = current
        current = _BRACKETED_SUFFIX_RE.sub("", current).strip()
    return current or title


def has_uncalled_for_variant_marker(candidate_text: str, input_text: str) -> str | None:
    """Returns the offending marker if `candidate_text` contains a
    variant-recording marker that `input_text` does NOT — e.g. the user
    asked for a normal studio track but the candidate is a karaoke cover.
    If the user's own input already mentions the marker (they explicitly
    want the live version), it is not treated as a red flag."""
    candidate_norm = normalize_text(candidate_text)
    input_norm = normalize_text(input_text)
    for marker, root in VARIANT_MARKERS:
        if marker in candidate_norm and root not in input_norm:
            return marker
    return None


def normalize_key(artist: str, title: str) -> str:
    """Cache key for the resolution cache — stable across trivial
    formatting differences in how a caller supplies the same request."""
    return f"{normalize_text(artist)}|{normalize_text(title)}"
