#!/bin/zsh

set -u

launcher_dir=${0:A:h}
cd "$launcher_dir" || exit 1

if ! command -v uv >/dev/null 2>&1; then
    print "uv is required. Install it from https://docs.astral.sh/uv/"
    read -r "?Press Return to close..."
    exit 1
fi

uv run mmd menu
menu_status=$?

print ""
read -r "?Press Return to close..."
exit "$menu_status"
