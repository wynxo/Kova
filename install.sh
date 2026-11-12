#!/usr/bin/env bash
# Install Kova: uv, its dependencies, and the `kova` command on your PATH.
# Safe to run again: it updates what is already there.
set -euo pipefail

cd -- "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '%s\n' "$*"; }

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

say "Kova installer"
say ""

UV="$(find_uv || true)"
if [ -z "$UV" ]; then
    say "uv is not installed. Installing it now from https://astral.sh/uv ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    UV="$(find_uv || true)"
    if [ -z "$UV" ]; then
        say "uv's installer ran, but uv still was not found."
        say "Close this terminal, open a new one, and run ./install.sh again."
        exit 1
    fi
    say "Installed uv at $UV."
else
    say "Using uv at $UV."
fi

PATH="$(dirname -- "$UV"):$PATH"
export PATH

say ""
say "Installing Kova's dependencies (uv sync) ..."
"$UV" sync

say ""
if "$UV" tool list 2>/dev/null | grep -q '^kova '; then
    say "Updating the kova command ..."
    "$UV" tool install --editable . --force
else
    say "Installing the kova command ..."
    "$UV" tool install --editable .
fi

say ""
if ! command -v kova >/dev/null 2>&1; then
    say "Note: the kova command was installed, but its folder is not on your PATH."
    say "Add this to your shell profile and restart the terminal:"
    say "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    say ""
fi

say "Done. Now run: kova chat"
say "First time? Point Kova at a model server: kova model add NAME URL MODEL"
