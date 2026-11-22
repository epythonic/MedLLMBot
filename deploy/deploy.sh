#!/usr/bin/env bash
# Updates the demo server to the latest main: code → rebuild containers → check that everything is up.
#
# Run from GitHub Actions over SSH after a PR is merged into main. The CI key is bound in authorized_keys
# to this script (command="…/deploy/deploy.sh"), so nothing else can be run with that key.
# Can also be run by hand: ./deploy/deploy.sh
#
# If text arrives on stdin (CI passes a .env built from the secrets of the GitHub Environment «demo»),
# it becomes the new .env with mode 600. When run by hand from a terminal, .env is left alone.
#
# The body is in a function: git updates this file too, and bash reads the script as it executes.
# The whole function is parsed before it runs, so the new version of the file does not mix with the old one.

set -euo pipefail

main() {
    cd "$(dirname "$0")/.."

    if [[ ! -t 0 ]]; then
        local env_content
        env_content="$(cat)"
        if [[ -n "$env_content" ]]; then
            echo "→ Updating .env from GitHub secrets"
            (umask 077 && printf '%s\n' "$env_content" > .env.new)
            mv .env.new .env
        fi
    fi

    echo "→ Fetching the latest main"
    git fetch --quiet origin main
    # Fast-forward only: edits made directly on the server stop the deploy instead of being lost
    git merge --ff-only --quiet origin/main
    echo "  version $(git rev-parse --short HEAD): $(git log -1 --format=%s)"

    echo "→ Rebuilding and restarting containers"
    docker compose --profile https up -d --build --remove-orphans

    echo "→ Waiting for the admin to become healthy"
    local health=""
    for _ in $(seq 1 45); do
        health="$(docker compose ps admin --format '{{.Health}}')"
        [[ "$health" == "healthy" ]] && break
        sleep 2
    done
    if [[ "$health" != "healthy" ]]; then
        echo "✗ The admin did not come up (status: ${health:-none}). Last log lines:" >&2
        docker compose logs --tail 50 admin >&2
        exit 1
    fi

    if [[ "$(docker compose ps bot --format '{{.State}}')" != "running" ]]; then
        echo "✗ The bot is not running. Last log lines:" >&2
        docker compose logs --tail 50 bot >&2
        exit 1
    fi

    # Old images left after a rebuild take up space on a small server
    docker image prune -f >/dev/null
    echo "✓ Demo updated to $(git rev-parse --short HEAD)"
}

main "$@"
exit
