"""CLI entry point: `spotify-mcp login|serve`."""

from __future__ import annotations

import argparse
import asyncio
import sys
import webbrowser

from spotify_mcp.auth.loopback import await_callback
from spotify_mcp.auth.manager import TokenManager
from spotify_mcp.auth.pkce import (
    build_authorize_url,
    code_challenge_from_verifier,
    generate_code_verifier,
    generate_state,
)
from spotify_mcp.config import Settings
from spotify_mcp.errors import SpotifyMCPError


def _cmd_login(_: argparse.Namespace) -> int:
    settings = Settings.load()
    verifier = generate_code_verifier()
    challenge = code_challenge_from_verifier(verifier)
    state = generate_state()

    url = build_authorize_url(
        client_id=settings.client_id,
        redirect_uri=settings.redirect_uri,
        scopes=settings.scopes,
        state=state,
        code_challenge=challenge,
    )

    print(f"Opening your browser to authorize spotify-mcp:\n\n  {url}\n")
    print(f"Listening on {settings.redirect_uri} for the redirect (5 minute timeout)...")
    webbrowser.open(url)

    result = await_callback(settings.redirect_port, settings.redirect_path)

    if result.error:
        print(f"Authorization failed: {result.error}", file=sys.stderr)
        return 1
    if not result.code:
        print("Timed out waiting for the browser redirect.", file=sys.stderr)
        return 1
    if result.state != state:
        print("State mismatch on the redirect — possible CSRF; aborting.", file=sys.stderr)
        return 1

    async def _exchange() -> None:
        manager = TokenManager(settings)
        try:
            await manager.exchange_code(
                code=result.code, code_verifier=verifier, redirect_uri=settings.redirect_uri
            )
        finally:
            await manager.aclose()

    try:
        asyncio.run(_exchange())
    except SpotifyMCPError as exc:
        print(f"Token exchange failed: {exc}", file=sys.stderr)
        return 1

    print(f"Signed in. Token saved to {settings.token_path}")
    print(f"Granted scopes: {' '.join(settings.scopes)}")
    return 0


def _cmd_serve(_: argparse.Namespace) -> int:
    from spotify_mcp.server import build_server

    settings = Settings.load()
    mcp = build_server(settings)
    mcp.run(transport="stdio")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="spotify-mcp")
    subparsers = parser.add_subparsers(dest="command", required=True)

    login = subparsers.add_parser("login", help="Authorize this app with your Spotify account")
    login.set_defaults(func=_cmd_login)
    serve = subparsers.add_parser("serve", help="Run the MCP server over stdio")
    serve.set_defaults(func=_cmd_serve)

    args = parser.parse_args()
    try:
        sys.exit(args.func(args))
    except SpotifyMCPError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
