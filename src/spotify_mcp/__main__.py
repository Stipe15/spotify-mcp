"""CLI entry point: `spotify-mcp login|serve`."""

from __future__ import annotations

import argparse
import asyncio
import sys
import webbrowser
from pathlib import Path

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


def _cmd_make_fixture(args: argparse.Namespace) -> int:
    from spotify_mcp.analytics import fixture

    kwargs = {}
    if args.seed is not None:
        kwargs["seed"] = args.seed
    if args.plays is not None:
        kwargs["total_plays"] = args.plays

    out_dir = Path(args.out_dir)
    path = fixture.write_fixture(out_dir, **kwargs)
    print(f"Wrote synthetic streaming history to {path}")
    print(f"Ingest it with: spotify-mcp ingest {out_dir}")
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    from spotify_mcp.analytics.ingest import ingest

    settings = Settings.load()
    report = ingest(
        settings.analytics_db_path,
        Path(args.export_dir),
        local_timezone=settings.local_timezone,
        skip_ms_threshold=settings.skip_ms_threshold,
        substantial_ms=settings.substantial_ms,
    )
    print(f"Ingest run {report.run_id}:")
    print(f"  files ingested:            {report.files_ingested}")
    print(f"  files already ingested:    {report.files_skipped_already_ingested}")
    print(f"  rows inserted this run:    {report.rows_inserted}")
    print(f"  total plays after rebuild: {report.total_plays_after_rebuild}")
    if report.unknown_fields:
        print(f"  unrecognized fields seen:  {report.unknown_fields}")
    print(f"Database: {settings.analytics_db_path}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="spotify-mcp")
    subparsers = parser.add_subparsers(dest="command", required=True)

    login = subparsers.add_parser("login", help="Authorize this app with your Spotify account")
    login.set_defaults(func=_cmd_login)

    serve = subparsers.add_parser("serve", help="Run the MCP server over stdio")
    serve.set_defaults(func=_cmd_serve)

    fixture = subparsers.add_parser(
        "make-fixture", help="Generate a synthetic streaming-history export for testing"
    )
    fixture.add_argument(
        "out_dir", nargs="?", default="./fixture", help="Directory to write the fixture into"
    )
    fixture.add_argument("--seed", type=int, default=None, help="Override the random seed")
    fixture.add_argument("--plays", type=int, default=None, help="Override the number of plays")
    fixture.set_defaults(func=_cmd_make_fixture)

    ingest_cmd = subparsers.add_parser(
        "ingest", help="Load a streaming-history export (or fixture) into the local database"
    )
    ingest_cmd.add_argument("export_dir", help="Directory containing the exported .json files")
    ingest_cmd.set_defaults(func=_cmd_ingest)

    args = parser.parse_args()
    try:
        sys.exit(args.func(args))
    except SpotifyMCPError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
