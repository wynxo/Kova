#!/bin/sh
# curl -fsSL https://raw.githubusercontent.com/wynxo/Kova/main/uninstall.sh | sh
main() {
    set -eu
    say() { printf '%s\n' "$*"; }
    die() { say "Error: $*" >&2; exit 1; }
    purge=0
    keep=0
    for arg in "$@"; do
        case "$arg" in
            --purge) purge=1 ;;
            --keep-data) keep=1 ;;
            -h|--help) say 'Usage: uninstall.sh [--purge | --keep-data]'; return 0 ;;
            *) die "Unknown option: $arg" ;;
        esac
    done
    [ "$purge" -eq 0 ] || [ "$keep" -eq 0 ] || die 'Choose --purge or --keep-data, not both.'
    [ "$(id -u)" != 0 ] && [ -z "${SUDO_USER-}" ] && [ -z "${SUDO_UID-}" ] ||
        die 'Run as your normal user, without root or sudo.'
    [ -n "${HOME-}" ] && [ "$HOME" != / ] || die 'HOME must be your user home directory.'
    UV_TOOL_BIN_DIR="$HOME/.local/bin"
    UV_TOOL_DIR="$HOME/.local/share/uv/tools"
    UV_PYTHON_INSTALL_DIR="$HOME/.local/share/uv/python"
    UV_CACHE_DIR="$HOME/.cache/uv"
    export UV_TOOL_BIN_DIR UV_TOOL_DIR UV_PYTHON_INSTALL_DIR UV_CACHE_DIR
    data=${KOVA_HOME:-$HOME/.kova}
    if [ "$purge" -eq 1 ]; then
        # Canonicalize parent segments and symlinks before any destructive work.
        [ ! -L "$data" ] || die "Refusing to delete symlink: $data"
        if [ -d "$data" ]; then
            real_data=$(CDPATH='' cd -- "$data" && pwd -P)
            real_home=$(CDPATH='' cd -- "$HOME" && pwd -P)
            case "$real_data" in "$real_home"/*) ;; *) die "Refusing to delete $data" ;; esac
        fi
    fi
    uv=''
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$candidate" ]; then uv=$candidate; break; fi
    done
    if [ -z "$uv" ]; then uv=$(command -v uv || true); fi
    # Prefer the installed command so CLI and standalone semantics stay in sync.
    kova="$HOME/.local/bin/kova"
    if [ -x "$kova" ]; then
        if [ "$purge" -eq 1 ]; then "$kova" uninstall --purge
        elif [ "$keep" -eq 1 ]; then "$kova" uninstall --keep-data
        else "$kova" uninstall
        fi
        return
    fi
    # Recovery path when the launcher is missing, but uv still has the tool.
    if [ -f "$data/server.pid" ]; then
        pid=$(cat "$data/server.pid")
        case "$pid" in ''|*[!0-9]*) die 'Invalid server.pid; cannot safely stop the server.' ;; esac
        [ "$pid" -gt 1 ] || die 'Invalid server PID.'
        say "Stopping Kova server $pid ..."
        if kill -0 "$pid" 2>/dev/null; then kill "$pid" || die 'Could not stop Kova server.'; fi
    fi
    if [ -n "$uv" ]; then
        # uv treats an already-absent tool as a successful no-op.
        say 'Removing the Kova commands (kova, kova-app, kova-web): uv tool uninstall kova-ai'
        "$uv" tool uninstall kova-ai
    else
        say 'uv was not found; no uv tool can be removed.'
    fi
    desktop="$data/desktop"
    if [ -f "$desktop/package.json" ] && [ ! -L "$desktop" ]; then
        # Staged desktop app files are reinstalled by install.sh, unlike data.
        rm -rf -- "$desktop"
        say "Removed the desktop app files in $desktop."
    fi
    if [ -d "$data" ]; then
        if [ "$purge" -eq 0 ] && [ "$keep" -eq 0 ] && [ -t 0 ]; then
            printf 'Delete your Kova data in %s? [y/N] ' "$data"
            answer=''
            read -r answer || answer=''
            case "$answer" in y|Y|yes|YES|Yes) purge=1 ;; esac
        fi
        if [ "$purge" -eq 1 ]; then
            [ ! -L "$data" ] || die "Refusing to delete symlink: $data"
            real_data=$(CDPATH='' cd -- "$data" && pwd -P)
            real_home=$(CDPATH='' cd -- "$HOME" && pwd -P)
            case "$real_data" in "$real_home"/*) ;; *) die "Refusing to delete $data" ;; esac
            rm -rf -- "$data"
            say "Deleted $data."
        else
            say "Keeping your data in $data. Run uninstall.sh --purge to delete it."
        fi
    fi
    say 'External memory folders and files are left alone.'
    say 'Done. Kova is removed.'
}
main "$@"
