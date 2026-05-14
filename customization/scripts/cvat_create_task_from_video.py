#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
Create a CVAT task from a video, set frame_filter step, import the full video
(CVAT ``stop_frame=0`` uses the media length), then create interleaved manual-frame
jobs (same layout as cvat_interleaved_jobs.py).

Subcommands:
  probe   — ffprobe: FPS, duration, estimated frame count (needs ffmpeg/ffprobe).
           No CVAT credentials required.
  wizard  — ffprobe summary, then prompts for step / task-name / job options;
           optional --video else asks interactively.
  create  — POST task, POST /tasks/{id}/data (local file or http(s) URL), wait for
            import, then rebuild Job1 + stripe jobs.

Environment:
  CVAT_HOST     e.g. http://localhost:8080

  Authentication (one of):
    CVAT_ACCESS_TOKEN   Bearer token (UI: Personal access token)
    or CVAT_TOKEN / CVAT_API_TOKEN (same)
    or CVAT_USER + CVAT_PASSWORD

Examples:
  python customization/scripts/cvat_create_task_from_video.py probe --video /data/clips/a.mp4

  python customization/scripts/cvat_create_task_from_video.py create \\
    --video https://example.com/video.mp4 \\
    --step 3 --task-name my_run

  python customization/scripts/cvat_create_task_from_video.py wizard --video /path/a.mp4

  # Same as: wizard --video ...
  python customization/scripts/cvat_create_task_from_video.py --video /path/a.mp4
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

from cvat_api_env import cvat_session_from_env
from cvat_interleaved_jobs import rebuild_interleaved_jobs

DEFAULT_IMAGE_QUALITY = 85
DEFAULT_SEGMENT_SIZE = 0  # CVAT expands 0 to the full task data size.


def _api(s: requests.Session, method: str, path: str, **kwargs: Any) -> requests.Response:
    url = f"{s.base_url}{path}"  # type: ignore[attr-defined]
    return s.request(method, url, **kwargs)


def _rate_to_float(spec: str) -> float:
    spec = (spec or "").strip()
    if not spec or spec == "0/0":
        return 0.0
    if "/" in spec:
        a, b = spec.split("/", 1)
        return float(a) / float(b)
    return float(spec)


def ffprobe_streams(video: str) -> dict[str, Any]:
    if not shutil.which("ffprobe"):
        print("ffprobe not found. Install ffmpeg (e.g. apt install ffmpeg).", file=sys.stderr)
        sys.exit(2)
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=avg_frame_rate,r_frame_rate,nb_frames,duration",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        video,
    ]
    raw = subprocess.check_output(cmd, text=True)
    return json.loads(raw)


def probe_video_info(video: str) -> dict[str, Any]:
    data = ffprobe_streams(video)
    streams = data.get("streams") or []
    st = streams[0] if streams else {}
    fmt = data.get("format") or {}
    avg = _rate_to_float(str(st.get("avg_frame_rate") or ""))
    rfr = _rate_to_float(str(st.get("r_frame_rate") or ""))
    fps = avg or rfr
    dur_stream = float(st["duration"]) if st.get("duration") else 0.0
    dur_fmt = float(fmt["duration"]) if fmt.get("duration") else 0.0
    duration_sec = dur_stream or dur_fmt
    nb = st.get("nb_frames")
    nb_frames = 0
    if nb not in (None, "", "N/A"):
        try:
            nb_frames = int(nb)
        except (TypeError, ValueError):
            nb_frames = 0
    if nb_frames <= 0 and fps > 0 and duration_sec > 0:
        nb_frames = int(round(duration_sec * fps))
    return {
        "video": video,
        "fps_avg_frame_rate": st.get("avg_frame_rate"),
        "fps_r_frame_rate": st.get("r_frame_rate"),
        "fps_float": round(fps, 6) if fps else None,
        "duration_seconds": round(duration_sec, 6) if duration_sec else None,
        "nb_frames_reported": int(st["nb_frames"]) if st.get("nb_frames") not in (None, "N/A") else None,
        "nb_frames_estimated": nb_frames or None,
    }


def _wait_rq(s: requests.Session, rq_id: str, *, timeout_s: int = 3600) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rr = _api(s, "GET", f"/api/requests/{rq_id}")
        rr.raise_for_status()
        body = rr.json()
        status = body.get("status")
        if status in ("finished", "failed"):
            return body
        time.sleep(1.0)
    raise TimeoutError(f"rq_id={rq_id} did not finish within {timeout_s}s")


def _is_remote(video: str) -> bool:
    v = video.strip().lower()
    return v.startswith("http://") or v.startswith("https://")


def cmd_probe(args: argparse.Namespace) -> None:
    info = probe_video_info(args.video)
    print(json.dumps(info, indent=2))


def _prompt_line(label: str, default: str) -> str:
    try:
        line = input(f"{label} [{default}]: ").strip()
    except EOFError:
        return default
    return line if line else default


def _prompt_int(label: str, default: int) -> int:
    return int(_prompt_line(label, str(default)))


def _cvat_upload_and_jobs(
    s: requests.Session,
    *,
    video: str,
    step: int,
    task_name: str | None,
    labels_json: str | None,
    project_id: int | None,
    anchor_job_size: int,
    stripe_step: int,
    dry_run: bool,
) -> None:
    labels = json.loads(labels_json) if labels_json else [
        {"name": "human", "color": "#ff0000", "attributes": []},
    ]
    name = task_name or Path(video.split("?", 1)[0]).stem[:200]

    task_body: dict[str, Any] = {
        "name": name,
        "labels": labels,
        "overlap": 0,
        "segment_size": DEFAULT_SEGMENT_SIZE,
        "consensus_replicas": 0,
    }
    if project_id is not None:
        task_body["project_id"] = project_id

    if dry_run:
        print("--- dry-run: would POST /api/tasks ---")
        print(json.dumps(task_body, indent=2))
        print("--- would POST /api/tasks/{id}/data with start_frame=0, stop_frame=0 (full video), frame_filter ---")
        print(
            json.dumps(
                {
                    "image_quality": DEFAULT_IMAGE_QUALITY,
                    "stop_frame": 0,
                    "frame_filter": f"step={step}",
                },
                indent=2,
            )
        )
        return

    tr = _api(s, "POST", "/api/tasks", json=task_body)
    if tr.status_code != 201:
        print(tr.text, file=sys.stderr)
    tr.raise_for_status()
    task = tr.json()
    tid = int(task["id"])
    print(f"created task id={tid} name={name!r}")

    frame_filter = f"step={step}"
    if _is_remote(video):
        dr = _api(
            s,
            "POST",
            f"/api/tasks/{tid}/data",
            json={
                "remote_files": [video],
                "image_quality": DEFAULT_IMAGE_QUALITY,
                "start_frame": 0,
                "stop_frame": 0,
                "frame_filter": frame_filter,
            },
        )
    else:
        path = Path(video)
        if not path.is_file():
            print(f"Not a file: {path}", file=sys.stderr)
            sys.exit(1)
        with open(path, "rb") as handle:
            files = {"client_files[0]": (path.name, handle, "application/octet-stream")}
            form: dict[str, Any] = {
                "image_quality": str(DEFAULT_IMAGE_QUALITY),
                "start_frame": "0",
                "stop_frame": "0",
                "frame_filter": frame_filter,
            }
            dr = _api(s, "POST", f"/api/tasks/{tid}/data", data=form, files=files)

    if dr.status_code not in (200, 202):
        print(dr.text, file=sys.stderr)
    dr.raise_for_status()
    dj = dr.json()
    rq_id = dj.get("rq_id")
    if not rq_id:
        print("No rq_id in data upload response", dj, file=sys.stderr)
        sys.exit(1)
    print(f"data import started rq_id={rq_id}")
    rq = _wait_rq(s, rq_id)
    if rq.get("status") != "finished":
        print("Import failed:", json.dumps(rq, indent=2), file=sys.stderr)
        sys.exit(1)

    gt = _api(s, "GET", f"/api/tasks/{tid}")
    gt.raise_for_status()
    t2 = gt.json()
    if t2.get("consensus_replicas", 0):
        print("Task has consensus_replicas != 0; interleaved job script will refuse.", file=sys.stderr)
        sys.exit(1)
    n = int(t2["size"])
    print(f"task size (annotation frames)={n}")

    anchor_size = min(anchor_job_size, n)
    if anchor_size < anchor_job_size:
        print(
            f"note: clamped anchor_job_size from {anchor_job_size} to {anchor_size} "
            f"(task size {n})"
        )

    rebuild_interleaved_jobs(
        s,
        tid,
        anchor_job_size=anchor_size,
        stripe_step=stripe_step,
        dry_run=False,
    )
    print(json.dumps({"task_id": tid, "task_size": n}, indent=2))


def cmd_create(args: argparse.Namespace) -> None:
    if args.step < 1:
        print("--step must be >= 1", file=sys.stderr)
        sys.exit(2)
    if args.anchor_job_size < 1:
        print("--anchor-job-size must be >= 1", file=sys.stderr)
        sys.exit(2)
    if args.stripe_step < 1:
        print("--stripe-step must be >= 1", file=sys.stderr)
        sys.exit(2)

    print("--- ffprobe ---")
    print(json.dumps(probe_video_info(args.video), indent=2))

    if args.dry_run:
        s = cvat_session_from_env(require=False)
        _cvat_upload_and_jobs(
            s,
            video=args.video,
            step=args.step,
            task_name=args.task_name,
            labels_json=args.labels_json,
            project_id=args.project_id,
            anchor_job_size=args.anchor_job_size,
            stripe_step=args.stripe_step,
            dry_run=True,
        )
        return

    s = cvat_session_from_env()
    _cvat_upload_and_jobs(
        s,
        video=args.video,
        step=args.step,
        task_name=args.task_name,
        labels_json=args.labels_json,
        project_id=args.project_id,
        anchor_job_size=args.anchor_job_size,
        stripe_step=args.stripe_step,
        dry_run=False,
    )


def cmd_wizard(args: argparse.Namespace) -> None:
    video = args.video or _prompt_line("Video path or URL", "")
    if not video:
        print("Video path or URL is required.", file=sys.stderr)
        sys.exit(2)

    print("\n--- ffprobe ---")
    info = probe_video_info(video)
    print(json.dumps(info, indent=2))

    default_step = 3
    step = _prompt_int("frame_filter step (>=1)", default_step)
    if step < 1:
        print("step must be >= 1", file=sys.stderr)
        sys.exit(2)

    default_name = Path(video.split("?", 1)[0]).stem[:200]
    task_name = _prompt_line("task name", default_name)

    anchor_job_size = _prompt_int("anchor_job_size (number of task frames in job_anchor)", 50)
    if anchor_job_size < 1:
        print("anchor_job_size must be >= 1", file=sys.stderr)
        sys.exit(2)
    stripe_step = _prompt_int("stripe_step (for jobs 2+)", 10)
    if stripe_step < 1:
        print("stripe_step must be >= 1", file=sys.stderr)
        sys.exit(2)

    print("\n--- create summary ---")
    print(f"task name: {task_name!r}")
    print(f"step: {step}")
    print(f"anchor_job_size: {anchor_job_size}")
    print(f"stripe_step: {stripe_step}")

    if args.dry_run:
        print("\n--- dry-run (no CVAT auth required) ---")
        s = cvat_session_from_env(require=False)
        _cvat_upload_and_jobs(
            s,
            video=video,
            step=step,
            task_name=task_name,
            labels_json=None,
            project_id=None,
            anchor_job_size=anchor_job_size,
            stripe_step=stripe_step,
            dry_run=True,
        )
        return

    s = cvat_session_from_env()
    _cvat_upload_and_jobs(
        s,
        video=video,
        step=step,
        task_name=task_name,
        labels_json=None,
        project_id=None,
        anchor_job_size=anchor_job_size,
        stripe_step=stripe_step,
        dry_run=False,
    )


def main() -> None:
    # Shortcut:  python .../cvat_create_task_from_video.py --video /path/to.mp4
    #  -> same as  wizard --video /path/to.mp4
    if len(sys.argv) >= 3 and sys.argv[1] == "--video":
        subcmds = {"probe", "create", "wizard", "-h", "--help"}
        if sys.argv[2] not in subcmds and not sys.argv[2].startswith("-"):
            tail = sys.argv[3:]
            sys.argv = [sys.argv[0], "wizard", "--video", sys.argv[2], *tail]

    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("probe", help="Print FPS / duration / frame estimate via ffprobe")
    pp.add_argument("--video", required=True, help="Local path or http(s) URL")
    pp.set_defaults(func=cmd_probe)

    cr = sub.add_parser("create", help="Create task + upload video + interleaved jobs")
    cr.add_argument("--video", required=True, help="Local file path or http(s) URL")
    cr.add_argument("--step", type=int, required=True, help="frame_filter step=N (CVAT subsampling)")
    cr.add_argument("--task-name", default=None, help="Defaults to video basename")
    cr.add_argument("--labels-json", default=None, help='JSON list of labels, default one "human" box')
    cr.add_argument("--project-id", type=int, default=None)
    cr.add_argument(
        "--anchor-job-size",
        type=int,
        default=50,
        metavar="N",
        help="job_anchor includes N task-relative frames (0..N-1; default 50)",
    )
    cr.add_argument(
        "--stripe-step",
        type=int,
        default=10,
        metavar="K",
        help="Stripe spacing; creates K stripe jobs for offsets 0..K-1 (default 10)",
    )
    cr.add_argument("--dry-run", action="store_true")
    cr.set_defaults(func=cmd_create)

    wz = sub.add_parser("wizard", help="Interactive: probe then prompt for step, task name, jobs")
    wz.add_argument("--video", default=None, help="Optional; if omitted you will be prompted")
    wz.add_argument("--dry-run", action="store_true", help="Only print what would be POSTed")
    wz.set_defaults(func=cmd_wizard)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
