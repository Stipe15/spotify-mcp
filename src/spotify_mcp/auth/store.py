"""Token file persistence, with best-effort access hardening.

`os.chmod(0o600)` is meaningful ACL enforcement on POSIX and a near no-op on
Windows (it only toggles the read-only attribute). On Windows we additionally
reset the file's ACL with `icacls` to grant only the current user. If that
fails, we log a loud warning rather than pretend the file is protected —
see PLAN.md R5.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from spotify_mcp.logging import get_logger

logger = get_logger("auth.store")


@dataclass
class StoredToken:
    access_token: str
    refresh_token: str
    expires_at: float  # unix timestamp
    scope: str
    obtained_at: float

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, raw: str) -> StoredToken:
        return cls(**json.loads(raw))


def _harden_posix(path: Path) -> None:
    os.chmod(path, 0o600)


def _harden_windows(path: Path) -> None:
    user = os.environ.get("USERNAME") or os.environ.get("USERDOMAIN\\USERNAME")
    if not user:
        logger.warning("could not determine current user; skipping icacls hardening of %s", path)
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning(
            "icacls hardening of %s failed: %s — permissions are not restricted", path, exc
        )


def harden_permissions(path: Path) -> None:
    if platform.system() == "Windows":
        _harden_windows(path)
    else:
        _harden_posix(path)


def write_token(path: Path, token: StoredToken) -> None:
    """Write atomically: temp file in the same directory, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".token-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token.to_json())
        harden_permissions(tmp_path)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
    harden_permissions(path)


def read_token(path: Path) -> StoredToken | None:
    if not path.exists():
        return None
    return StoredToken.from_json(path.read_text(encoding="utf-8"))


def clear_token(path: Path) -> None:
    path.unlink(missing_ok=True)
