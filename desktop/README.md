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

Linux: `./install.sh --dev --desktop` installs the Python CLI, installs
Electron dependencies and registers this checkout in the application menu.
Keep the checkout in place. npm must be on PATH. electron-builder supplies
desktop integration for packaged Linux builds; kova.desktop is the reference.
`./desktop-app --browser` retains the original Python/browser path.
