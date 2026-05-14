#!/usr/bin/env bash
# Manage local CVAT patches (apply / revert / check).
# Usage:
#   customization/patches/apply.sh apply
#   customization/patches/apply.sh revert
#   customization/patches/apply.sh check

set -euo pipefail

cmd="${1:-}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"

if [[ -z "$cmd" ]]; then
    echo "usage: $0 {apply|revert|check}" >&2
    exit 2
fi

mapfile -t patches < <(find "$script_dir" -maxdepth 1 -type f -name '*.patch' | sort)

if [[ ${#patches[@]} -eq 0 ]]; then
    echo "no patches found in $script_dir" >&2
    exit 1
fi

cd "$repo_root"

run_each() {
    local action="$1"
    shift
    local args=("$@")
    local failed=0
    for patch in "${patches[@]}"; do
        local rel="${patch#$repo_root/}"
        if git apply "${args[@]}" "$patch"; then
            echo "$action ok: $rel"
        else
            echo "$action FAILED: $rel" >&2
            failed=1
        fi
    done
    return $failed
}

check_one() {
    local patch="$1"
    local rel="${patch#$repo_root/}"
    if git apply --check "$patch" >/dev/null 2>&1; then
        echo "unapplied (forward apply ok): $rel"
        return 0
    fi
    if git apply --check -R "$patch" >/dev/null 2>&1; then
        echo "applied (reverse apply ok): $rel"
        return 0
    fi
    echo "CONFLICT (neither forward nor reverse applies): $rel" >&2
    return 1
}

case "$cmd" in
    check)
        failed=0
        for patch in "${patches[@]}"; do
            if ! check_one "$patch"; then
                failed=1
            fi
        done
        exit $failed
        ;;
    apply)
        run_each apply
        ;;
    revert)
        # Apply patches in reverse order when reverting so dependent
        # patches unwind before their prerequisites.
        mapfile -t reversed < <(printf '%s\n' "${patches[@]}" | tac)
        patches=("${reversed[@]}")
        run_each revert -R
        ;;
    *)
        echo "usage: $0 {apply|revert|check}" >&2
        exit 2
        ;;
esac
