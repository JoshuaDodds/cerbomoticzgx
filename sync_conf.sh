#!/usr/bin/env bash
#
# Synchronize cerbomoticzGx runtime configuration and persistent data to the
# cluster storage mounted by the Kubernetes deployment.
#
# This intentionally does not use --delete: files that exist only on the
# cluster are preserved.

set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_HOST="${SYNC_CONF_HOST:-root@n1}"
REMOTE_DIR="${SYNC_CONF_REMOTE_DIR:-/pv-storage/cerbomoticzgx-secrets}"

usage() {
    cat <<'EOF'
Usage: ./sync_conf.sh [--push|--pull] [--dry-run]

Pushes .env, .secrets, and data/ to:
  root@n1:/pv-storage/cerbomoticzgx-secrets/

Options:
  --push      Copy this development environment to cluster storage (default)
  --pull      Copy cluster storage into this development environment
  --dry-run   Show what would change without copying anything
  -h, --help  Show this help

Environment overrides:
  SYNC_CONF_HOST        SSH destination (default: root@n1)
  SYNC_CONF_REMOTE_DIR  Remote directory
EOF
}

DIRECTION="push"
direction_explicitly_set=""
DRY_RUN="false"
RSYNC_OPTIONS=(
    --archive
    --human-readable
    --itemize-changes
    --out-format='%i|%n%L'
)

CHANGE_LOG=""
cleanup() {
    if [[ -n "${CHANGE_LOG}" ]]; then
        rm -f -- "${CHANGE_LOG}"
    fi
}
trap cleanup EXIT

print_changes() {
    local destination
    if [[ "${DIRECTION}" == "push" ]]; then
        destination="the cluster (remote n1)"
    else
        destination="this checkout (local)"
    fi

    awk -F'|' -v destination="${destination}" '
        function print_heading(kind, count) {
            if (count) printf "\n%s %s (%d):\n", kind, destination, count
        }
        function print_paths(kind, marker, list, count,    i) {
            print_heading(kind, count)
            for (i = 1; i <= count; i++) printf "  %s %s\n", marker, list[i]
        }
        {
            item = $1
            path = substr($0, length(item) + 2)
            if (!path || item == "*deleting") next

            # The itemized format is YXcstpoguax. A + marks a new path;
            # s/c means file content changed; everything else is metadata.
            if (item ~ /\+\+\+\+\+\+\+\+\+/) {
                added[++added_count] = path
            } else if (substr(item, 2, 1) == "f" &&
                       (substr(item, 3, 1) != "." || substr(item, 4, 1) != ".")) {
                updated[++updated_count] = path
            } else {
                metadata[++metadata_count] = path
            }
        }
        END {
            if (!added_count && !updated_count && !metadata_count) {
                printf "No files need copying; both locations are already in sync.\n"
                exit
            }
            print_paths("Added to", "+", added, added_count)
            print_paths("Updated in", "~", updated, updated_count)
            print_paths("Metadata updated in", "·", metadata, metadata_count)
        }
    ' "${CHANGE_LOG}"
}

run_rsync() {
    CHANGE_LOG="$(mktemp "${TMPDIR:-/tmp}/sync-conf.XXXXXX")"
    if ! rsync "$@" >"${CHANGE_LOG}"; then
        printf 'Synchronization failed; no summary was produced.\n' >&2
        exit 1
    fi
    print_changes
}

while (($#)); do
    case "$1" in
        --push|--pull)
            requested_direction="${1#--}"
            if [[ -n "${direction_explicitly_set}" &&
                  "${DIRECTION}" != "${requested_direction}" ]]; then
                printf 'Choose either --push or --pull, not both.\n' >&2
                exit 2
            fi
            DIRECTION="${requested_direction}"
            direction_explicitly_set="yes"
            ;;
        --dry-run)
            RSYNC_OPTIONS+=(--dry-run)
            DRY_RUN="true"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ "${REMOTE_DIR}" != /* || "${REMOTE_DIR}" == "/" ]]; then
    printf 'Refusing unsafe remote directory: %s\n' "${REMOTE_DIR}" >&2
    exit 1
fi

if [[ "${DIRECTION}" == "push" ]]; then
    for required_path in .env .secrets data; do
        if [[ ! -e "${PROJECT_DIR}/${required_path}" ]]; then
            printf 'Required source is missing: %s\n' \
                "${PROJECT_DIR}/${required_path}" >&2
            exit 1
        fi
    done

    printf 'Pushing .env, .secrets, and data/ to %s:%s/\n' \
        "${REMOTE_HOST}" "${REMOTE_DIR}"
    if [[ "${DRY_RUN}" == "true" ]]; then
        printf 'Dry run — no files will be copied.\n'
    fi

    run_rsync "${RSYNC_OPTIONS[@]}" \
        "${PROJECT_DIR}/.env" \
        "${PROJECT_DIR}/.secrets" \
        "${PROJECT_DIR}/data" \
        "${REMOTE_HOST}:${REMOTE_DIR}/"
else
    printf 'Pulling .env, .secrets, and data/ from %s:%s/ into %s/\n' \
        "${REMOTE_HOST}" "${REMOTE_DIR}" "${PROJECT_DIR}"
    if [[ "${DRY_RUN}" == "true" ]]; then
        printf 'Dry run — no files will be copied.\n'
    fi

    run_rsync "${RSYNC_OPTIONS[@]}" \
        --include='/.env' \
        --include='/.secrets' \
        --include='/data/***' \
        --exclude='*' \
        "${REMOTE_HOST}:${REMOTE_DIR}/" \
        "${PROJECT_DIR}/"
fi
