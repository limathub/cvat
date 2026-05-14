# SPDX-License-Identifier: MIT
"""Map CVAT task-relative frame indices to source media frame numbers."""

from __future__ import annotations

import re
from typing import Any

import requests


def frame_step(meta: dict[str, Any]) -> int:
    match = re.search(r"(?:^|;)step=(\d+)(?:;|$)", meta.get("frame_filter") or "")
    return int(match.group(1)) if match else 1


def load_task_data_meta(s: requests.Session, task_id: int) -> dict[str, Any]:
    r = s.get(f"{s.base_url}/api/tasks/{task_id}/data/meta")  # type: ignore[attr-defined]
    r.raise_for_status()
    return r.json()


def frame_mapping_info(meta: dict[str, Any]) -> dict[str, Any]:
    size = int(meta.get("size") or 0)
    step = frame_step(meta)
    start = int(meta.get("start_frame", 0))
    inc = meta.get("included_frames")
    if isinstance(inc, list) and len(inc) == size and size > 0:
        return {"rule": "included_frames_index", "start_frame": start, "frame_step": step, "size": size}
    return {"rule": "uniform_step", "start_frame": start, "frame_step": step, "size": size}


def included_task_frames(meta: dict[str, Any]) -> list[int]:
    """Return job-owned frames in task-relative annotation coordinates."""
    step = frame_step(meta)
    start = int(meta.get("start_frame", 0))
    frames = []
    for frame in meta.get("included_frames", []) or []:
        original_frame = int(frame)
        offset = original_frame - start
        if offset % step:
            raise ValueError(f"Included frame {original_frame} does not match frame_filter step={step}")
        frames.append(offset // step)
    return frames


def task_frame_to_source_media_frame(meta: dict[str, Any], task_frame: int) -> int:
    size = int(meta.get("size") or 0)
    if task_frame < 0 or (size > 0 and task_frame >= size):
        raise ValueError(f"task_frame {task_frame} out of range for task size {size}")
    inc = meta.get("included_frames")
    if isinstance(inc, list) and len(inc) == size and size > 0:
        return int(inc[task_frame])
    start = int(meta.get("start_frame", 0))
    step = frame_step(meta)
    return start + int(task_frame) * step
