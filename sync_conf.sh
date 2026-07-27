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
RSYNC_OPTIONS=(
    --archive
    --human-readable
    --itemize-changes
)

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

    rsync "${RSYNC_OPTIONS[@]}" \
        "${PROJECT_DIR}/.env" \
        "${PROJECT_DIR}/.secrets" \
        "${PROJECT_DIR}/data" \
        "${REMOTE_HOST}:${REMOTE_DIR}/"
else
    printf 'Pulling .env, .secrets, and data/ from %s:%s/ into %s/\n' \
        "${REMOTE_HOST}" "${REMOTE_DIR}" "${PROJECT_DIR}"

    rsync "${RSYNC_OPTIONS[@]}" \
        --include='/.env' \
        --include='/.secrets' \
        --include='/data/***' \
        --exclude='*' \
        "${REMOTE_HOST}:${REMOTE_DIR}/" \
        "${PROJECT_DIR}/"
fi
