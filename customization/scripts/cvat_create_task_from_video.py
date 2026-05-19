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
  CVAT_API_URL  e.g. http://localhost:8080

  Authentication (one of):
    CVAT_ACCESS_TOKEN   Bearer token (UI: Personal access token)
    or CVAT_TOKEN / CVAT_API_TOKEN (same)
    or CVAT_USER + CVAT_PASSWORD

Examples:
  python customization/scripts/cvat_create_task_from_video.py probe --video /data/clips/a.mp4

  python customization/scripts/cvat_create_task_from_video.py create \\
    --video https://example.com/video.mp4 \\
    --step 3 --task-name my_run

  python customization/scripts/cvat_create_task_from_video.py create \\
    --manifest data/cvat_exports/<task_name>/manifest.json

  python customization/scripts/cvat_create_task_from_video.py wizard --video /path/a.mp4

  # Same as: wizard --video ...
  python customization/scripts/cvat_create_task_from_video.py --video /path/a.mp4

  # Same as: create --manifest ...
  python customization/scripts/cvat_create_task_from_video.py --manifest path/manifest.json
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
DEFAULT_NUM_STRIPE_JOBS = 6
DEFAULT_CLIP_NAME = "clip.mp4"
_LABEL_COLORS = ("#ff0000", "#00aa00", "#0066ff", "#ffaa00", "#aa00ff", "#00aaaa")

_EXPORT_MANIFEST_REQUIRED = (
    "task_name",
    "cvat_step",
    "cvat_start_frame_in_clip",
    "frame_decimation",
)


def _default_anchor_job_size(stripe_step: int) -> int:
    return 3 * stripe_step


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


def load_export_manifest(manifest_path: Path) -> dict[str, Any]:
    """Load a detectrack ``cvat_export`` sidecar and validate round-trip fields."""
    path = manifest_path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Manifest not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Manifest must be a JSON object: {path}")

    missing = [k for k in _EXPORT_MANIFEST_REQUIRED if k not in data]
    if missing:
        raise ValueError(f"Manifest {path} missing required keys: {', '.join(missing)}")

    cvat_step = int(data["cvat_step"])
    frame_decimation = int(data["frame_decimation"])
    if cvat_step < 1:
        raise ValueError(f"Manifest cvat_step must be >= 1, got {cvat_step}")
    if frame_decimation < 1:
        raise ValueError(f"Manifest frame_decimation must be >= 1, got {frame_decimation}")
    if cvat_step != frame_decimation:
        raise ValueError(
            f"Manifest cvat_step ({cvat_step}) must equal frame_decimation ({frame_decimation})"
        )

    start_frame_in_clip = int(data["cvat_start_frame_in_clip"])
    if start_frame_in_clip < 0:
        raise ValueError(
            f"Manifest cvat_start_frame_in_clip must be >= 0, got {start_frame_in_clip}"
        )

    clip_path = path.parent / DEFAULT_CLIP_NAME
    if not clip_path.is_file():
        raise FileNotFoundError(
            f"Expected clip next to manifest: {clip_path} (from {path})"
        )

    class_names = data.get("class_names")
    if class_names is not None and not isinstance(class_names, list):
        raise ValueError("Manifest class_names must be a list of strings")

    data = dict(data)
    data["_manifest_path"] = str(path)
    data["_clip_path"] = str(clip_path)
    return data


def _labels_json_from_class_names(class_names: list[str] | None) -> str | None:
    if not class_names:
        return None
    labels = [
        {
            "name": name,
            "color": _LABEL_COLORS[i % len(_LABEL_COLORS)],
            "attributes": [],
        }
        for i, name in enumerate(class_names)
    ]
    return json.dumps(labels)


def _stripe_step_from_manifest(manifest: dict[str, Any]) -> int | None:
    if "num_stripe_jobs" in manifest and manifest["num_stripe_jobs"] is not None:
        return int(manifest["num_stripe_jobs"])
    return None


def _anchor_job_size_from_manifest(manifest: dict[str, Any]) -> int | None:
    if "anchor_job_size" in manifest and manifest["anchor_job_size"] is not None:
        return int(manifest["anchor_job_size"])
    return None


def _apply_export_manifest(args: argparse.Namespace) -> None:
    manifest_path = getattr(args, "manifest", None)
    if not manifest_path:
        return

    manifest = load_export_manifest(Path(manifest_path))
    args._export_manifest = manifest

    if not getattr(args, "video", None):
        args.video = manifest["_clip_path"]
    if getattr(args, "step", None) is None:
        args.step = int(manifest["cvat_step"])
    if not getattr(args, "task_name", None):
        args.task_name = str(manifest["task_name"])
    if getattr(args, "start_frame", None) is None:
        args.start_frame = int(manifest["cvat_start_frame_in_clip"])
    if not getattr(args, "labels_json", None):
        args.labels_json = _labels_json_from_class_names(manifest.get("class_names"))

    if getattr(args, "stripe_step", None) is None:
        manifest_stripe = _stripe_step_from_manifest(manifest)
        if manifest_stripe is not None:
            args.stripe_step = manifest_stripe

    manifest_anchor = _anchor_job_size_from_manifest(manifest)
    if manifest_anchor is not None and getattr(args, "anchor_job_size", None) is None:
        args.anchor_job_size = manifest_anchor


def _require_create_fields(args: argparse.Namespace) -> None:
    if not getattr(args, "video", None):
        print("Provide --video or --manifest.", file=sys.stderr)
        sys.exit(2)
    if getattr(args, "step", None) is None:
        print("Provide --step or --manifest.", file=sys.stderr)
        sys.exit(2)
    if getattr(args, "start_frame", None) is None:
        args.start_frame = 0


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
    start_frame: int,
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
        print(
            "--- would POST /api/tasks/{id}/data with start_frame, stop_frame=0 "
            "(full clip), frame_filter ---"
        )
        print(
            json.dumps(
                {
                    "image_quality": DEFAULT_IMAGE_QUALITY,
                    "start_frame": start_frame,
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
                "start_frame": start_frame,
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
                "start_frame": str(start_frame),
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
    try:
        _apply_export_manifest(args)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)
    _require_create_fields(args)

    if getattr(args, "_export_manifest", None):
        print("--- export manifest ---")
        print(json.dumps(args._export_manifest, indent=2))

    if args.stripe_step is None:
        args.stripe_step = DEFAULT_NUM_STRIPE_JOBS

    if args.step < 1:
        print("--step must be >= 1", file=sys.stderr)
        sys.exit(2)
    if args.stripe_step < 1:
        print("--num-stripe-jobs must be >= 1", file=sys.stderr)
        sys.exit(2)
    anchor_job_size = (
        args.anchor_job_size
        if args.anchor_job_size is not None
        else _default_anchor_job_size(args.stripe_step)
    )
    if anchor_job_size < 1:
        print("--anchor-job-size must be >= 1", file=sys.stderr)
        sys.exit(2)

    print("--- ffprobe ---")
    print(json.dumps(probe_video_info(args.video), indent=2))

    if args.dry_run:
        s = cvat_session_from_env(require=False)
        _cvat_upload_and_jobs(
            s,
            video=args.video,
            step=args.step,
            start_frame=args.start_frame,
            task_name=args.task_name,
            labels_json=args.labels_json,
            project_id=args.project_id,
            anchor_job_size=anchor_job_size,
            stripe_step=args.stripe_step,
            dry_run=True,
        )
        return

    s = cvat_session_from_env()
    _cvat_upload_and_jobs(
        s,
        video=args.video,
        step=args.step,
        start_frame=args.start_frame,
        task_name=args.task_name,
        labels_json=args.labels_json,
        project_id=args.project_id,
        anchor_job_size=anchor_job_size,
        stripe_step=args.stripe_step,
        dry_run=False,
    )


def cmd_wizard(args: argparse.Namespace) -> None:
    try:
        _apply_export_manifest(args)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)

    video = args.video or _prompt_line("Video path or URL", "")
    if not video:
        print("Video path or URL is required.", file=sys.stderr)
        sys.exit(2)

    print("\n--- ffprobe ---")
    info = probe_video_info(video)
    print(json.dumps(info, indent=2))

    default_step = args.step if getattr(args, "step", None) is not None else 3
    step = _prompt_int("frame_filter step (>=1)", default_step)
    if step < 1:
        print("step must be >= 1", file=sys.stderr)
        sys.exit(2)

    if getattr(args, "task_name", None):
        default_name = args.task_name
    else:
        default_name = Path(video.split("?", 1)[0]).stem[:200]
    task_name = _prompt_line("task name", default_name)

    default_stripe = (
        args.stripe_step if getattr(args, "stripe_step", None) is not None else DEFAULT_NUM_STRIPE_JOBS
    )
    stripe_step = _prompt_int("num_stripe_jobs (for jobs 2+)", default_stripe)
    if stripe_step < 1:
        print("num_stripe_jobs must be >= 1", file=sys.stderr)
        sys.exit(2)
    default_anchor = (
        args.anchor_job_size
        if getattr(args, "anchor_job_size", None) is not None
        else _default_anchor_job_size(stripe_step)
    )
    anchor_job_size = _prompt_int(
        "anchor_job_size (number of task frames in job_anchor)",
        default_anchor,
    )
    if anchor_job_size < 1:
        print("anchor_job_size must be >= 1", file=sys.stderr)
        sys.exit(2)

    print("\n--- create summary ---")
    print(f"task name: {task_name!r}")
    print(f"step: {step}")
    print(f"anchor_job_size: {anchor_job_size}")
    print(f"num_stripe_jobs: {stripe_step}")
    start_frame = getattr(args, "start_frame", None)
    if start_frame is None:
        start_frame = 0
    print(f"start_frame: {start_frame}")

    labels_json = getattr(args, "labels_json", None)

    if args.dry_run:
        print("\n--- dry-run (no CVAT auth required) ---")
        s = cvat_session_from_env(require=False)
        _cvat_upload_and_jobs(
            s,
            video=video,
            step=step,
            start_frame=start_frame,
            task_name=task_name,
            labels_json=labels_json,
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
        start_frame=start_frame,
        task_name=task_name,
        labels_json=labels_json,
        project_id=None,
        anchor_job_size=anchor_job_size,
        stripe_step=stripe_step,
        dry_run=False,
    )


def main() -> None:
    subcmds = {"probe", "create", "wizard", "-h", "--help"}
    # Shortcut:  python .../cvat_create_task_from_video.py --video /path/to.mp4
    #  -> same as  wizard --video /path/to.mp4
    if len(sys.argv) >= 3 and sys.argv[1] == "--video":
        if sys.argv[2] not in subcmds and not sys.argv[2].startswith("-"):
            tail = sys.argv[3:]
            sys.argv = [sys.argv[0], "wizard", "--video", sys.argv[2], *tail]
    # Shortcut:  python .../cvat_create_task_from_video.py --manifest path/manifest.json
    #  -> same as  create --manifest ...
    if len(sys.argv) >= 3 and sys.argv[1] == "--manifest":
        if sys.argv[2] not in subcmds and not sys.argv[2].startswith("-"):
            tail = sys.argv[3:]
            sys.argv = [sys.argv[0], "create", "--manifest", sys.argv[2], *tail]

    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("probe", help="Print FPS / duration / frame estimate via ffprobe")
    pp.add_argument("--video", required=True, help="Local path or http(s) URL")
    pp.set_defaults(func=cmd_probe)

    cr = sub.add_parser("create", help="Create task + upload video + interleaved jobs")
    cr.add_argument("--video", default=None, help="Local file path or http(s) URL")
    cr.add_argument(
        "--manifest",
        default=None,
        help="detectrack export manifest.json; uses sibling clip.mp4 and sidecar fields",
    )
    cr.add_argument(
        "--step",
        type=int,
        default=None,
        help="frame_filter step=N (CVAT subsampling); default from --manifest",
    )
    cr.add_argument(
        "--start-frame",
        type=int,
        default=None,
        help="CVAT data start_frame within clip; default from manifest or 0",
    )
    cr.add_argument("--task-name", default=None, help="Defaults to video basename")
    cr.add_argument("--labels-json", default=None, help='JSON list of labels, default one "human" box')
    cr.add_argument("--project-id", type=int, default=None)
    cr.add_argument(
        "--anchor-job-size",
        type=int,
        default=None,
        metavar="N",
        help="job_anchor includes N task-relative frames (0..N-1; default 3 * --num-stripe-jobs)",
    )
    cr.add_argument(
        "--num-stripe-jobs",
        "--num_stripe_jobs",
        type=int,
        default=None,
        dest="stripe_step",
        metavar="K",
        help=(
            f"Number of stripe jobs to create, one per offset 0..K-1 "
            f"(default {DEFAULT_NUM_STRIPE_JOBS}, or manifest num_stripe_jobs)"
        ),
    )
    cr.add_argument("--stripe-step", type=int, dest="stripe_step", help=argparse.SUPPRESS)
    cr.add_argument("--dry-run", action="store_true")
    cr.set_defaults(func=cmd_create)

    wz = sub.add_parser("wizard", help="Interactive: probe then prompt for step, task name, jobs")
    wz.add_argument("--video", default=None, help="Optional; if omitted you will be prompted")
    wz.add_argument(
        "--manifest",
        default=None,
        help="detectrack export manifest.json; pre-fills wizard defaults from sidecar",
    )
    wz.add_argument("--dry-run", action="store_true", help="Only print what would be POSTed")
    wz.set_defaults(func=cmd_wizard)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
