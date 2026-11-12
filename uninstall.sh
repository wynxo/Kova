#!/usr/bin/env bash
# Remove the `kova` command and, if you say so, your Kova data.
# Your data is kept unless you confirm deleting it (or pass --purge).
set -euo pipefail

cd -- "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

KOVA_DIR="${KOVA_HOME:-$HOME/.kova}"
DELETE_DATA=0
SKIP_ASK=0

say() { printf '%s\n' "$*"; }

for arg in "$@"; do
    case "$arg" in
        --purge) DELETE_DATA=1 ;;
        --keep-data) SKIP_ASK=1 ;;
        -h | --help)
            say "Usage: ./uninstall.sh [--keep-data | --purge]"
            say ""
            say "  --keep-data   stop the server and remove the kova command, keep $KOVA_DIR"
            say "  --purge       also delete $KOVA_DIR without asking"
            exit 0
            ;;
        *)
            say "Unknown option: $arg"
            exit 2
            ;;
    esac
done

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

say "Stopping the Kova server (if running) ..."
if command -v kova >/dev/null 2>&1; then
    kova stop >/dev/null 2>&1 || true
else
    say "The kova command is not installed; nothing to stop."
fi

say "Removing the kova command ..."
UV="$(find_uv || true)"
if [ -n "$UV" ]; then
    "$UV" tool uninstall kova >/dev/null 2>&1 || true
    say "The kova command is gone."
else
    say "uv was not found; nothing to remove there."
fi

# Data lives in $KOVA_HOME (default ~/.kova). Your memory notes and files may
# point elsewhere (config.toml); those are never touched here.
custom_memory=""
if [ -f "$KOVA_DIR/config.toml" ]; then
    custom_memory="$(awk -F'"' '
        /^\[memory\]/ { in_memory = 1; next }
        /^\[/ { in_memory = 0 }
        in_memory && /^path *=/ { print $2 }
    ' "$KOVA_DIR/config.toml" 2>/dev/null || true)"
fi

say ""
if [ ! -d "$KOVA_DIR" ]; then
    say "Done. Kova is removed."
    exit 0
fi

if [ -n "$custom_memory" ] && [ "$custom_memory" != "$KOVA_DIR/memory" ]; then
    say "Your memory notes live in $custom_memory, outside $KOVA_DIR. They are left alone."
fi

remove_data() {
    case "$KOVA_DIR" in
        "" | "/" | "$HOME")
            say "Refusing to delete $KOVA_DIR."
            exit 1
            ;;
    esac
    rm -rf -- "$KOVA_DIR"
    say "Deleted $KOVA_DIR."
}

if [ "$DELETE_DATA" -eq 1 ]; then
    remove_data
elif [ "$SKIP_ASK" -eq 1 ]; then
    say "Keeping your data in $KOVA_DIR."
elif [ -t 0 ]; then
    printf 'Delete your Kova data in %s (chats, memory, model settings, token)? [y/N] ' "$KOVA_DIR"
    answer=""
    read -r answer || answer=""
    case "$answer" in
        y | Y | yes | YES | Yes) remove_data ;;
        *) say "Keeping your data in $KOVA_DIR." ;;
    esac
else
    say "Keeping your data in $KOVA_DIR (not a terminal, so nobody was asked)."
    say "Run ./uninstall.sh --purge to delete it."
fi

say ""
say "Done. Kova is removed. It still runs from this checkout with ./cli."
