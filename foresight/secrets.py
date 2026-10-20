"""GitHub token: $GITHUB_TOKEN, else .github_token."""

from __future__ import annotations

from . import config

_TOKEN_FILE = config.REPO_ROOT / ".github_token"


def github_token() -> str | None:
    import os

    env = os.environ.get("GITHUB_TOKEN")
    if env:
        return env.strip()
    if _TOKEN_FILE.exists():
        tok = _TOKEN_FILE.read_text().strip()
        return tok or None
    return None
