#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
Import detectrack labeling packages and export labeled results.

Inbound (zip): manifest.json + clip.mp4 -> tmp/labeling/<task_name>/ + CVAT task.
Outbound: merge stripe jobs, export formats from manifest, zip outbound_<task_name>.zip.

Environment: same as other customization scripts (CVAT_API_URL, auth).

Examples:
  python3 customization/scripts/package_labeling_handoff.py import \\
    --package /path/to/my_task.zip

  python3 customization/scripts/package_labeling_handoff.py export --task-id 9
  python3 customization/scripts/package_labeling_handoff.py export --task-name my_task
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

from cvat_api_env import cvat_session_from_env
from cvat_create_task_from_video import (
    DEFAULT_NUM_STRIPE_JOBS,
    _anchor_job_size_from_manifest,
    _cvat_upload_and_jobs,
    _default_anchor_job_size,
    _labels_json_from_class_names,
    _stripe_step_from_manifest,
)
from cvat_export import export_job_dataset
from labeling_workspace import (
    DEFAULT_CLIP_NAME,
    INBOUND_ZIP_NAME,
    OUTBOUND_ZIP_TEMPLATE,
    append_labeling_server,
    assert_workspace_available,
    export_dir,
    export_formats_from_manifest,
    export_save_images_from_manifest,
    export_zip_basename,
    load_handoff_manifest,
    manifest_path,
    register_task,
    resolve_workspace_by_task_id,
    resolve_workspace_by_task_name,
    save_manifest,
    utc_now_iso,
)

_REQUIRED_PACKAGE_MEMBERS = frozenset({DEFAULT_CLIP_NAME, "manifest.json"})


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _merge_script() -> Path:
    return Path(__file__).resolve().parent / "merge_jobs_to_master_review.py"


def _zip_strip_prefix(names: list[str]) -> str:
    """If archive has a single top-level directory, return that prefix (with trailing /)."""
    files = [n for n in names if n and not n.endswith("/")]
    if not files:
        return ""
    tops = {n.split("/", 1)[0] for n in files if "/" in n}
    if len(tops) != 1:
        return ""
    prefix = f"{tops.pop()}/"
    if all(n.startswith(prefix) for n in files):
        return prefix
    return ""


def _validate_package_members(member_names: list[str]) -> None:
    prefix = _zip_strip_prefix(member_names)
    normalized = set()
    for name in member_names:
        if name.endswith("/"):
            continue
        rel = name[len(prefix) :] if prefix and name.startswith(prefix) else name
        if ".." in Path(rel).parts:
            raise ValueError(f"Unsafe zip path: {name}")
        normalized.add(Path(rel).name)
    missing = _REQUIRED_PACKAGE_MEMBERS - normalized
    if missing:
        raise ValueError(
            f"Package must contain {sorted(_REQUIRED_PACKAGE_MEMBERS)}; missing {sorted(missing)}"
        )


def _extract_package(package: Path, workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(package) as zf:
        names = zf.namelist()
        _validate_package_members(names)
        prefix = _zip_strip_prefix(names)
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = info.filename[len(prefix) :] if prefix and info.filename.startswith(prefix) else info.filename
            if ".." in Path(rel).parts:
                raise ValueError(f"Unsafe zip path: {info.filename}")
            target = workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _create_from_workspace(s: requests.Session, workspace: Path) -> dict[str, Any]:
    manifest = load_handoff_manifest(manifest_path(workspace))
    task_name = str(manifest["task_name"])
    step = int(manifest["cvat_step"])
    start_frame = int(manifest["cvat_start_frame_in_clip"])
    labels_json = _labels_json_from_class_names(manifest.get("class_names"))
    stripe_step = _stripe_step_from_manifest(manifest) or DEFAULT_NUM_STRIPE_JOBS
    anchor_job_size = _anchor_job_size_from_manifest(manifest)
    if anchor_job_size is None:
        anchor_job_size = _default_anchor_job_size(stripe_step)

    result = _cvat_upload_and_jobs(
        s,
        video=manifest["_clip_path"],
        step=step,
        start_frame=start_frame,
        task_name=task_name,
        labels_json=labels_json,
        project_id=None,
        anchor_job_size=anchor_job_size,
        stripe_step=stripe_step,
        dry_run=False,
    )
    if result is None:
        raise RuntimeError("create failed (unexpected dry-run)")
    return {**result, "task_name": task_name}


def cmd_import(args: argparse.Namespace) -> None:
    package = Path(args.package).expanduser().resolve()
    if not package.is_file():
        print(f"Package not found: {package}", file=sys.stderr)
        sys.exit(1)

    with zipfile.ZipFile(package) as zf:
        _validate_package_members(zf.namelist())
        manifest_data = None
        prefix = _zip_strip_prefix(zf.namelist())
        for name in zf.namelist():
            rel = name[len(prefix) :] if prefix and name.startswith(prefix) else name
            if Path(rel).name == "manifest.json":
                manifest_data = json.loads(zf.read(name))
                break
    if not isinstance(manifest_data, dict) or "task_name" not in manifest_data:
        raise ValueError("Package manifest.json must be a JSON object with task_name")

    task_name = str(manifest_data["task_name"])
    workspace = assert_workspace_available(task_name)

    try:
        _extract_package(package, workspace)
        shutil.copy2(package, workspace / INBOUND_ZIP_NAME)
        manifest = load_handoff_manifest(manifest_path(workspace))
        s = cvat_session_from_env()
        result = _create_from_workspace(s, workspace)
        cvat_task_id = int(result["task_id"])
        manifest = append_labeling_server(
            manifest,
            cvat_task_id=cvat_task_id,
            workspace=workspace,
        )
        save_manifest(workspace, manifest)
        register_task(task_name, cvat_task_id, workspace)
    except Exception:
        if workspace.exists():
            shutil.rmtree(workspace)
        raise

    print(json.dumps({"task_name": task_name, "workspace": str(workspace), **result}, indent=2))


def _run_merge(task_id: int, output_dir: Path) -> None:
    cmd = [
        sys.executable,
        str(_merge_script()),
        "--task-id",
        str(task_id),
        "--output-dir",
        str(output_dir),
    ]
    print("running:", " ".join(cmd))
    subprocess.run(cmd, cwd=_repo_root(), check=True)


def _review_job_from_provenance(output_dir: Path) -> int:
    path = output_dir / "provenance_manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"Merge provenance not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    job_id = data.get("apply_job_id")
    if job_id is None:
        raise RuntimeError(
            f"provenance_manifest.json has no apply_job_id; re-run merge or pass --review-job"
        )
    return int(job_id)


def _build_outbound_zip(workspace: Path, task_name: str) -> Path:
    outbound_name = OUTBOUND_ZIP_TEMPLATE.format(task_name=task_name)
    outbound_path = workspace / outbound_name
    manifest_file = manifest_path(workspace)
    exp = export_dir(workspace)
    with zipfile.ZipFile(outbound_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(manifest_file, arcname="manifest.json")
        if exp.is_dir():
            for child in sorted(exp.rglob("*")):
                if child.is_file():
                    rel = child.relative_to(exp)
                    zf.write(child, arcname=f"export/{rel.as_posix()}")
    return outbound_path


def cmd_export(args: argparse.Namespace) -> None:
    if args.task_id is not None:
        workspace = resolve_workspace_by_task_id(args.task_id)
        if workspace is None:
            print(
                f"No labeling workspace registered for cvat task_id={args.task_id}",
                file=sys.stderr,
            )
            sys.exit(1)
        cvat_task_id = args.task_id
        manifest = load_handoff_manifest(manifest_path(workspace))
        task_name = str(manifest["task_name"])
    else:
        task_name = str(args.task_name)
        workspace = resolve_workspace_by_task_name(task_name)
        manifest = load_handoff_manifest(manifest_path(workspace))
        server = manifest.get("labeling_server") or {}
        cvat_task_id = int(server.get("cvat_task_id", 0))
        if not cvat_task_id:
            from labeling_workspace import load_registry  # noqa: PLC0415

            registry_entry = load_registry()["tasks"].get(task_name)
            if registry_entry:
                cvat_task_id = int(registry_entry["cvat_task_id"])
        if not cvat_task_id:
            print("manifest has no labeling_server.cvat_task_id", file=sys.stderr)
            sys.exit(1)

    out_dir = export_dir(workspace)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_merge:
        _run_merge(cvat_task_id, out_dir)

    review_job_id = args.review_job or _review_job_from_provenance(out_dir)

    formats = export_formats_from_manifest(manifest)
    save_images = export_save_images_from_manifest(manifest)
    s = cvat_session_from_env()

    written: list[str] = []
    export_files: list[str] = []
    for fmt in formats:
        basename = export_zip_basename(fmt)
        dest = out_dir / basename
        print(f"exporting job {review_job_id} format={fmt!r} -> {dest}")
        blob = export_job_dataset(
            s,
            review_job_id,
            fmt,
            save_images=save_images,
            filename=basename,
        )
        dest.write_bytes(blob)
        written.append(str(dest))
        export_files.append(f"export/{basename}")

    for name in ("merged_annotations.json", "provenance_manifest.json"):
        p = out_dir / name
        if p.is_file():
            export_files.append(f"export/{name}")

    manifest = append_labeling_server(
        manifest,
        cvat_task_id=cvat_task_id,
        workspace=workspace,
        review_job_id=review_job_id,
        exported_at=utc_now_iso(),
        export_files=sorted(set(export_files)),
    )
    save_manifest(workspace, manifest)

    outbound = _build_outbound_zip(workspace, task_name)
    manifest = append_labeling_server(
        manifest,
        outbound_zip=str(outbound.relative_to(workspace)),
    )
    save_manifest(workspace, manifest)

    print(json.dumps({
        "task_name": task_name,
        "cvat_task_id": cvat_task_id,
        "review_job_id": review_job_id,
        "workspace": str(workspace),
        "outbound_zip": str(outbound),
        "export_formats": formats,
        "files": written,
    }, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    imp = sub.add_parser("import", help="Unpack package zip and create CVAT task")
    imp.add_argument(
        "--package",
        required=True,
        type=Path,
        help="Zip containing manifest.json and clip.mp4",
    )
    imp.set_defaults(func=cmd_import)

    exp = sub.add_parser("export", help="Merge, export formats, build outbound zip")
    exp.add_argument("--task-id", type=int, default=None, help="CVAT task id")
    exp.add_argument("--task-name", default=None, help="Handoff task_name from manifest")
    exp.add_argument(
        "--skip-merge",
        action="store_true",
        help="Use existing export/ merge outputs (provenance must include apply_job_id)",
    )
    exp.add_argument(
        "--review-job",
        type=int,
        default=None,
        help="Override review job id for dataset export",
    )
    exp.set_defaults(func=cmd_export)

    args = p.parse_args()
    if args.cmd == "export" and args.task_id is None and args.task_name is None:
        p.error("export requires --task-id or --task-name")
    args.func(args)


if __name__ == "__main__":
    main()
