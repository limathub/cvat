# SPDX-License-Identifier: MIT
"""Shared CVAT REST session from environment (API base URL + auth)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import requests

_DEFAULT_API_URL = "http://localhost:8080"


def _load_dotenv_if_present() -> None:
    """Load repo-root .env when present; never override existing environment variables."""
    for directory in (Path.cwd(), *Path.cwd().parents):
        path = directory / ".env"
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = value
        return


def cvat_session_from_env(*, require: bool = True) -> requests.Session:
    """
    Auth (first match wins):
      - CVAT_ACCESS_TOKEN or CVAT_TOKEN or CVAT_API_TOKEN: Authorization: Bearer <token>
      - CVAT_USER + CVAT_PASSWORD: HTTP Basic

    API base URL:
      - CVAT_API_URL (default http://localhost:8080)

    Do not use CVAT_HOST here — that name is reserved for docker compose / Traefik
    (hostname only, e.g. localhost or james-26-laptop).

    Loads .env from the repository root when the file exists. Variables already
    exported in the shell take precedence.

    If require=False and no credentials are set, returns a session with only
    base_url/origin set (for dry-run paths that never call the API).
    """
    _load_dotenv_if_present()

    if os.environ.get("CVAT_HOST") and not os.environ.get("CVAT_API_URL"):
        print(
            "warning: CVAT_HOST is set but CVAT_API_URL is not. "
            "Customization scripts use CVAT_API_URL (full URL, e.g. "
            "http://localhost:8080). For docker compose, keep CVAT_HOST as a "
            "hostname only in .env — do not export http://... in the same shell.",
            file=sys.stderr,
        )

    api_url = os.environ.get("CVAT_API_URL", _DEFAULT_API_URL).rstrip("/")
    token = (
        os.environ.get("CVAT_ACCESS_TOKEN")
        or os.environ.get("CVAT_TOKEN")
        or os.environ.get("CVAT_API_TOKEN")
        or ""
    ).strip()
    s = requests.Session()
    s.headers.setdefault("Origin", api_url)
    s.base_url = api_url  # type: ignore[attr-defined]

    if token:
        s.headers["Authorization"] = f"Bearer {token}"
        return s

    user = os.environ.get("CVAT_USER")
    password = os.environ.get("CVAT_PASSWORD")
    if user and password:
        s.auth = (user, password)
        return s

    if require:
        print(
            "CVAT auth: set CVAT_ACCESS_TOKEN (Bearer) or CVAT_USER + CVAT_PASSWORD",
            file=sys.stderr,
        )
        sys.exit(2)
    return s
