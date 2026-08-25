"""Pydantic models for the post-February-2026 Spotify response shapes.

Only the fields that still exist are modelled. Unknown fields from the API
are ignored rather than rejected — see PLAN.md §5 "Response models".
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ExternalUrls(BaseModel):
    model_config = ConfigDict(extra="ignore")
    spotify: str | None = None


class Image(BaseModel):
    model_config = ConfigDict(extra="ignore")
    url: str
    height: int | None = None
    width: int | None = None


class Me(BaseModel):
    """GET /me. No `email`, `country`, `product`, `explicit_content`, or
    `followers` — all removed from the user object in February 2026.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    display_name: str | None = None
    uri: str
    external_urls: ExternalUrls = ExternalUrls()
    images: list[Image] = []
