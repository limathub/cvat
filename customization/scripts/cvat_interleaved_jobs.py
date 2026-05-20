#!/usr/bin/env python3
# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT
"""
Rebuild annotation jobs on a task into:
  - job_anchor: first anchor_job_size task-relative frames
    (default 3 * num_stripe_jobs)
  - Stripe jobs: one per offset 0..num_stripe_jobs-1, with frames
    offset + k * num_stripe_jobs

Requires CVAT with:
  - POST /api/jobs accepting type=annotation + frame_selection_method=manual + frames=[...]
  - DELETE /api/jobs/{id} for annotation jobs (patched JobViewSet.perform_destroy)

Environment:
  CVAT_API_URL  e.g. http://localhost:8080

  Authentication (one of):
    CVAT_ACCESS_TOKEN or CVAT_TOKEN or CVAT_API_TOKEN (Bearer)
    or CVAT_USER + CVAT_PASSWORD

Example (after you created a new task, uploaded the same video, set frame_filter step=3, etc.):

  export CVAT_API_URL=http://localhost:8080 CVAT_USER=admin CVAT_PASSWORD=...
  python customization/scripts/cvat_interleaved_jobs.py rebuild --task-id 12

Use --dry-run to only print frame lists.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import requests

from cvat_api_env import cvat_session_from_env

DEFAULT_NUM_STRIPE_JOBS = 6


def _session() -> requests.Session:
    return cvat_session_from_env()


def _api(s: requests.Session, method: str, path: str, **kwargs) -> requests.Response:
    url = f"{s.base_url}{path}"  # type: ignore[attr-defined]
    return s.request(method, url, **kwargs)


def rebuild_interleaved_jobs(
    s: requests.Session,
    tid: int,
    *,
    anchor_job_size: int | None = None,
    stripe_step: int = DEFAULT_NUM_STRIPE_JOBS,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Delete all annotation jobs on task, create job_anchor + stripe manual-frame jobs.

    ``anchor_job_size`` is the number of task-relative frames in job_anchor
    (indices ``0 .. anchor_job_size-1``).
    If it exceeds task ``size``, it is clamped (with a note on stderr).

    Returns list of created job dicts from the API (each includes id, frame_count, ...).
    """
    r = _api(s, "GET", f"/api/tasks/{tid}")
    r.raise_for_status()
    task: dict[str, Any] = r.json()

    if task.get("consensus_replicas", 0):
        print("This script requires consensus_replicas == 0 on the task.", file=sys.stderr)
        sys.exit(1)

    n = int(task["size"])
    if n < 1:
        print(f"task size is {n}; nothing to assign to jobs.", file=sys.stderr)
        sys.exit(1)
    if stripe_step < 1:
        print(f"num_stripe_jobs ({stripe_step}) must be >= 1", file=sys.stderr)
        sys.exit(1)
    if anchor_job_size is None:
        anchor_job_size = 3 * stripe_step
    if anchor_job_size < 1:
        print(f"anchor_job_size ({anchor_job_size}) must be >= 1", file=sys.stderr)
        sys.exit(1)

    anchor_size = min(anchor_job_size, n)
    if anchor_size < anchor_job_size:
        print(
            f"note: clamped anchor_job_size from {anchor_job_size} to {anchor_size} "
            f"(task size {n})",
            file=sys.stderr,
        )

    anchor_frames = list(range(0, anchor_size))
    stripe_jobs: list[tuple[int, list[int]]] = []
    for offset in range(stripe_step):
        j = offset + 2
        frames = list(range(offset, n, stripe_step))
        stripe_jobs.append((j, frames))

    print(f"task_id={tid} size={n}")
    print(
        f"job_anchor: {len(anchor_frames)} frames -> "
        f"{anchor_frames[:5]}{'...' if len(anchor_frames) > 5 else ''}"
    )
    for j, frames in stripe_jobs:
        preview = frames[:6]
        suf = "..." if len(frames) > 6 else ""
        print(f"job{j}: {len(frames)} frames -> {preview}{suf}")

    if dry_run:
        return []

    jobs_r = _api(s, "GET", f"/api/jobs", params={"task_id": tid, "page_size": "500"})
    jobs_r.raise_for_status()
    jobs_payload = jobs_r.json()
    jobs = jobs_payload.get("results", jobs_payload)

    for job in jobs:
        jid = job["id"]
        jt = job.get("type")
        if jt == "ground_truth":
            print(f"skip delete job {jid} (ground_truth)")
            continue
        if jt != "annotation":
            print(f"skip delete job {jid} (type={jt})")
            continue
        dr = _api(s, "DELETE", f"/api/jobs/{jid}")
        if dr.status_code not in (204, 200):
            print(f"DELETE /api/jobs/{jid} -> {dr.status_code} {dr.text}", file=sys.stderr)
            dr.raise_for_status()
        print(f"deleted job {jid}")

    def _create_manual_job(frames: list[int]) -> dict[str, Any]:
        pr = _api(
            s,
            "POST",
            "/api/jobs",
            json={
                "type": "annotation",
                "task_id": tid,
                "frame_selection_method": "manual",
                "frames": frames,
            },
        )
        if pr.status_code != 201:
            print(pr.text, file=sys.stderr)
        pr.raise_for_status()
        return pr.json()

    created: list[dict[str, Any]] = []
    anchor_job = _create_manual_job(anchor_frames)
    print(f"created job_anchor id={anchor_job['id']} frame_count={anchor_job.get('frame_count')}")
    created.append({"slot": "anchor", **anchor_job})

    for j, frames in stripe_jobs:
        if not frames:
            print(f"warning: job{j} has zero frames, skipping", file=sys.stderr)
            continue
        job = _create_manual_job(frames)
        print(f"created job{j} id={job['id']} frame_count={job.get('frame_count')}")
        created.append({"slot": j, **job})
    return created


def cmd_rebuild(args: argparse.Namespace) -> None:
    s = _session()
    rebuild_interleaved_jobs(
        s,
        args.task_id,
        anchor_job_size=args.anchor_job_size,
        stripe_step=args.stripe_step,
        dry_run=args.dry_run,
    )


def main() -> None:
    p = argparse.ArgumentParser(description="CVAT interleaved manual-frame annotation jobs")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("rebuild", help="Delete all annotation jobs on task, create job_anchor+stripes")
    r.add_argument("--task-id", type=int, required=True)
    r.add_argument(
        "--anchor-job-size",
        type=int,
        default=None,
        metavar="N",
        help="job_anchor includes N task-relative frames (indices 0..N-1; default 3 * --num-stripe-jobs)",
    )
    r.add_argument(
        "--num-stripe-jobs",
        "--num_stripe_jobs",
        type=int,
        default=DEFAULT_NUM_STRIPE_JOBS,
        dest="stripe_step",
        metavar="K",
        help=f"Number of stripe jobs to create, one per offset 0..K-1 (default {DEFAULT_NUM_STRIPE_JOBS})",
    )
    r.add_argument("--stripe-step", type=int, dest="stripe_step", help=argparse.SUPPRESS)
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_rebuild)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
