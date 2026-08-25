from __future__ import annotations

import base64
import hashlib

from spotify_mcp.auth.pkce import (
    build_authorize_url,
    code_challenge_from_verifier,
    generate_code_verifier,
    generate_state,
)


def test_verifier_length_within_rfc7636_bounds():
    verifier = generate_code_verifier()
    assert 43 <= len(verifier) <= 128


def test_verifier_is_url_safe_and_unpadded():
    verifier = generate_code_verifier()
    assert "=" not in verifier
    assert all(c.isalnum() or c in "-_" for c in verifier)


def test_verifier_is_random_across_calls():
    assert generate_code_verifier() != generate_code_verifier()


def test_challenge_is_sha256_s256_of_verifier():
    verifier = generate_code_verifier()
    challenge = code_challenge_from_verifier(verifier)
    digest = hashlib.sha256(verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert challenge == expected


def test_state_is_random_and_nonempty():
    a, b = generate_state(), generate_state()
    assert a != b
    assert len(a) > 10


def test_authorize_url_contains_required_pkce_params():
    url = build_authorize_url(
        client_id="cid",
        redirect_uri="http://127.0.0.1:8888/callback",
        scopes=["playlist-read-private", "user-top-read"],
        state="abc123",
        code_challenge="chal",
    )
    assert url.startswith("https://accounts.spotify.com/authorize?")
    assert "response_type=code" in url
    assert "client_id=cid" in url
    assert "code_challenge_method=S256" in url
    assert "code_challenge=chal" in url
    assert "state=abc123" in url
    assert "redirect_uri=" in url
    assert "playlist-read-private" in url
