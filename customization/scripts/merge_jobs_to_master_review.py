#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
Merge completed interleaved CVAT jobs into one master-review annotation payload.

The anchor job defines final track identity. Stripe jobs are treated as
owners for their included task frames; if an owner job has a keyframe on its
owned frame, that keyframe replaces the anchor keyframe in the merged result.

This script does not synthesize interpolation points. CVAT tracks are sparse,
so intermediate boxes remain CVAT's normal track interpolation.

Environment:
  CVAT_API_URL  e.g. http://localhost:8080
  CVAT_ACCESS_TOKEN / CVAT_TOKEN / CVAT_API_TOKEN
  or CVAT_USER + CVAT_PASSWORD

Examples:
  python customization/scripts/merge_jobs_to_master_review.py --task-id 7 --dry-run

  python customization/scripts/merge_jobs_to_master_review.py \
    --anchor-job 7 --stripe-jobs 8-16 --output-dir /tmp/cvat-merge --dry-run

  python customization/scripts/merge_jobs_to_master_review.py \
    --anchor-job 7 --stripe-jobs 8-16 --apply-job 42
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import requests

from cvat_api_env import cvat_session_from_env
from cvat_frame_map import (
    frame_mapping_info,
    included_task_frames,
    load_task_data_meta,
    task_frame_to_source_media_frame,
)


def _session() -> requests.Session:
    return cvat_session_from_env()


def _api(s: requests.Session, method: str, path: str, **kwargs: Any) -> requests.Response:
    url = f"{s.base_url}{path}"  # type: ignore[attr-defined]
    return s.request(method, url, **kwargs)


def _parse_job_range(spec: str) -> list[int]:
    spec = spec.strip()
    if "-" in spec and "," not in spec:
        a, b = spec.split("-", 1)
        lo, hi = int(a), int(b)
        if lo > hi:
            lo, hi = hi, lo
        return list(range(lo, hi + 1))
    return [int(x.strip()) for x in spec.split(",") if x.strip()]


def _task_annotation_jobs(s: requests.Session, task_id: int) -> list[dict[str, Any]]:
    r = _api(s, "GET", "/api/jobs", params={"task_id": task_id, "page_size": "500"})
    r.raise_for_status()
    payload = r.json()
    jobs = payload.get("results", payload)
    annotation_jobs = [job for job in jobs if job.get("type") == "annotation"]
    annotation_jobs.sort(key=lambda job: int(job["id"]))
    return annotation_jobs


def _completed_annotation_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        job
        for job in jobs
        if job.get("stage") == "annotation" and job.get("state") == "completed"
    ]


def _print_job_plan(anchor_job: int, stripe_jobs: list[int], task_jobs: list[dict[str, Any]]) -> None:
    print(f"anchor job: {anchor_job}")
    print(f"stripe jobs: {','.join(map(str, stripe_jobs))}")
    if not task_jobs:
        return
    print("task annotation jobs:")
    for index, job in enumerate(task_jobs):
        job_id = int(job["id"])
        role = "anchor" if job_id == anchor_job else "stripe" if job_id in stripe_jobs else "ignored"
        print(
            f"  {index + 1}. job_id={job_id} role={role} "
            f"frames={job.get('frame_count')} stage={job.get('stage')} "
            f"state={job.get('state')} assignee={job.get('assignee')}"
        )


def _strip_shape(shape: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(shape)
    out.pop("id", None)
    return out


def _strip_track(track: dict[str, Any]) -> dict[str, Any]:
    shapes = [_strip_shape(s) for s in track.get("shapes", [])]
    shapes.sort(key=lambda s: int(s["frame"]))
    out: dict[str, Any] = {
        "label_id": track["label_id"],
        "frame": int(shapes[0]["frame"]) if shapes else int(track.get("frame", 0)),
        "group": track.get("group", 0),
        "source": "manual",
        "shapes": shapes,
        "attributes": copy.deepcopy(track.get("attributes", [])),
    }
    elements = track.get("elements") or []
    if elements:
        cleaned = []
        for element in elements:
            e = copy.deepcopy(element)
            e.pop("id", None)
            cleaned.append(e)
        out["elements"] = cleaned
    return out


def _assignee_fields(job: dict[str, Any]) -> tuple[str | None, int | None]:
    assignee = job.get("assignee")
    if isinstance(assignee, dict):
        username = assignee.get("username") or assignee.get("email")
        user_id = assignee.get("id")
        if user_id is not None:
            return username, int(user_id)
        return username, None
    if assignee is None:
        return None, None
    return str(assignee), None


def _provenance_actor(job: dict[str, Any], job_id: int) -> dict[str, Any]:
    assignee_username, assignee_id = _assignee_fields(job)
    return {
        "job_id": job_id,
        "assignee": assignee_username,
        "assignee_username": assignee_username,
        "assignee_id": assignee_id,
    }


def _load_job(s: requests.Session, job_id: int) -> dict[str, Any]:
    jr = _api(s, "GET", f"/api/jobs/{job_id}")
    jr.raise_for_status()
    mr = _api(s, "GET", f"/api/jobs/{job_id}/data/meta")
    mr.raise_for_status()
    ar = _api(s, "GET", f"/api/jobs/{job_id}/annotations")
    ar.raise_for_status()
    return {
        "job": jr.json(),
        "meta": mr.json(),
        "ann": ar.json(),
    }


def _shape_map(track: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {int(shape["frame"]): _strip_shape(shape) for shape in track.get("shapes", [])}


def _merge(
    s: requests.Session,
    *,
    anchor_id: int,
    stripe_ids: list[int],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    anchor = _load_job(s, anchor_id)
    task_meta = load_task_data_meta(s, int(anchor["job"]["task_id"]))
    anchor_tracks = anchor["ann"].get("tracks") or []
    if not anchor_tracks:
        raise RuntimeError(f"Anchor job {anchor_id} has no tracks")

    merged_tracks = [_strip_track(track) for track in anchor_tracks]
    merged_shape_maps = [_shape_map(track) for track in merged_tracks]
    anchor_task_frames = set(included_task_frames(anchor["meta"]))

    provenance: dict[str, Any] = {
        "anchor_job_id": anchor_id,
        "task_id": anchor["job"].get("task_id"),
        "strategy": "anchor_tracks_with_stripe_keyframe_overrides",
        "track_match": "track_index",
        "frame_mapping": frame_mapping_info(task_meta),
        "frame_sources": {},
        "jobs": {
            str(anchor_id): {
                "job_id": anchor_id,
                "state": anchor["job"].get("state"),
                **_provenance_actor(anchor["job"], anchor_id),
                "owned_task_frames": sorted(anchor_task_frames),
            }
        },
    }
    warnings: list[str] = []
    source_counts: Counter[str] = Counter()
    fallback_counts: Counter[str] = Counter()
    override_counts: Counter[str] = Counter()

    for frame in sorted(anchor_task_frames):
        provenance["frame_sources"][str(frame)] = {
            **_provenance_actor(anchor["job"], anchor_id),
            "source_media_frame": task_frame_to_source_media_frame(task_meta, frame),
            "rule": "anchor",
        }
        source_counts[str(anchor_id)] += 1

    for stripe_id in stripe_ids:
        loaded = _load_job(s, stripe_id)
        job = loaded["job"]
        ann = loaded["ann"]
        tracks = ann.get("tracks") or []
        owned_frames = included_task_frames(loaded["meta"])
        provenance["jobs"][str(stripe_id)] = {
            "job_id": stripe_id,
            "state": job.get("state"),
            **_provenance_actor(job, stripe_id),
            "owned_task_frames": owned_frames,
        }

        if len(tracks) != len(merged_tracks):
            warnings.append(
                f"job {stripe_id}: track count {len(tracks)} differs from anchor count {len(merged_tracks)}"
            )

        for track_index, merged_map in enumerate(merged_shape_maps):
            if track_index >= len(tracks):
                for frame in owned_frames:
                    fallback_counts[str(stripe_id)] += 1
                continue

            owner_shapes = _shape_map(tracks[track_index])
            for frame in owned_frames:
                shape = owner_shapes.get(frame)
                if shape is None:
                    fallback_counts[str(stripe_id)] += 1
                    continue

                merged_map[frame] = shape
                override_counts[str(stripe_id)] += 1

                provenance["frame_sources"][str(frame)] = {
                    **_provenance_actor(job, stripe_id),
                    "source_media_frame": task_frame_to_source_media_frame(task_meta, frame),
                    "rule": "stripe_owner_keyframe",
                }
                source_counts[str(stripe_id)] += 1

    for track, shape_by_frame in zip(merged_tracks, merged_shape_maps):
        shapes = [shape_by_frame[frame] for frame in sorted(shape_by_frame)]
        track["shapes"] = shapes
        track["frame"] = int(shapes[0]["frame"]) if shapes else int(track.get("frame", 0))

    payload = {
        "version": 0,
        "tags": [],
        "shapes": [],
        "tracks": merged_tracks,
    }
    provenance["summary"] = {
        "track_count": len(merged_tracks),
        "track_shape_counts": [len(track["shapes"]) for track in merged_tracks],
        "frame_source_counts": dict(sorted(source_counts.items(), key=lambda item: int(item[0]))),
        "override_keyframe_counts": dict(sorted(override_counts.items(), key=lambda item: int(item[0]))),
        "fallback_missing_keyframe_counts": dict(
            sorted(fallback_counts.items(), key=lambda item: int(item[0]))
        ),
    }
    return payload, provenance, warnings


def _write_outputs(output_dir: Path, payload: dict[str, Any], provenance: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "merged_annotations.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "provenance_manifest.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--task-id", type=int, help="Infer anchor/stripe jobs from task annotation jobs")
    p.add_argument("--anchor-job", type=int, help="Defaults to first annotation job when --task-id is used")
    p.add_argument(
        "--stripe-jobs",
        help="e.g. 8-16 or 8,9,10; defaults to all non-anchor annotation jobs when --task-id is used",
    )
    p.add_argument("--output-dir", type=Path, help="Defaults to tmp/task-<task_id> with --task-id")
    p.add_argument("--apply-job", type=int, help="PUT merged annotations into this master-review job")
    p.add_argument("--dry-run", action="store_true", help="Do not PUT to --apply-job")
    args = p.parse_args()

    s = _session()
    task_jobs: list[dict[str, Any]] = []
    if args.task_id is not None:
        task_jobs = _task_annotation_jobs(s, args.task_id)
        eligible_jobs = _completed_annotation_jobs(task_jobs)
        if len(eligible_jobs) < 2:
            print(
                f"Task {args.task_id} needs at least 2 completed annotation-stage jobs for merge; "
                f"found {len(eligible_jobs)}",
                file=sys.stderr,
            )
            sys.exit(1)
        anchor_id = args.anchor_job or int(eligible_jobs[0]["id"])
        if args.stripe_jobs:
            stripe_ids = _parse_job_range(args.stripe_jobs)
        else:
            stripe_ids = [int(job["id"]) for job in eligible_jobs if int(job["id"]) != anchor_id]
        output_dir = args.output_dir or Path(f"tmp/task-{args.task_id}")
    else:
        if args.anchor_job is None or args.stripe_jobs is None:
            p.error("provide either --task-id, or both --anchor-job and --stripe-jobs")
        anchor_id = args.anchor_job
        stripe_ids = _parse_job_range(args.stripe_jobs)
        output_dir = args.output_dir or Path("tmp/cvat-task-output")

    if anchor_id in stripe_ids:
        print(f"anchor job {anchor_id} must not be included in stripe jobs", file=sys.stderr)
        sys.exit(2)
    if not stripe_ids:
        print("No stripe jobs selected", file=sys.stderr)
        sys.exit(2)

    _print_job_plan(anchor_id, stripe_ids, task_jobs)
    payload, provenance, warnings = _merge(s, anchor_id=anchor_id, stripe_ids=stripe_ids)
    _write_outputs(output_dir, payload, provenance)

    summary = provenance["summary"]
    print(f"wrote {output_dir / 'merged_annotations.json'}")
    print(f"wrote {output_dir / 'provenance_manifest.json'}")
    print(f"tracks: {summary['track_count']} shape_counts={summary['track_shape_counts']}")
    print(f"frame_source_counts: {summary['frame_source_counts']}")
    print(f"override_keyframe_counts: {summary['override_keyframe_counts']}")
    print(f"fallback_missing_keyframe_counts: {summary['fallback_missing_keyframe_counts']}")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if args.apply_job and not args.dry_run:
        current = _api(s, "GET", f"/api/jobs/{args.apply_job}/annotations")
        current.raise_for_status()
        payload["version"] = int(current.json().get("version", 0))
        response = _api(s, "PUT", f"/api/jobs/{args.apply_job}/annotations", json=payload)
        if not response.ok:
            print(response.text, file=sys.stderr)
        response.raise_for_status()
        print(f"applied merged annotations to job {args.apply_job}")


if __name__ == "__main__":
    main()
