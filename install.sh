#!/bin/sh
# curl -fsSL https://raw.githubusercontent.com/wynxo/Kova/main/install.sh | sh
# Keep all executable work inside main: an incomplete download cannot install.
main() {
    set -eu
    say() { printf '%s\n' "$*"; }
    die() { say "Error: $*" >&2; exit 1; }
    ref=${KOVA_REF-main}
    dev=0
    register_desktop=1
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --ref) [ "$#" -ge 2 ] || die '--ref needs a value'; ref=$2; shift 2 ;;
            --dev) dev=1; shift ;;
            --desktop) register_desktop=1; shift ;; # compatibility alias
            --no-desktop) register_desktop=0; shift ;;
            -h|--help) say 'Usage: install.sh [--ref REF] [--dev] [--no-desktop]'; return 0 ;;
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
        "$uv" tool install --force --refresh --upgrade-package kova-ai --python 3.12 --editable "$source"
    else
        "$uv" tool install --force --refresh --upgrade-package kova-ai --python 3.12 "$source"
    fi

    # The desktop app is optional: if it cannot be prepared, finish the CLI
    # install anyway and say how to add the app later.
    packaged_registration=0
    skip_desktop() {
        say "Note: $* Kova itself is installed; open it in your browser with kova web."
        if [ -n "${tree-}" ]; then rm -rf -- "$tree"; trap - 0; fi
        register_desktop=0
    }
    prepare_desktop() {
        if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1 ||
           ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 22 ? 0 : 1)'; then
            skip_desktop 'The desktop app needs Node.js 22+ (including npm). Install it from https://nodejs.org and rerun this installer to add Kova to your app menu.'; return
        fi
        # Prepare Electron from the installed revision before registering the launcher.
        data=${KOVA_HOME:-$HOME/.kova}
        dest=$data/desktop
        if [ -x "$UV_TOOL_BIN_DIR/kova" ] && "$UV_TOOL_DIR/kova-ai/bin/python" -c \
            'from kova.app import refresh_desktop' 2>/dev/null; then
            if "$UV_TOOL_DIR/kova-ai/bin/python" -c 'import kova.desktop_registration' 2>/dev/null; then
                "$UV_TOOL_BIN_DIR/kova" app --prepare --register ||
                    { skip_desktop 'Preparing or registering the desktop app failed.'; return; }
                packaged_registration=1
                say "Desktop app files: $dest"
                say 'Registered Kova in your application menu.'
                return
            fi
            "$UV_TOOL_BIN_DIR/kova" app --prepare ||
                { skip_desktop 'Preparing the desktop app failed.'; return; }
            desktop_src=$dest
        else
            # Compatibility with refs that predate packaged desktop assets.
            desktop_src=''
            if [ -n "${KOVA_SOURCE-}" ] && [ -f "$KOVA_SOURCE/src/kova/desktop/package.json" ]; then
                desktop_src=$KOVA_SOURCE/src/kova/desktop
            elif [ -n "$here" ] && [ -f "$here/src/kova/desktop/package.json" ]; then
                desktop_src=$here/src/kova/desktop
            elif command -v curl >/dev/null 2>&1 && command -v tar >/dev/null 2>&1 &&
                 command -v mktemp >/dev/null 2>&1; then
                tree=$(mktemp -d "${TMPDIR:-/tmp}/kova-desktop.XXXXXX")
                trap 'rm -rf -- "$tree"' 0
                url="https://github.com/wynxo/Kova/archive/$ref.tar.gz"
                say "Downloading the desktop app from $url ..."
                if curl -fsSL "$url" -o "$tree/kova.tar.gz" && mkdir -p "$tree/tree" &&
                   tar -xzf "$tree/kova.tar.gz" -C "$tree/tree"; then
                    for candidate in "$tree/tree"/Kova-*/src/kova/desktop "$tree/tree"/Kova-*/desktop; do
                        if [ -f "$candidate/package.json" ]; then desktop_src=$candidate; break; fi
                    done
                fi
            fi
        fi
        if [ -n "$desktop_src" ]; then
            if [ "$desktop_src" != "$dest" ]; then
                mkdir -p "$dest"
                for item in "$desktop_src"/*; do
                    [ -e "$item" ] || continue
                    name=$(basename -- "$item")
                    # Install dependencies here, never copy checkout build artifacts.
                    case $name in node_modules|dist) continue ;; esac
                    rm -rf -- "${dest:?}/${name:?}"
                    cp -R -- "$item" "$dest/$name"
                done
                say 'Preparing Electron (this may take a few minutes) ...'
                (cd -- "$dest" && npm ci --include=dev) ||
                    { skip_desktop 'Preparing the desktop app (npm ci) failed. Check your connection and rerun this installer.'; return; }
            fi
            if [ "$(uname -s)" = Darwin ]; then
                electron="$dest/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"
            else
                electron="$dest/node_modules/electron/dist/electron"
            fi
            [ -x "$electron" ] || { skip_desktop 'Electron is missing after npm ci. Rerun this installer with Electron downloads enabled.'; return; }
            # Exec and the generated shell launcher must not interpret path metacharacters.
            case "$dest$electron" in *[!a-zA-Z0-9_./\ -]*) skip_desktop 'The desktop app cannot be registered from a path with unusual characters.'; return ;; esac
            {
                say '#!/bin/sh'
                say 'unset ELECTRON_RUN_AS_NODE'
                printf 'export KOVA_HOME="%s"\n' "$data"
                printf 'exec "%s" "%s" --ozone-platform-hint=auto "$@"\n' "$electron" "$dest"
            } > "$dest/launch"
            chmod +x "$dest/launch"
            say "Desktop app files: $dest"
            if [ -n "${tree-}" ]; then rm -rf -- "$tree"; trap - 0; fi
        else
            skip_desktop 'The desktop app files could not be downloaded (curl and tar are needed). Rerun this installer to add it.'
        fi
    }
    if [ "$register_desktop" -eq 1 ]; then prepare_desktop; fi

    say 'To use kova in this terminal, run (or add to your shell profile):'
    # shellcheck disable=SC2016 # Print a copyable shell command, not current values.
    say '  export PATH="$HOME/.local/bin:$PATH"'
    if [ "$register_desktop" -eq 1 ] && [ "$packaged_registration" -eq 0 ]; then
        case "$(uname -s)" in
        Linux)
        [ -f "$dest/assets/icon.png" ] || die 'The desktop app files are incomplete: assets/icon.png is missing.'
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
            printf 'Exec="%s/launch"\n' "$dest"
            say 'Icon=kova'
            say 'Terminal=false'
            say 'Categories=Utility;'
            say 'StartupWMClass=Kova'
        } > "$apps/kova.desktop"
        if command -v update-desktop-database >/dev/null 2>&1; then
            update-desktop-database "$apps" || say 'Note: launcher cache refresh failed; sign out and back in if Kova is missing.'
        fi
        if command -v gtk-update-icon-cache >/dev/null 2>&1; then
            gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" || say 'Note: icon cache refresh failed.'
        fi
        say 'Registered Kova in your application menu.'
        ;;
        Darwin)
        bundle="$HOME/Applications/Kova.app/Contents"
        mkdir -p "$bundle/MacOS" "$bundle/Resources"
        cp "$dest/launch" "$bundle/MacOS/Kova"
        icon="$dest/assets/icon.icns"
        if [ ! -f "$icon" ]; then
            icon="$dest/node_modules/electron/dist/Electron.app/Contents/Resources/electron.icns"
        fi
        cp "$icon" "$bundle/Resources/kova.icns"
        cat > "$bundle/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>io.github.wynxo.kova.launcher</string>
<key>CFBundleName</key><string>Kova</string>
<key>CFBundleDisplayName</key><string>Kova</string>
<key>CFBundleExecutable</key><string>Kova</string>
<key>CFBundleIconFile</key><string>kova.icns</string>
<key>CFBundlePackageType</key><string>APPL</string>
</dict></plist>
PLIST
        lsregister=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
        if [ -x "$lsregister" ]; then "$lsregister" -f "$HOME/Applications/Kova.app" || :; fi
        say 'Registered Kova in Applications and Spotlight.'
        ;;
        *) say 'Note: this platform has no app-menu integration; use kova app or kova web.'; register_desktop=0 ;;
        esac
    fi
    if [ "$register_desktop" -eq 1 ]; then
        say 'Done. Open Kova from your app menu (or Applications / Spotlight) and choose Add provider.'
    else
        say 'Done (without the desktop app). Run: kova setup'
    fi
    say 'Terminal chat: kova setup, then kova'
    say 'Desktop app: kova app    Web UI in your browser: kova web'
}
main "$@"
