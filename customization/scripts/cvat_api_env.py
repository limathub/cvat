# SPDX-License-Identifier: MIT
"""Shared CVAT REST session from environment (host + auth)."""

from __future__ import annotations

import os
import sys
from typing import Any

import requests


def cvat_session_from_env(*, require: bool = True) -> requests.Session:
    """
    Auth (first match wins):
      - CVAT_ACCESS_TOKEN or CVAT_TOKEN or CVAT_API_TOKEN: Authorization: Bearer <token>
      - CVAT_USER + CVAT_PASSWORD: HTTP Basic

    Host:
      - CVAT_HOST (default http://localhost:8080)

    If require=False and no credentials are set, returns a session with only
    base_url/origin set (for dry-run paths that never call the API).
    """
    host = os.environ.get("CVAT_HOST", "http://localhost:8080").rstrip("/")
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
