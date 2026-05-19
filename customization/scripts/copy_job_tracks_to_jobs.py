#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
Copy tracks from the anchor CVAT job into target stripe jobs.

This intentionally copies the full track keyframe sequence. CVAT interpolation
tracks are sparse: GET /annotations returns keyframes, while the UI renders
interpolated boxes on intermediate frames. Filtering keyframes by a target
job's included_frames breaks that behavior.

Use after interleaved/stripe jobs so stripe jobs inherit overlapping rectangles.

Environment (same style as cvat_interleaved_jobs.py):
  CVAT_API_URL  e.g. http://localhost:8080
  CVAT_ACCESS_TOKEN / CVAT_TOKEN / CVAT_API_TOKEN
  or CVAT_USER + CVAT_PASSWORD

Example:
  # Infer job_anchor + stripe jobs from task annotation jobs:
  python customization/scripts/copy_job_tracks_to_jobs.py --task-id 7 --dry-run
  python customization/scripts/copy_job_tracks_to_jobs.py --task-id 7
  python customization/scripts/copy_job_tracks_to_jobs.py --task-id 7 --assignees user1,user2,user3

  # Or provide explicit job ids:
  python customization/scripts/copy_job_tracks_to_jobs.py --anchor-job 7 --target-jobs 8-16
"""

from __future__ import annotations

import argparse
import copy
import sys
from typing import Any

import requests

from cvat_api_env import cvat_session_from_env


def _session() -> requests.Session:
    return cvat_session_from_env()


def _api(s: requests.Session, method: str, path: str, **kwargs: Any) -> requests.Response:
    url = f"{s.base_url}{path}"  # type: ignore[attr-defined]
    return s.request(method, url, **kwargs)


def _strip_shape(shape: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(shape)
    out.pop("id", None)
    return out


def _strip_track_for_create(track: dict[str, Any]) -> dict[str, Any] | None:
    shapes = [_strip_shape(s) for s in track.get("shapes", [])]
    if not shapes:
        return None
    shapes.sort(key=lambda s: int(s["frame"]))
    out: dict[str, Any] = {
        "label_id": track["label_id"],
        "frame": int(shapes[0]["frame"]),
        "group": track.get("group", 0),
        "source": "manual",
        "shapes": shapes,
        "attributes": copy.deepcopy(track.get("attributes", [])),
    }
    elements = track.get("elements") or []
    if elements:
        cleaned: list[dict[str, Any]] = []
        for el in elements:
            e = copy.deepcopy(el)
            e.pop("id", None)
            cleaned.append(e)
        out["elements"] = cleaned
    return out


def _parse_job_range(spec: str) -> list[int]:
    spec = spec.strip()
    if "-" in spec and "," not in spec:
        a, b = spec.split("-", 1)
        lo, hi = int(a), int(b)
        if lo > hi:
            lo, hi = hi, lo
        return list(range(lo, hi + 1))
    return [int(x.strip()) for x in spec.split(",") if x.strip()]


def _parse_csv(spec: str | None) -> list[str]:
    if not spec:
        return []
    return [item.strip() for item in spec.split(",") if item.strip()]


def _task_annotation_jobs(s: requests.Session, task_id: int) -> list[dict[str, Any]]:
    r = _api(s, "GET", "/api/jobs", params={"task_id": task_id, "page_size": "500"})
    r.raise_for_status()
    payload = r.json()
    jobs = payload.get("results", payload)
    annotation_jobs = [job for job in jobs if job.get("type") == "annotation"]
    annotation_jobs.sort(key=lambda job: int(job["id"]))
    return annotation_jobs


def _resolve_user(s: requests.Session, username: str) -> dict[str, Any]:
    r = _api(s, "GET", "/api/users", params={"username": username, "page_size": "10"})
    r.raise_for_status()
    payload = r.json()
    users = payload.get("results", payload)
    exact = [user for user in users if user.get("username") == username]

    if not exact:
        r = _api(s, "GET", "/api/users", params={"search": username, "page_size": "20"})
        r.raise_for_status()
        payload = r.json()
        users = payload.get("results", payload)
        exact = [user for user in users if user.get("username") == username]

    if len(exact) != 1:
        found = ", ".join(str(user.get("username")) for user in users) if users else "none"
        raise RuntimeError(f"Could not resolve username {username!r} exactly (found: {found})")
    return exact[0]


def _resolve_assignees(s: requests.Session, usernames: list[str]) -> list[dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    users: list[dict[str, Any]] = []
    for username in usernames:
        if username not in cache:
            cache[username] = _resolve_user(s, username)
        users.append(cache[username])
    return users


def _assignment_plan(target_jobs: list[int], assignees: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    if not assignees:
        return {}
    return {
        job_id: assignees[index % len(assignees)]
        for index, job_id in enumerate(target_jobs)
    }


def _print_job_plan(anchor_job: int, target_jobs: list[int], task_jobs: list[dict[str, Any]]) -> None:
    print(f"anchor job: {anchor_job}")
    print(f"target jobs: {','.join(map(str, target_jobs))}")
    if not task_jobs:
        return
    print("task annotation jobs:")
    for index, job in enumerate(task_jobs):
        job_id = int(job["id"])
        role = "anchor" if job_id == anchor_job else "target" if job_id in target_jobs else "ignored"
        print(
            f"  {index + 1}. job_id={job_id} role={role} "
            f"frames={job.get('frame_count')} assignee={job.get('assignee')}"
        )


def _print_assignment_plan(plan: dict[int, dict[str, Any]]) -> None:
    if not plan:
        return
    print("assignment plan:")
    for job_id, user in plan.items():
        print(f"  job_id={job_id} -> {user.get('username')} (id={user.get('id')})")


def _assign_job(s: requests.Session, job_id: int, user: dict[str, Any]) -> None:
    response = _api(s, "PATCH", f"/api/jobs/{job_id}", json={"assignee": int(user["id"])})
    if not response.ok:
        print(response.text, file=sys.stderr)
    response.raise_for_status()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--task-id", type=int, help="Infer anchor/target jobs from task annotation jobs")
    p.add_argument("--anchor-job", type=int, help="Defaults to first annotation job when --task-id is used")
    p.add_argument(
        "--target-jobs",
        type=str,
        help="e.g. 8-16 or 8,9,10; defaults to all non-anchor annotation jobs when --task-id is used",
    )
    p.add_argument(
        "--assignees",
        help="Comma-separated CVAT usernames assigned to target jobs in order; repeats round-robin",
    )
    p.add_argument("--dry-run", action="store_true", help="Print plan and counts only, no PUT/PATCH")
    args = p.parse_args()

    s = _session()
    task_jobs: list[dict[str, Any]] = []
    if args.task_id is not None:
        task_jobs = _task_annotation_jobs(s, args.task_id)
        if not task_jobs:
            print(f"No annotation jobs found for task {args.task_id}", file=sys.stderr)
            sys.exit(1)
        anchor_id = args.anchor_job or int(task_jobs[0]["id"])
        if args.target_jobs:
            targets = _parse_job_range(args.target_jobs)
        else:
            targets = [int(job["id"]) for job in task_jobs if int(job["id"]) != anchor_id]
    else:
        if args.anchor_job is None or args.target_jobs is None:
            p.error("provide either --task-id, or both --anchor-job and --target-jobs")
        anchor_id = args.anchor_job
        targets = _parse_job_range(args.target_jobs)

    if anchor_id in targets:
        print(f"anchor job {anchor_id} must not be included in target jobs", file=sys.stderr)
        sys.exit(2)
    if not targets:
        print("No target jobs selected", file=sys.stderr)
        sys.exit(2)

    _print_job_plan(anchor_id, targets, task_jobs)
    assignee_names = _parse_csv(args.assignees)
    try:
        assignees = _resolve_assignees(s, assignee_names)
    except RuntimeError as ex:
        print(str(ex), file=sys.stderr)
        sys.exit(2)
    assignments = _assignment_plan(targets, assignees)
    _print_assignment_plan(assignments)

    r = _api(s, "GET", f"/api/jobs/{anchor_id}/annotations")
    r.raise_for_status()
    anchor_ann: dict[str, Any] = r.json()
    anchor_tracks = anchor_ann.get("tracks") or []
    if not anchor_tracks:
        print(f"No tracks on anchor job {anchor_id}", file=sys.stderr)
        sys.exit(1)

    for tid in targets:
        new_tracks: list[dict[str, Any]] = []
        for tr in anchor_tracks:
            nt = _strip_track_for_create(tr)
            if nt:
                new_tracks.append(nt)

        ar = _api(s, "GET", f"/api/jobs/{tid}/annotations")
        ar.raise_for_status()
        cur = ar.json()
        version = int(cur.get("version", 0))

        payload = {
            "version": version,
            "tags": [],
            "shapes": [],
            "tracks": new_tracks,
        }

        print(f"job {tid}: tracks={len(new_tracks)}", end="")
        if new_tracks:
            nsh = sum(len(t["shapes"]) for t in new_tracks)
            print(f" (shapes={nsh})")
        else:
            print()

        if args.dry_run:
            continue

        pr = _api(s, "PUT", f"/api/jobs/{tid}/annotations", json=payload)
        if not pr.ok:
            print(pr.text, file=sys.stderr)
        pr.raise_for_status()

        if assignee := assignments.get(tid):
            _assign_job(s, tid, assignee)
            print(f"assigned job {tid} -> {assignee.get('username')} (id={assignee.get('id')})")


if __name__ == "__main__":
    main()
