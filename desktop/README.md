# Kova desktop

Electron is a thin, sandboxed window over Kova's existing local web app.
Install Node.js 22+ and uv (or the Kova CLI), then run:

```sh
cd desktop
npm ci
npm start
npm test
npm run dist
```

Build on the target OS: Linux produces AppImage/deb, Windows NSIS,
macOS DMG. Artifacts appear in desktop/dist. Signing/notarization requires
your own platform credentials; these development builds are unsigned.
The Python server is a prerequisite, not bundled: packaged apps need an
installed `kova` executable on PATH or ~/.local/bin. Source launches can
use `uv run kova serve` from the repository. Set up models with `kova setup`.

The shell reads KOVA_HOME/config.toml (default ~/.kova), including its port.
It reuses an authenticated server or starts one, logs to server.log, and
exchanges the local token for a one-use /app/session ticket. Only the ticket
fragment reaches the renderer; /app/login sets the existing HttpOnly cookie.
Electron storage and remembered window bounds also live under KOVA_HOME.
No remote pages or subresources are loaded inside Electron. HTTP(S)/mailto
external links go to your system browser. No renderer Node access.

Closing the window hides it to the tray. Open restores it; Quit or Ctrl/Cmd+Q
exits and terminates only a server this shell spawned. A pre-existing server
is left running. Ctrl/Cmd+W hides, Ctrl/Cmd+R reconnects, F11 toggles fullscreen.
Startup failures show Retry and the exact server log path.

The one-line installers register Kova by default: a Linux menu entry and icon
(with cache refresh), a Windows Start-menu shortcut, or a per-user macOS
`~/Applications/Kova.app` launcher registered with Launch Services for Spotlight.
They run `npm ci --include=dev` during installation and launch the prepared
Electron binary directly, without a terminal or npm on first click. Node.js 22+
with npm must be installed first; errors point to https://nodejs.org and explain
how to rerun. `--no-desktop` installs only the backend, without Node/npm.
Uninstallers remove this integration and staged app even with `--keep-data`.

## Release downloads

`.github/workflows/desktop-release.yml` runs on version tags (`vX.Y.Z`, optionally
with a prerelease suffix). It tests and builds on each target OS, then attaches
Linux x64 AppImage/deb, Windows x64 NSIS exe, and macOS arm64/x64 DMG assets to
the tag's GitHub Release. Artifact versions come from the tag. No tag is created
by the workflow. Release upload waits for **all** platform builds to succeed.
See electron-builder's [publishing options](https://www.electron.build/v26/docs/publish/).

Download from https://github.com/wynxo/Kova/releases. Install the backend first
using the README's one-line installer with `--no-desktop` (it supplies uv and
Python 3.12). The shell bundles Electron, **not Python, uv, the backend, or a
model**; Node/npm are unnecessary for downloads. It finds `kova` on PATH or in
`~/.local/bin`, starts `kova serve` or reuses the authenticated running server,
just like `kova-app`. Only checkout launches fall back to `uv run kova serve`.
Missing-backend errors link to the installation instructions and offer Retry.

Use the .deb or NSIS .exe for app/Start-menu integration. Open the .dmg and drag
Kova to Applications for Finder/Spotlight. With AppImage, mark executable and
open from the file manager; AppImage alone does not register a menu entry.
These builds are unsigned and not notarized; no signing secrets are required
by CI. Platform verification prompts are expected until signing is configured.
The OS uninstaller removes downloaded shells; `kova uninstall` independently
removes the Python backend. Both keep chats unless explicitly purged.

Build notes: electron-builder's Linux deb helper needs libcrypt.so.1
(on Fedora, provided by libxcrypt-compat). AppImage builds do not need it.
Windows/macOS installers are built on native CI runners; local validation on one
platform is not evidence of interactive launch on the others.
