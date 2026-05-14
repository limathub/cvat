#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
Build a provenance/candidate manifest from multiple CVAT jobs.

The manifest keeps each job's raw sparse tracks so no annotator output is lost.
It also indexes exact keyframe boxes on owned task frames as frame/track
candidates. Optionally include admin final in the same file with --admin-job.

Environment:
  CVAT_HOST     e.g. http://localhost:8080
  CVAT_ACCESS_TOKEN / CVAT_TOKEN / CVAT_API_TOKEN
  or CVAT_USER + CVAT_PASSWORD
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter
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


def _print_job_plan(anchor_job: int, candidate_jobs: list[int], task_jobs: list[dict[str, Any]]) -> None:
    print(f"anchor job: {anchor_job}")
    print(f"candidate jobs: {','.join(map(str, candidate_jobs))}")
    if not task_jobs:
        return
    print("task annotation jobs:")
    for index, job in enumerate(task_jobs):
        job_id = int(job["id"])
        role = "anchor" if job_id == anchor_job else "candidate" if job_id in candidate_jobs else "ignored"
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
    shapes = [_strip_shape(shape) for shape in track.get("shapes", [])]
    shapes.sort(key=lambda shape: int(shape["frame"]))
    out: dict[str, Any] = {
        "label_id": track["label_id"],
        "frame": int(shapes[0]["frame"]) if shapes else int(track.get("frame", 0)),
        "group": track.get("group", 0),
        "source": track.get("source", "manual"),
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


def _load_job(s: requests.Session, job_id: int) -> dict[str, Any]:
    jr = _api(s, "GET", f"/api/jobs/{job_id}")
    jr.raise_for_status()
    mr = _api(s, "GET", f"/api/jobs/{job_id}/data/meta")
    mr.raise_for_status()
    ar = _api(s, "GET", f"/api/jobs/{job_id}/annotations")
    ar.raise_for_status()
    return {"job": jr.json(), "meta": mr.json(), "ann": ar.json()}


def _shape_by_frame(track: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {int(shape["frame"]): _strip_shape(shape) for shape in track.get("shapes", [])}


def _enrich_shape_source_media(shape: dict[str, Any], task_meta: dict[str, Any]) -> None:
    shape["source_media_frame"] = task_frame_to_source_media_frame(task_meta, int(shape["frame"]))


def _job_entry(loaded: dict[str, Any]) -> dict[str, Any]:
    job = loaded["job"]
    assignee_username, assignee_id = _assignee_fields(job)
    return {
        "id": job["id"],
        "task_id": job.get("task_id"),
        "state": job.get("state"),
        "assignee": assignee_username,
        "assignee_username": assignee_username,
        "assignee_id": assignee_id,
        "owned_task_frames": included_task_frames(loaded["meta"]),
        "raw_tracks": [_strip_track(track) for track in loaded["ann"].get("tracks", [])],
    }


def attach_admin_final(manifest: dict[str, Any], admin_job_id: int) -> dict[str, Any]:
    s = _session()
    task_id = manifest.get("task_id")
    if task_id is None:
        raise RuntimeError("manifest has no task_id")
    task_meta = load_task_data_meta(s, int(task_id))

    loaded = _load_job(s, admin_job_id)
    job = loaded["job"]
    assignee_username, assignee_id = _assignee_fields(job)
    raw_tracks = [_strip_track(track) for track in loaded["ann"].get("tracks", [])]
    for track in raw_tracks:
        for shape in track.get("shapes", []):
            _enrich_shape_source_media(shape, task_meta)

    shape_maps = [_shape_by_frame(track) for track in raw_tracks]
    attached_counts: Counter[str] = Counter()
    missing_counts: Counter[str] = Counter()

    out = copy.deepcopy(manifest)
    out["admin_final"] = {
        "job_id": admin_job_id,
        "task_id": job.get("task_id"),
        "assignee": assignee_username,
        "assignee_username": assignee_username,
        "assignee_id": assignee_id,
        "state": job.get("state"),
        "track_match": "track_index",
        "raw_tracks": raw_tracks,
    }

    for record in out.get("candidate_records", []):
        track_index = int(record["track_index"])
        frame = int(record["frame"])
        shape = shape_maps[track_index].get(frame) if track_index < len(shape_maps) else None
        if shape:
            _enrich_shape_source_media(shape, task_meta)
            record["admin_final"] = {
                "role": "admin_final",
                "job_id": admin_job_id,
                "assignee": assignee_username,
                "assignee_username": assignee_username,
                "assignee_id": assignee_id,
                "track_index": track_index,
                "shape": shape,
                "exact_keyframe": True,
            }
            attached_counts[str(track_index)] += 1
        else:
            record["admin_final"] = None
            missing_counts[str(track_index)] += 1

    out.setdefault("summary", {})
    out.setdefault("frame_mapping", frame_mapping_info(task_meta))
    out["summary"]["admin_final_track_count"] = len(raw_tracks)
    out["summary"]["admin_final_exact_keyframe_counts_by_track"] = dict(
        sorted(attached_counts.items(), key=lambda item: int(item[0]))
    )
    out["summary"]["admin_final_missing_keyframe_counts_by_track"] = dict(
        sorted(missing_counts.items(), key=lambda item: int(item[0]))
    )
    return out


def _candidate_for_shape(
    *,
    role: str,
    job: dict[str, Any],
    track_index: int,
    shape: dict[str, Any],
    task_meta: dict[str, Any],
) -> dict[str, Any]:
    sh = _strip_shape(shape)
    tf = int(sh["frame"])
    sh["source_media_frame"] = task_frame_to_source_media_frame(task_meta, tf)
    return {
        "role": role,
        "job_id": job["id"],
        "assignee": job.get("assignee_username"),
        "assignee_username": job.get("assignee_username"),
        "assignee_id": job.get("assignee_id"),
        "track_index": track_index,
        "shape": sh,
        "exact_keyframe": True,
    }


def build_manifest(anchor_job: int, candidate_jobs: list[int]) -> dict[str, Any]:
    s = _session()
    anchor_loaded = _load_job(s, anchor_job)
    candidate_loaded = [_load_job(s, job_id) for job_id in candidate_jobs]

    task_id = anchor_loaded["job"].get("task_id")
    if task_id is None:
        raise RuntimeError("anchor job has no task_id")
    task_meta = load_task_data_meta(s, int(task_id))

    anchor_entry = _job_entry(anchor_loaded)
    job_entries = {str(anchor_job): anchor_entry}
    for loaded in candidate_loaded:
        entry = _job_entry(loaded)
        job_entries[str(entry["id"])] = entry

    for entry in job_entries.values():
        for tr in entry["raw_tracks"]:
            for sh in tr.get("shapes", []):
                sh["source_media_frame"] = task_frame_to_source_media_frame(task_meta, int(sh["frame"]))

    anchor_tracks = anchor_entry["raw_tracks"]
    candidate_records: list[dict[str, Any]] = []
    candidate_counts: Counter[str] = Counter()
    missing_counts: Counter[str] = Counter()

    for loaded in candidate_loaded:
        entry = job_entries[str(loaded["job"]["id"])]
        job_id = entry["id"]
        owned_frames = set(entry["owned_task_frames"])
        tracks = entry["raw_tracks"]

        for track_index, anchor_track in enumerate(anchor_tracks):
            anchor_shapes = _shape_by_frame(anchor_track)
            job_shapes = _shape_by_frame(tracks[track_index]) if track_index < len(tracks) else {}

            for frame in sorted(owned_frames):
                anchor_shape = anchor_shapes.get(frame)
                job_shape = job_shapes.get(frame)
                if anchor_shape is None and job_shape is None:
                    continue

                record: dict[str, Any] = {
                    "frame": frame,
                    "source_media_frame": task_frame_to_source_media_frame(task_meta, frame),
                    "track_index": track_index,
                    "label_id": anchor_track.get("label_id"),
                    "anchor": (
                        _candidate_for_shape(
                            role="anchor",
                            job=anchor_entry,
                            track_index=track_index,
                            shape=anchor_shape,
                            task_meta=task_meta,
                        )
                        if anchor_shape
                        else None
                    ),
                    "candidates": [],
                }
                if job_shape:
                    record["candidates"].append(
                        _candidate_for_shape(
                            role="annotator",
                            job=entry,
                            track_index=track_index,
                            shape=job_shape,
                            task_meta=task_meta,
                        )
                    )
                    candidate_counts[str(job_id)] += 1
                else:
                    missing_counts[str(job_id)] += 1
                candidate_records.append(record)

    return {
        "version": 1,
        "kind": "cvat_candidate_manifest",
        "task_id": anchor_loaded["job"].get("task_id"),
        "anchor_job_id": anchor_job,
        "candidate_job_ids": candidate_jobs,
        "track_match": "track_index",
        "frame_mapping": frame_mapping_info(task_meta),
        "jobs": job_entries,
        "candidate_records": candidate_records,
        "summary": {
            "anchor_track_count": len(anchor_tracks),
            "candidate_record_count": len(candidate_records),
            "candidate_keyframe_counts": dict(
                sorted(candidate_counts.items(), key=lambda item: int(item[0]))
            ),
            "missing_keyframe_counts": dict(sorted(missing_counts.items(), key=lambda item: int(item[0]))),
        },
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--task-id", type=int, help="Infer anchor/candidate jobs from task annotation jobs")
    p.add_argument("--anchor-job", type=int, help="Defaults to first annotation job when --task-id is used")
    p.add_argument(
        "--candidate-jobs",
        help="e.g. 8-16 or 8,9,10; defaults to all non-anchor annotation jobs when --task-id is used",
    )
    p.add_argument(
        "--admin-job",
        type=int,
        help="If set, attach this admin/master-review job as admin_final",
    )
    p.add_argument("--output", type=Path, help="Defaults to tmp/task-<task_id>/candidate_manifest.json")
    args = p.parse_args()

    s = _session()
    task_jobs: list[dict[str, Any]] = []
    if args.task_id is not None:
        task_jobs = _task_annotation_jobs(s, args.task_id)
        eligible_jobs = _completed_annotation_jobs(task_jobs)
        if len(eligible_jobs) < 2:
            print(
                f"Task {args.task_id} needs at least 2 completed annotation-stage jobs for manifest; "
                f"found {len(eligible_jobs)}",
                file=sys.stderr,
            )
            sys.exit(1)
        anchor_job = args.anchor_job or int(eligible_jobs[0]["id"])
        if args.candidate_jobs:
            candidate_jobs = _parse_job_range(args.candidate_jobs)
        else:
            candidate_jobs = [int(job["id"]) for job in eligible_jobs if int(job["id"]) != anchor_job]
        output = args.output or Path(f"tmp/task-{args.task_id}/candidate_manifest.json")
    else:
        if args.anchor_job is None or args.candidate_jobs is None:
            p.error("provide either --task-id, or both --anchor-job and --candidate-jobs")
        anchor_job = args.anchor_job
        candidate_jobs = _parse_job_range(args.candidate_jobs)
        output = args.output or Path("tmp/cvat-task-output/candidate_manifest.json")

    if anchor_job in candidate_jobs:
        print(f"anchor job {anchor_job} must not be included in candidate jobs", file=sys.stderr)
        sys.exit(2)
    if not candidate_jobs:
        print("No candidate jobs selected", file=sys.stderr)
        sys.exit(2)

    _print_job_plan(anchor_job, candidate_jobs, task_jobs)
    manifest = build_manifest(anchor_job, candidate_jobs)
    if args.admin_job is not None:
        manifest = attach_admin_final(manifest, args.admin_job)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    summary = manifest["summary"]
    print(f"wrote {output}")
    print(f"anchor_track_count: {summary['anchor_track_count']}")
    print(f"candidate_record_count: {summary['candidate_record_count']}")
    print(f"candidate_keyframe_counts: {summary['candidate_keyframe_counts']}")
    print(f"missing_keyframe_counts: {summary['missing_keyframe_counts']}")
    if args.admin_job is not None and "admin_final_track_count" in summary:
        print(f"admin_final_track_count: {summary['admin_final_track_count']}")


if __name__ == "__main__":
    main()
