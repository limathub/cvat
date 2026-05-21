# SPDX-License-Identifier: MIT
"""Per-task labeling workspaces under tmp/labeling/ and registry lookup."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LABELING_ROOT = Path("tmp/labeling")
REGISTRY_PATH = LABELING_ROOT / "registry.json"
DEFAULT_CLIP_NAME = "clip.mp4"
INBOUND_ZIP_NAME = "inbound.zip"
OUTBOUND_ZIP_TEMPLATE = "outbound_{task_name}.zip"

_EXPORT_MANIFEST_REQUIRED = (
    "task_name",
    "cvat_step",
    "cvat_start_frame_in_clip",
    "frame_decimation",
)

# CVAT REST API format names (see /api/server/annotation/formats).
DEFAULT_VIDEO_EXPORT_FORMAT = "CVAT for video 1.1"

_FORMAT_ALIASES: dict[str, str] = {
    "cvat_for_video_1.1": DEFAULT_VIDEO_EXPORT_FORMAT,
    "cvat_for_video": DEFAULT_VIDEO_EXPORT_FORMAT,
    "cvat for video 1.1": DEFAULT_VIDEO_EXPORT_FORMAT,
    "mot_1.1": "MOT 1.1",
    "mot 1.1": "MOT 1.1",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def workspace_for_task_name(task_name: str) -> Path:
    safe = task_name.strip()
    if not safe or "/" in safe or "\\" in safe or safe in (".", ".."):
        raise ValueError(f"Invalid task_name for workspace: {task_name!r}")
    return LABELING_ROOT / safe


def export_dir(workspace: Path) -> Path:
    return workspace / "export"


def manifest_path(workspace: Path) -> Path:
    return workspace / "manifest.json"


def load_registry() -> dict[str, Any]:
    if not REGISTRY_PATH.is_file():
        return {"tasks": {}}
    data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Registry must be a JSON object: {REGISTRY_PATH}")
    data.setdefault("tasks", {})
    return data


def save_registry(registry: dict[str, Any]) -> None:
    LABELING_ROOT.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(
        json.dumps(registry, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def register_task(task_name: str, cvat_task_id: int, workspace: Path) -> None:
    registry = load_registry()
    tasks: dict[str, Any] = registry["tasks"]
    rel_workspace = str(workspace)
    if task_name in tasks:
        existing = tasks[task_name]
        if int(existing.get("cvat_task_id", -1)) != cvat_task_id:
            raise RuntimeError(
                f"Registry already has task_name={task_name!r} "
                f"with cvat_task_id={existing.get('cvat_task_id')}"
            )
    for name, entry in tasks.items():
        if name != task_name and int(entry.get("cvat_task_id", -1)) == cvat_task_id:
            raise RuntimeError(
                f"cvat_task_id={cvat_task_id} already registered as task_name={name!r}"
            )
    tasks[task_name] = {
        "cvat_task_id": cvat_task_id,
        "workspace": rel_workspace,
    }
    save_registry(registry)


def resolve_workspace_by_task_id(task_id: int) -> Path | None:
    registry = load_registry()
    for entry in registry.get("tasks", {}).values():
        if int(entry.get("cvat_task_id", -1)) == task_id:
            return Path(str(entry["workspace"]))
    return None


def resolve_workspace_by_task_name(task_name: str) -> Path:
    registry = load_registry()
    entry = registry.get("tasks", {}).get(task_name)
    if not entry:
        raise KeyError(f"task_name {task_name!r} not found in {REGISTRY_PATH}")
    return Path(str(entry["workspace"]))


def assert_workspace_available(task_name: str) -> Path:
    workspace = workspace_for_task_name(task_name)
    if workspace.exists():
        raise FileExistsError(
            f"Labeling workspace already exists: {workspace} (refuse to overwrite)"
        )
    registry = load_registry()
    if task_name in registry.get("tasks", {}):
        raise RuntimeError(
            f"task_name {task_name!r} already registered in {REGISTRY_PATH}"
        )
    return workspace


def normalize_export_format(name: str) -> str:
    key = name.strip().lower().replace("-", "_")
    if key in _FORMAT_ALIASES:
        return _FORMAT_ALIASES[key]
    # Allow exact CVAT display names passed through.
    for alias, canonical in _FORMAT_ALIASES.items():
        if name.strip().lower() == alias:
            return canonical
    return name.strip()


def export_formats_from_manifest(manifest: dict[str, Any]) -> list[str]:
    raw: list[str] = []
    if isinstance(manifest.get("export_formats"), list):
        raw = [str(x) for x in manifest["export_formats"]]
    elif manifest.get("export_format") is not None:
        raw = [str(manifest["export_format"])]
    elif manifest.get("annotation_format") is not None:
        raw = [str(manifest["annotation_format"])]

    if not raw:
        raw = [DEFAULT_VIDEO_EXPORT_FORMAT]

    formats = []
    seen: set[str] = set()
    for item in raw:
        fmt = normalize_export_format(item)
        if fmt not in seen:
            seen.add(fmt)
            formats.append(fmt)
    return formats


def export_save_images_from_manifest(manifest: dict[str, Any]) -> bool:
    return bool(manifest.get("export_save_images", False))


def export_zip_basename(format_name: str) -> str:
    lower = format_name.lower()
    if "mot" in lower and "png" not in lower:
        return "annotations_mot.zip"
    if "cvat" in lower and "video" in lower:
        return "annotations_cvat.zip"
    if "cvat" in lower and "image" in lower:
        return "annotations_cvat_images.zip"
    slug = re.sub(r"[^a-z0-9]+", "_", lower).strip("_")
    return f"annotations_{slug}.zip"


def load_handoff_manifest(path: Path) -> dict[str, Any]:
    """Load and validate detectrack handoff manifest (clip must exist in workspace)."""
    path = path.expanduser().resolve()
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

    out = dict(data)
    out["_manifest_path"] = str(path)
    out["_clip_path"] = str(clip_path)
    return out


def save_manifest(workspace: Path, manifest: dict[str, Any]) -> None:
    clean = {k: v for k, v in manifest.items() if not str(k).startswith("_")}
    manifest_path(workspace).write_text(
        json.dumps(clean, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def append_labeling_server(
    manifest: dict[str, Any],
    *,
    cvat_task_id: int | None = None,
    workspace: Path | None = None,
    review_job_id: int | None = None,
    exported_at: str | None = None,
    export_files: list[str] | None = None,
    outbound_zip: str | None = None,
) -> dict[str, Any]:
    server = dict(manifest.get("labeling_server") or {})
    if cvat_task_id is not None:
        server["cvat_task_id"] = cvat_task_id
    if workspace is not None:
        server["workspace"] = str(workspace)
    if "imported_at" not in server:
        server.setdefault("imported_at", utc_now_iso())
    if review_job_id is not None:
        server["review_job_id"] = review_job_id
    if exported_at is not None:
        server["exported_at"] = exported_at
    if export_files is not None:
        server["export_files"] = export_files
    if outbound_zip is not None:
        server["outbound_zip"] = outbound_zip
    manifest = dict(manifest)
    manifest["labeling_server"] = server
    return manifest
