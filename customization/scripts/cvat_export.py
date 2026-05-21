# SPDX-License-Identifier: MIT
"""CVAT v2 dataset export via REST (jobs/tasks)."""

from __future__ import annotations

import time
from typing import Any

import requests


def _api(s: requests.Session, method: str, path: str, **kwargs: Any) -> requests.Response:
    url = f"{s.base_url}{path}"  # type: ignore[attr-defined]
    return s.request(method, url, **kwargs)


def wait_request(
    s: requests.Session,
    rq_id: str,
    *,
    timeout_s: int = 3600,
    poll_s: float = 1.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rr = _api(s, "GET", f"/api/requests/{rq_id}")
        rr.raise_for_status()
        body = rr.json()
        status = body.get("status")
        if status in ("finished", "failed"):
            return body
        time.sleep(poll_s)
    raise TimeoutError(f"rq_id={rq_id} did not finish within {timeout_s}s")


def export_job_dataset(
    s: requests.Session,
    job_id: int,
    format_name: str,
    *,
    save_images: bool = False,
    filename: str | None = None,
    timeout_s: int = 3600,
) -> bytes:
    params: dict[str, str] = {
        "format": format_name,
        "save_images": "true" if save_images else "false",
    }
    if filename:
        params["filename"] = filename

    init = _api(s, "POST", f"/api/jobs/{job_id}/dataset/export", params=params)
    if init.status_code not in (200, 202):
        init.raise_for_status()
    body = init.json()
    rq_id = body.get("rq_id")
    if not rq_id:
        raise RuntimeError(f"No rq_id in export response: {body}")

    result = wait_request(s, rq_id, timeout_s=timeout_s)
    if result.get("status") != "finished":
        raise RuntimeError(f"Export failed for job {job_id} format={format_name!r}: {result}")

    result_url = result.get("result_url")
    if not result_url:
        raise RuntimeError(f"No result_url after export: {result}")

    download = s.get(result_url)
    download.raise_for_status()
    return download.content
