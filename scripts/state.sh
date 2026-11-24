#!/usr/bin/env bash
# Check out (pull) or save (push) the run state kept on the `state` branch, in ./state.
# Usage: scripts/state.sh pull | push "<message>"
set -euo pipefail
cd "$(dirname "$0")/.."
REMOTE="https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"

case "${1:-}" in
  pull)
    rm -rf state
    if git ls-remote --exit-code --heads "$REMOTE" state >/dev/null 2>&1; then
      git clone --quiet --depth 1 --branch state "$REMOTE" state
    else
      mkdir state && git -C state init --quiet -b state
      git -C state remote add origin "$REMOTE"
      echo "# Clipper run state (written by GitHub Actions, do not edit by hand)" > state/README.md
    fi
    ;;
  push)
    cd state
    git config user.name "clipper-bot"
    git config user.email "clipper-bot@users.noreply.github.com"
    git add -A
    if git diff --cached --quiet; then echo "state unchanged"; exit 0; fi
    git commit --quiet -m "${2:-update state}"
    for i in 1 2 3; do
      git push --quiet origin state && exit 0
      sleep 5
    done
    echo "could not push state" >&2; exit 1
    ;;
  *) echo "usage: $0 pull|push [message]" >&2; exit 2 ;;
esac
