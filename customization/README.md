# Customization bundle

Everything in this tree is **not upstream CVAT**: Python automation for a tracked-annotation workflow, small engine patches, and runbooks. Keeping it under `customization/` avoids clashing with upstream paths (for example `scripts/`).

Upstream CVAT splits work into default segment jobs. Our workflow instead uses:

- **`job_anchor`** — first N task-relative frames; one annotator defines canonical track identity (labels, track IDs, initial keyframes).
- **Stripe jobs** — K jobs with interleaved frame ownership (`offset`, `offset+K`, …); annotators refine copied tracks on their frames only.
- **Merge** — completed stripe keyframes override anchor where present; output is a single annotation payload plus provenance metadata.

Full step-by-step instructions: **[docs/customization.md](docs/customization.md)**.

## Prerequisites

1. **CVAT running** with the customization patches baked into the server image (see [Patches](#patches) below).
2. **Dev compose override** when building locally:

   ```bash
   export COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml
   ```

3. **Python 3** and dependencies used by the scripts (`requests`; run from repo root so `customization/scripts/` imports work).
4. **ffmpeg/ffprobe** for `cvat_create_task_from_video.py` probe/upload paths.

On a fresh clone of branch `local/cvat-customization`, patch sources are usually **already committed** — run `customization/patches/apply.sh check` (expect `applied (reverse apply ok)`). Rebuild `cvat_server` after clone so the container matches the tree.

## Environment

Copy [`.env.example`](../.env.example) to `.env` at the repo root (gitignored):

```bash
cp .env.example .env
```

Two variables sound similar but must not be mixed up:

| Variable | Used by | Example |
|----------|---------|---------|
| `CVAT_API_URL` | **Customization Python scripts** | `http://localhost:8080` (full URL) |
| `CVAT_HOST` | **docker compose / Traefik** | `localhost` or `james-26-laptop` (hostname only, no `http://`) |

**Docker Compose** reads `.env` automatically when you run `docker compose` from the repo root. **Customization scripts** load the same file via `cvat_api_env.py` (walk up from the current directory); variables already exported in your shell take precedence. You do not need `source .env` before running Python unless you prefer that workflow.

**Common mistake:** `CVAT_HOST=http://localhost:8080` breaks Traefik routing (404 on `/`). Scripts use `CVAT_API_URL`; compose uses `CVAT_HOST` as a hostname only.

More detail: [docs/customization.md](docs/customization.md#environment).

## Workflow at a glance

```text
1. Create task + upload video + job_anchor + stripe jobs
      → cvat_create_task_from_video.py

2. Annotate job_anchor (canonical tracks)

3. Copy anchor tracks → stripe jobs (+ optional assignees)
      → copy_job_tracks_to_jobs.py

4. Annotators complete their stripe jobs

5. Merge completed jobs → merged_annotations.json + provenance_manifest.json
      → merge_jobs_to_master_review.py

6. (Optional) candidate_manifest.json for audit
      → build_candidate_manifest.py
```

Detectrack handoff (zip with `manifest.json` + `clip.mp4`) registers a workspace and creates the task:

```bash
python3 customization/scripts/package_labeling_handoff.py import \
  --package /path/to/<task_name>.zip
```

After labeling and merge, ship labels back:

```bash
python3 customization/scripts/package_labeling_handoff.py export --task-id <id>
# -> tmp/labeling/<task_name>/outbound_<task_name>.zip
```

Local dev can still use `--video` / `--manifest` on unpacked folders via `cvat_create_task_from_video.py`.

## Layout

```text
customization/
  README.md                 ← this file
  scripts/                  Python CLI (run from CVAT repo root)
  patches/                  git-apply patches + apply.sh
  docs/
    customization.md        Full workflow runbook
    distribution.md         Deployment / HTTPS / hosting notes
```

## Scripts

All scripts use `customization/scripts/cvat_api_env.py` for `CVAT_API_URL`, auth, and optional `.env` loading.

| Script | Purpose |
|--------|---------|
| `cvat_create_task_from_video.py` | Create task, upload video (`--video` or `--manifest`), set `frame_filter`, build `job_anchor` + stripe jobs. Subcommands: `probe`, `create`, `wizard`. |
| `cvat_interleaved_jobs.py` | Rebuild manual-frame jobs on an existing task (`rebuild --task-id N`). |
| `copy_job_tracks_to_jobs.py` | Copy full anchor track keyframes into stripe jobs; optional `--assignees` round-robin. |
| `merge_jobs_to_master_review.py` | Merge completed annotation-stage jobs; auto-creates validation review job if needed; writes under `tmp/task-<id>/`. |
| `build_candidate_manifest.py` | Audit manifest of per-job candidates; optional `--admin-job`. |
| `package_labeling_handoff.py` | Import zip package → `tmp/labeling/<task_name>/`; export merged labels + format zips → `outbound_<task_name>.zip`. |
| `labeling_workspace.py` | Registry and workspace paths (imported by handoff + merge). |
| `cvat_export.py` | CVAT job dataset export helper (imported by handoff). |
| `cvat_frame_map.py` | Helpers for task-frame ↔ source-frame mapping (imported by other tools). |
| `cvat_api_env.py` | Shared `requests` session; not invoked directly. |

Example invocations:

```bash
# Interactive wizard
python3 customization/scripts/cvat_create_task_from_video.py --video /path/to/clip.mp4

# From detectrack sidecar
python3 customization/scripts/cvat_create_task_from_video.py \
  --manifest ~/detectrack_pipeline/data/cvat_exports/<task_name>/manifest.json

python3 customization/scripts/copy_job_tracks_to_jobs.py --task-id 9 --dry-run
python3 customization/scripts/copy_job_tracks_to_jobs.py --task-id 9 --assignees alice,bob

python3 customization/scripts/merge_jobs_to_master_review.py --task-id 9
python3 customization/scripts/merge_jobs_to_master_review.py --task-id 9 --dry-run
python3 customization/scripts/merge_jobs_to_master_review.py --task-id 9 --apply-job <review_job_id>
```

After editing scripts:

```bash
python3 -m py_compile customization/scripts/cvat_create_task_from_video.py \
  customization/scripts/cvat_interleaved_jobs.py \
  customization/scripts/copy_job_tracks_to_jobs.py \
  customization/scripts/merge_jobs_to_master_review.py \
  customization/scripts/build_candidate_manifest.py \
  customization/scripts/labeling_workspace.py \
  customization/scripts/cvat_export.py \
  customization/scripts/package_labeling_handoff.py
```

## Patches

`0001-allow-manual-annotation-jobs.patch` extends the CVAT API so automation can:

- `POST /api/jobs` with `type=annotation`, `frame_selection_method=manual`, and an explicit `frames` list.
- `DELETE /api/jobs/{id}` for annotation jobs (upstream allows GT only).

Without a **patched** `cvat/server` image, create/rebuild scripts fail with errors like `Only ground truth jobs can be removed` or `Unexpected job type 'annotation'`.

From repo root:

```bash
customization/patches/apply.sh check    # already applied on local/cvat-customization?
customization/patches/apply.sh apply    # only on a clean upstream tree
customization/patches/apply.sh revert   # before merging upstream develop
```

Then build and start:

```bash
export CVAT_HOST=localhost   # or hostname you use in the browser
docker compose build cvat_server
docker compose up -d
```

Patch maintenance detail: **[patches/README.md](patches/README.md)**.

## Docker quick reference

```bash
cd ~/repos/cvat   # or your clone path

# .env holds CVAT_HOST (+ optional COMPOSE_FILE); compose loads it automatically
docker compose up -d
docker compose down
docker compose ps
docker compose logs -f cvat_server traefik
```

Run scripts from the repo root (or any subdirectory — `.env` is found by walking parents):

```bash
python3 customization/scripts/copy_job_tracks_to_jobs.py --task-id 9
```

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/customization.md](docs/customization.md) | Full workflow, manifest fields, merge rules, output files, API checks |
| [docs/distribution.md](docs/distribution.md) | Hosting, HTTPS, Cloudflare, server sizing |
| [patches/README.md](patches/README.md) | Patch format, upstream upgrade procedure |

## Outputs

Merge and manifest scripts write under **`tmp/task-<task_id>/`** or, for handoff tasks, **`tmp/labeling/<task_name>/export/`** (gitignored):

- `merged_annotations.json` — CVAT annotation payload for review/import
- `provenance_manifest.json` — which job/assignee owned each merged frame (`apply_job_id` for export)
- `candidate_manifest.json` — per-job candidate preservation for audit
- `annotations_cvat.zip` / `annotations_mot.zip` — format exports when using `package_labeling_handoff.py export`
