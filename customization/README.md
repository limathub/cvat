# Customization bundle

This directory groups everything that is **not upstream CVAT**: automation scripts, local patches, and runbooks. Keeping it under one tree avoids name clashes with future upstream directories (for example `scripts/`).

## Layout

```text
customization/
  README.md                 ← this file
  scripts/                  Python CLI tools (run from repo root)
  patches/                  git-apply patches + apply.sh
  docs/
    customization.md        Tracked-annotation workflow runbook
    distribution.md         HTTPS / deployment / patch strategy notes
```

## Environment

Copy [`.env.example`](../.env.example) to `.env` at the repo root (or export `CVAT_HOST` / `CVAT_API_URL` and auth vars manually). See [docs/customization.md](docs/customization.md#environment).

## Quick commands

From the CVAT repository root:

```bash
# Patches (upstream CVAT code changes)
customization/patches/apply.sh check
customization/patches/apply.sh apply
customization/patches/apply.sh revert

# Example automation (see docs/customization.md)
python3 customization/scripts/cvat_create_task_from_video.py --video /path/to/video.mp4
```

## Documentation

- Workflow: [docs/customization.md](docs/customization.md)
- Distribution / HTTPS / updates: [docs/distribution.md](docs/distribution.md)
- Patch workflow detail: [patches/README.md](patches/README.md)
