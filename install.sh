#!/bin/sh
# curl -fsSL https://raw.githubusercontent.com/wynxo/Kova/main/install.sh | sh
# Keep all executable work inside main: an incomplete download cannot install.
main() {
    set -eu
    say() { printf '%s\n' "$*"; }
    die() { say "Error: $*" >&2; exit 1; }
    ref=${KOVA_REF-main}
    dev=0
    register_desktop=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --ref) [ "$#" -ge 2 ] || die '--ref needs a value'; ref=$2; shift 2 ;;
            --dev) dev=1; shift ;;
            --desktop) register_desktop=1; shift ;;
            -h|--help) say 'Usage: install.sh [--ref REF] [--dev] [--desktop]'; return 0 ;;
            *) die "Unknown option: $1" ;;
        esac
    done
    case "$ref" in ''|-*|*[!a-zA-Z0-9_./-]*) die 'Invalid ref: use a branch, tag, or commit SHA.' ;; esac
    [ "$(id -u)" != 0 ] && [ -z "${SUDO_USER-}" ] && [ -z "${SUDO_UID-}" ] ||
        die 'Run as your normal user, without root or sudo.'
    [ -n "${HOME-}" ] && [ "$HOME" != / ] || die 'HOME must be your user home directory.'

    # Explicit user paths: never write to a global/XDG tool or Python location.
    UV_TOOL_BIN_DIR="$HOME/.local/bin"
    UV_TOOL_DIR="$HOME/.local/share/uv/tools"
    UV_PYTHON_INSTALL_DIR="$HOME/.local/share/uv/python"
    UV_CACHE_DIR="$HOME/.cache/uv"
    export UV_TOOL_BIN_DIR UV_TOOL_DIR UV_PYTHON_INSTALL_DIR UV_CACHE_DIR
    uv=''
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$candidate" ]; then uv=$candidate; break; fi
    done
    if [ -z "$uv" ]; then uv=$(command -v uv || true); fi
    if [ -z "$uv" ]; then
        say 'Installing uv from https://astral.sh/uv/install.sh ...'
        command -v curl >/dev/null 2>&1 || die 'curl is required to install uv.'
        # Download completely before executing; never edit shell startup files.
        mkdir -p "$HOME/.local/bin"
        uv_installer=$(mktemp "$HOME/.local/bin/.uv-install.XXXXXX")
        trap 'rm -f "$uv_installer"' 0
        curl -fsSL https://astral.sh/uv/install.sh -o "$uv_installer"
        UV_UNMANAGED_INSTALL="$HOME/.local/bin" sh "$uv_installer"
        rm -f "$uv_installer"
        trap - 0
        uv="$HOME/.local/bin/uv"
        [ -x "$uv" ] || die 'uv was not found after installation.'
    fi
    say "Using uv at $uv."
    # This folder, when the installer runs from a checkout (--dev or KOVA_SOURCE).
    here=$(CDPATH='' cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || :)
    if [ "$dev" -eq 1 ]; then
        # --dev is a checkout-only path; piped installs use GitHub by default.
        source=${KOVA_SOURCE-$here}
        [ -f "$source/pyproject.toml" ] || die '--dev needs a checkout (or KOVA_SOURCE).'
    elif [ -n "${KOVA_SOURCE-}" ]; then
        source=$KOVA_SOURCE
    elif command -v git >/dev/null 2>&1; then
        source="kova-ai @ git+https://github.com/wynxo/Kova@$ref"
    else
        say 'git was not found; using the GitHub source archive.'
        source="kova-ai @ https://github.com/wynxo/Kova/archive/$ref.tar.gz"
    fi
    # Future PyPI switch: replace source above with source='kova-ai==VERSION'.
    say "Installing/updating Kova: $source"
    if [ "$dev" -eq 1 ]; then
        "$uv" tool install --force --refresh --python 3.12 --editable "$source"
    else
        "$uv" tool install --force --refresh --python 3.12 "$source"
    fi

    # The Electron app is not part of the Python wheel, so keep a copy of
    # desktop/ under the data dir: kova-app runs it (and npm ci) from there.
    data=${KOVA_HOME:-$HOME/.kova}
    dest=$data/desktop
    desktop_src=''
    if [ -n "${KOVA_SOURCE-}" ] && [ -f "$KOVA_SOURCE/desktop/package.json" ]; then
        desktop_src=$KOVA_SOURCE/desktop
    elif [ -n "$here" ] && [ -f "$here/desktop/package.json" ]; then
        desktop_src=$here/desktop
    elif command -v curl >/dev/null 2>&1 && command -v tar >/dev/null 2>&1 &&
         command -v mktemp >/dev/null 2>&1; then
        tree=$(mktemp -d "${TMPDIR:-/tmp}/kova-desktop.XXXXXX")
        trap 'rm -rf -- "$tree"' 0
        url="https://github.com/wynxo/Kova/archive/$ref.tar.gz"
        say "Downloading the desktop app from $url ..."
        if curl -fsSL "$url" -o "$tree/kova.tar.gz" && mkdir -p "$tree/tree" &&
           tar -xzf "$tree/kova.tar.gz" -C "$tree/tree"; then
            for candidate in "$tree/tree"/Kova-*/desktop; do
                if [ -f "$candidate/package.json" ]; then desktop_src=$candidate; break; fi
            done
        fi
        rm -rf -- "$tree"
        trap - 0
    fi
    if [ -n "$desktop_src" ]; then
        mkdir -p "$dest"
        for item in "$desktop_src"/*; do
            [ -e "$item" ] || continue
            name=$(basename -- "$item")
            # node_modules and dist are rebuilt by kova-app and electron-builder.
            case $name in node_modules|dist) continue ;; esac
            rm -rf -- "$dest/$name"
            cp -R -- "$item" "$dest/$name"
        done
        say "Desktop app files: $dest"
    else
        say 'Note: the desktop app files were not installed; kova-web works now.'
        say 'Rerun this installer (curl and tar are required) to add kova-app.'
    fi

    say 'To use kova in this terminal, run (or add to your shell profile):'
    # shellcheck disable=SC2016 # Print a copyable shell command, not current values.
    say '  export PATH="$HOME/.local/bin:$PATH"'
    if [ "$register_desktop" -eq 1 ]; then
        [ "$(uname -s)" = Linux ] || die '--desktop registration is Linux-only.'
        [ -f "$dest/package.json" ] || die 'The desktop app files are missing; rerun without --desktop first.'
        [ -f "$dest/assets/icon.png" ] || die 'The desktop app files are incomplete: assets/icon.png is missing.'
        # Desktop Exec has its own quoting rules; reject unusual install paths.
        case "$UV_TOOL_BIN_DIR" in *[!a-zA-Z0-9_./\ -]*) die 'Unsupported launcher path for the desktop entry.' ;; esac
        apps="$HOME/.local/share/applications"
        icons="$HOME/.local/share/icons/hicolor/512x512/apps"
        mkdir -p "$apps" "$icons"
        cp "$dest/assets/icon.png" "$icons/kova.png"
        {
            say '[Desktop Entry]'
            say 'Type=Application'
            say 'Name=Kova'
            say 'Comment=Private local AI assistant'
            # The session's PATH may miss ~/.local/bin, so point at the launcher.
            printf 'Exec="%s/kova-app"\n' "$UV_TOOL_BIN_DIR"
            say 'Icon=kova'
            say 'Terminal=false'
            say 'Categories=Utility;'
            say 'StartupWMClass=Kova'
        } > "$apps/kova.desktop"
        say 'Registered Kova in your application menu.'
    fi
    say 'Done. Run: kova setup'
    say 'Then: kova'
    say 'Desktop app: kova-app    Web UI in your browser: kova-web'
}
main "$@"
