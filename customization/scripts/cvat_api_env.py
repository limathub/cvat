# SPDX-License-Identifier: MIT
"""Shared CVAT REST session from environment (host + auth)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


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


def _cvat_base_url() -> str:
    """
    REST base URL for customization scripts.

    Prefers CVAT_API_URL, then CVAT_HOST. Bare hostnames (as used for Traefik in
    .env) get http:// and :8080 when no port is present.
    """
    raw = (
        os.environ.get("CVAT_API_URL")
        or os.environ.get("CVAT_HOST")
        or "http://localhost:8080"
    ).strip().rstrip("/")
    if not raw:
        raw = "http://localhost:8080"
    if not raw.startswith(("http://", "https://")):
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    if parsed.hostname and parsed.port is None:
        netloc = f"{parsed.hostname}:8080"
        if parsed.username:
            auth = parsed.username
            if parsed.password:
                auth = f"{auth}:{parsed.password}"
            netloc = f"{auth}@{netloc}"
        raw = f"{parsed.scheme}://{netloc}{parsed.path or ''}".rstrip("/")
    return raw


def cvat_session_from_env(*, require: bool = True) -> requests.Session:
    """
    Auth (first match wins):
      - CVAT_ACCESS_TOKEN or CVAT_TOKEN or CVAT_API_TOKEN: Authorization: Bearer <token>
      - CVAT_USER + CVAT_PASSWORD: HTTP Basic

    Host:
      - CVAT_API_URL or CVAT_HOST (default http://localhost:8080)

    Loads .env from the repository root when the file exists. Variables already
    exported in the shell take precedence (laptop CI, one-off overrides).

    If require=False and no credentials are set, returns a session with only
    base_url/origin set (for dry-run paths that never call the API).
    """
    _load_dotenv_if_present()
    host = _cvat_base_url()
    token = (
        os.environ.get("CVAT_ACCESS_TOKEN")
        or os.environ.get("CVAT_TOKEN")
        or os.environ.get("CVAT_API_TOKEN")
        or ""
    ).strip()
    s = requests.Session()
    s.headers.setdefault("Origin", host)
    s.base_url = host  # type: ignore[attr-defined]

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
