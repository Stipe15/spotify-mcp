"""PKCE verifier/challenge generation and the authorize URL builder.

Pure functions, no I/O — see https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

from spotify_mcp.config import AUTHORIZE_URL


def generate_code_verifier() -> str:
    """A random 43-128 character verifier, per RFC 7636."""
    # 64 random bytes -> 86-char base64url string, comfortably inside the range.
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")


def code_challenge_from_verifier(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_state() -> str:
    return secrets.token_urlsafe(24)


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    state: str,
    code_challenge: str,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
        "scope": " ".join(scopes),
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"
