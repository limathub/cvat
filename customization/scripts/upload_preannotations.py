#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
Upload detectrack preannotations into a task's jobs, one job at a time.

CVAT's task-level annotation import ignores manual-frame job structure: it
copies every box into every job, so each stripe job ends up holding the whole
file and only a handful of its boxes land on frames that job owns. That breaks
the merge step, which treats a stripe job as the owner of its own frames.

So each job is given only the boxes on the frames it owns, as `shapes` rather
than tracks — a per-frame detection needs no interpolation and no terminator.

Two numbering conventions meet here, which is where uploads go wrong:

  - The XML `frame` attribute from `detectrack export-cvat` is **clip-relative**
    (multiples of the step). That is what CVAT's own import accepts; task
    ordinals are rejected as "Unknown frame N".
  - The job endpoints (`/annotations`, `/data/meta`) speak **task ordinals**
    (`clip_frame // step`).

`cvat_frame_map.py` owns that translation; do not redo it inline.

The anchor job is deliberately left bare. It defines the canonical tracks every
stripe job inherits, so seeding it with model boxes would push the labeler
toward the model's idea of the object's extent and that bias would land in the
ground truth.

Environment: same as the other customization scripts (CVAT_API_URL, auth).

Usage:
  python3 customization/scripts/upload_preannotations.py \
      --task-name S01_002__0014_0060 --xml /path/to/preannotations.xml
"""
import sys, xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0, str(Path.home() / "repos/cvat/customization/scripts"))
from cvat_api_env import cvat_session_from_env
from cvat_frame_map import included_task_frames, load_task_data_meta, frame_step

import argparse

ap = argparse.ArgumentParser(description=__doc__,
                             formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--task-name", required=True, help="Handoff task_name from the manifest")
ap.add_argument("--xml", required=True, type=Path, help="preannotations.xml from export-cvat")
ap.add_argument("--keep-existing", action="store_true",
                help="Skip the task-annotation wipe (default clears first).")
args = ap.parse_args()
TASK_NAME, XML = args.task_name, args.xml

s = cvat_session_from_env(); base = s.base_url.rstrip("/")
task = next(t for t in s.get(f"{base}/api/tasks", params={"search": TASK_NAME}).json()["results"]
            if t["name"] == TASK_NAME)
tid = task["id"]

if not args.keep_existing:
    r = s.delete(f"{base}/api/tasks/{tid}/annotations")
    print("cleared task annotations ->", r.status_code)

labels = s.get(f"{base}/api/labels", params={"task_id": tid}).json()["results"]
label_id = next(lb["id"] for lb in labels if lb["name"] == "smoke")

meta = load_task_data_meta(s, tid)
step = frame_step(meta)
root = ET.parse(XML).getroot()
by_task_frame = {}
for tr in root.findall("track"):
    for b in tr.findall("box"):
        if b.get("outside") == "1":
            continue                      # shapes need no terminator
        tf = int(b.get("frame")) // step   # clip-relative -> task ordinal
        by_task_frame.setdefault(tf, []).append(
            [float(b.get("xtl")), float(b.get("ytl")), float(b.get("xbr")), float(b.get("ybr"))]
        )
total_boxes = sum(len(v) for v in by_task_frame.values())
print(f"parsed {total_boxes} boxes on {len(by_task_frame)} task frames "
      f"({min(by_task_frame)}..{max(by_task_frame)})")

jobs = sorted(s.get(f"{base}/api/jobs", params={"task_id": tid, "page_size": 50}).json()["results"],
              key=lambda j: j["id"])
anchor_id = min(jobs, key=lambda j: j["frame_count"])["id"]
placed = 0
for j in jobs:
    jmeta = s.get(f"{base}/api/jobs/{j['id']}/data/meta").json()
    own = set(included_task_frames(jmeta))
    if j["id"] == anchor_id:
        print(f"job {j['id']}: anchor — left bare ({len(own)} frames)")
        continue
    shapes = [
        {"type": "rectangle", "occluded": False, "outside": False, "z_order": 0,
         "rotation": 0.0, "points": pts, "frame": f, "label_id": label_id,
         "group": 0, "source": "manual", "attributes": []}
        for f in sorted(own & set(by_task_frame))
        for pts in by_task_frame[f]
    ]
    resp = s.put(f"{base}/api/jobs/{j['id']}/annotations",
                 json={"version": 0, "tags": [], "shapes": shapes, "tracks": []})
    placed += len(shapes)
    print(f"job {j['id']}: {len(own):3d} frames -> {len(shapes):3d} boxes  [{resp.status_code}]")
print(f"placed {placed} of {total_boxes} boxes "
      f"({total_boxes - placed} fell on anchor-only frames)")
