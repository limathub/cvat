# Local CVAT Patches

This directory keeps minimal local modifications to upstream CVAT so we can track them as patch files instead of maintaining a heavy fork.

## Why

We only change a small surface of upstream CVAT to support our tracked-annotation workflow. Storing those changes as patch files makes upstream upgrades easier: apply patches once, fix only on conflict.

## Layout

```text
customization/patches/
  0001-allow-manual-annotation-jobs.patch
  apply.sh
  README.md
```

Patch filename convention: `NNNN-short-description.patch`. Increment `NNNN` for new patches. Keep each patch focused on one logical change.

## What Each Patch Does

`0001-allow-manual-annotation-jobs.patch`

- Files: `cvat/apps/engine/serializers.py`, `cvat/apps/engine/views.py`
- `JobWriteSerializer.create`: allow `type == ANNOTATION` in addition to `GROUND_TRUTH`. GT-only validation and `validation_layout` updates are gated to GT jobs only.
- `JobViewSet.perform_destroy`: allow deleting `ANNOTATION` jobs in addition to `GROUND_TRUTH` jobs.
- Purpose: enable manual-frame annotation job creation/deletion via API for the stripe/anchor workflow.

## Day-To-Day Usage

Run from the **CVAT repository root** (parent of `customization/`):

```bash
customization/patches/apply.sh check
customization/patches/apply.sh apply
customization/patches/apply.sh revert
```

## Upgrading Upstream CVAT

1. Make sure you have a clean working tree.
2. Revert local patches so the tree matches upstream:

   ```bash
   customization/patches/apply.sh revert
   ```

3. Pull upstream changes:

   ```bash
   git fetch upstream
   git merge upstream/develop   # or rebase, your choice
   ```

4. Re-apply patches:

   ```bash
   customization/patches/apply.sh apply
   ```

5. If `apply.sh apply` fails, the upstream code in the affected files changed. Fix the conflict by hand, then regenerate the patch:

   ```bash
   git diff cvat/apps/engine/serializers.py cvat/apps/engine/views.py \
     > customization/patches/0001-allow-manual-annotation-jobs.patch
   ```

6. Run CVAT tests / smoke tests, commit the result.

## Notes

- Patches are plain `git diff` outputs and applied with `git apply`.
- Keep patches small. If a change grows large, consider whether it should be a real upstream PR.
- Do not commit local generated artifacts (e.g. backup files) into `customization/patches/`.
