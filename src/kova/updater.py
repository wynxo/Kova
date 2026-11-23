"""Install, refresh, restart and verify the per-user Kova installation."""

from __future__ import annotations

import base64
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import nullcontext, suppress
from pathlib import Path

import psutil
from filelock import FileLock, Timeout

from kova import autostart, build, desktop_integration, egress, lifecycle, update_runtime as runtime
from kova.client import KovaError
from kova.config import CONFIG_SCHEMA, kova_home, load_config, private_write, save_config
from kova.app import DesktopError, refresh_desktop

FAILURES = (OSError, subprocess.SubprocessError, lifecycle.LifecycleError, DesktopError,
            autostart.AutostartError, KovaError, egress.LocalOnlyError, Timeout, psutil.Error, sqlite3.Error, ValueError)


def _target(ref: str, source: str) -> build.Build | None:
    if not os.environ.get("KOVA_SOURCE"):
        with suppress(lifecycle.LifecycleError):
            return lifecycle.remote_build(ref)
        return None
    version = tomllib.loads((Path(source) / "pyproject.toml").read_text())["project"]["version"]
    commit = None
    if git := shutil.which("git"):
        with suppress(OSError, subprocess.SubprocessError):
            revision = subprocess.check_output([git, "-C", source, "rev-parse", "HEAD"],
                                               text=True, stderr=subprocess.DEVNULL, timeout=3).strip()
            if build.COMMIT.fullmatch(revision):
                commit = revision
    return build.Build(version, commit)


# Electron keeps the web app's service worker and HTTP caches in its profile. A
# worker from the old build can answer the reopened window with old files, so an
# update clears them while the desktop is closed. Cookies and settings stay.
DESKTOP_WEB_CACHES = ("Service Worker", "Cache", "Code Cache")


def clear_desktop_web_cache(home: Path) -> None:
    profile = home / "desktop-data"
    for name in DESKTOP_WEB_CACHES:
        target = profile / name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)


def _command(args, env, verbose, *, label, timeout=300) -> str:
    try:
        result = subprocess.run(args, env=env, check=True, encoding="utf-8", errors="replace",
                                timeout=timeout, stdin=subprocess.DEVNULL, capture_output=not verbose)
    except subprocess.CalledProcessError as exc:
        lines = (exc.stderr or exc.stdout or "").strip().splitlines()
        detail = f": {lines[-1][:240]}" if lines else ""
        raise lifecycle.LifecycleError(f"{label} exited with code {exc.returncode}{detail}") from None
    return result.stdout or ""


def _install(uv, source, target, launcher, env, verbose) -> build.Build:
    """Keep a relocatable copy of the old uv tool until its replacement runs."""
    tool = build.tool_dir()
    backup = Path(tempfile.mkdtemp(dir=tool.parent, prefix=".kova-rollback-"))
    old_launcher = backup / launcher.name
    old_tool = backup / tool.name
    preserve = False
    try:
        if tool.exists():
            shutil.copytree(tool, old_tool, symlinks=True)
        if launcher.is_symlink():
            old_launcher.symlink_to(launcher.readlink())
        elif launcher.is_file():
            shutil.copy2(launcher, old_launcher)
        try:
            # --refresh alone can reinstall the cached commit of a moving ref.
            _command([uv, "tool", "install", "--force", "--refresh", "--upgrade-package", "kova-ai",
                      "--python", "3.12", source], env, verbose, label="uv tool install")
            if os.environ.get("KOVA_SOURCE") and target and target.commit and (dist := build.tool_distribution()):
                (dist / "kova-source-commit").write_text(target.commit)
            installed = build.tool_build()
            if installed is None or not launcher.is_file():
                raise lifecycle.LifecycleError("uv did not produce an installed CLI.")
            reported = _command([launcher, "--version"], env, False, label="kova --version", timeout=15)
            if f"v{installed.version}" not in reported:
                raise lifecycle.LifecycleError("The new CLI did not report its installed version.")
            return installed
        except BaseException:
            try:
                if tool.exists():
                    tool.rename(backup / "failed-tool")
                launcher.unlink(missing_ok=True)
                if old_tool.exists():
                    old_tool.rename(tool)
                if old_launcher.exists() or old_launcher.is_symlink():
                    old_launcher.replace(launcher)
            except OSError as exc:
                preserve = True
                raise lifecycle.LifecycleError(f"Could not restore the old CLI; recovery files: {backup}") from exc
            raise
    finally:
        if not preserve:
            shutil.rmtree(backup, ignore_errors=True)


def stage_desktop(home: Path) -> None:
    """Build and download Electron in a separate directory before swapping it in."""
    home.mkdir(parents=True, exist_ok=True)
    project = home / "desktop"
    with FileLock(home / ".desktop-install.lock", timeout=300):
        staging = Path(tempfile.mkdtemp(dir=home, prefix=".desktop-update-"))
        previous = staging / "previous"
        preserve = False
        try:
            candidate = refresh_desktop(staging)
            if (project / "launch").is_file():
                shutil.copy2(project / "launch", candidate / "launch")
            if project.exists():
                project.rename(previous)
            try:
                candidate.rename(project)
            except BaseException:
                if previous.exists():
                    try:
                        previous.rename(project)
                    except OSError as exc:
                        preserve = True
                        raise DesktopError(f"Could not restore the old desktop; recovery files: {previous}") from exc
                raise
        finally:
            if not preserve:
                shutil.rmtree(staging, ignore_errors=True)


def _migrate(home: Path) -> None:
    """Run the installed config/store migrations without starting a server."""
    path = home / "config.toml"
    if path.is_file():
        schema = tomllib.loads(path.read_text()).get("schema_version", 0)
        if schema < CONFIG_SCHEMA:
            save_config(load_config(home))
    database = home / "kova.db"
    if database.is_file():
        from kova.store import Store
        Store(database)


def _login_enabled(state) -> bool:
    if state.enabled:
        return True
    if not state.stale:
        return False
    if state.mechanism == "systemd":
        return autostart._systemctl(None, "is-enabled", autostart.SYSTEMD_UNIT) == 0
    # An existing Run value, LaunchAgent or fallback entry may point at an
    # earlier installation without having been disabled.
    return True


def _details(exc: Exception) -> str:
    if isinstance(exc, (lifecycle.LifecycleError, DesktopError, autostart.AutostartError, KovaError, egress.LocalOnlyError)):
        return str(exc)
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timed out"
    return str(exc).splitlines()[0][:240] if str(exc) else type(exc).__name__


def update_installation(ref: str, emit=print, *, verbose=False, progress=None, worker=False) -> None:
    home = kova_home().resolve()
    if not os.environ.get("KOVA_HOME"):
        if registered_home := desktop_integration.registered_linux_home(desktop_integration.menu_path()):
            home = registered_home.resolve()
    # Fail before looking up GitHub, invoking any helpers, or changing files.
    with egress.scope(config_loader=lambda: load_config(home, read_only=True), audit=False):
        egress.check_external_process()
    if sys.platform == "win32" and build.managed() and not worker:
        _defer_windows(ref, verbose)
        emit("→ Update started in a separate console so Windows can replace the running CLI.")
        return

    progress = progress or (lambda _: nullcontext())
    env = lifecycle.tool_env()
    env["KOVA_HOME"] = str(home)
    env.pop("PYTHONPATH", None)
    launcher = Path(env["UV_TOOL_BIN_DIR"]) / ("kova.exe" if os.name == "nt" else "kova")
    tool_root = build.tool_dir().parent
    tool_root.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(tool_root / ".kova-update.lock", timeout=0):
            _update_locked(ref, home, launcher, env, emit, verbose, progress)
    except Timeout:
        raise lifecycle.LifecycleError("Another Kova update is already running.") from None


def _update_locked(ref, home, launcher, env, emit, verbose, progress) -> None:
    results = []
    retry = f"kova update --ref {ref} --verbose"

    def step(name, action, repair=retry):
        emit(f"→ {name}")
        try:
            with progress(name):
                value = action()
        except FAILURES as exc:
            message = _details(exc)
            results.append((name, False, message, repair))
            emit(f"✗ {name}: {message}")
            return None
        results.append((name, True, "", ""))
        emit(f"✓ {name}")
        return value if value is not None else True

    def skipped(name, reason):
        results.append((name, None, reason, ""))
        emit(f"– {name}: {reason}")

    current = build.tool_build() or build.installed()
    source = lifecycle.source_spec(ref)
    emit("→ Looking up the target build")
    with progress("Looking up the target build"):
        try:
            target = _target(ref, source)
        except FAILURES:
            target = None
    if target:
        emit(f"Kova {current.label()} → {target.label()}")
        if not os.environ.get("KOVA_SOURCE"):
            with progress("Fetching changelog"):
                for subject in lifecycle.changelog(current.commit, target.commit):
                    emit(f"  • {subject}")
            # Use the revision we showed. If GitHub is unavailable, uv still
            # re-resolves the moving ref as before.
            if target.commit:
                if source.startswith("kova-ai @ git+"):
                    source = f"kova-ai @ git+{lifecycle.REPOSITORY}@{target.commit}"
                else:
                    source = f"kova-ai @ {lifecycle.REPOSITORY}/archive/{target.commit}.tar.gz"
    else:
        emit(f"Kova {current.label()} → {ref} (details unavailable; continuing)")

    cfg = step("Read runtime settings", lambda: load_config(home, read_only=True), "kova doctor")
    server = step("Detect running server", lambda: (runtime.running_server(cfg),), "kova status") if cfg else None
    server = server[0] if server else None
    desktop = step("Detect running desktop", lambda: (runtime.running_desktop(home),), "kova app")
    desktop = desktop[0] if desktop else None
    login = step("Read start-at-login state", lambda: autostart.status(kova_home=home), "kova autostart status")
    menu = desktop_integration.menu_path()
    wants_desktop = desktop or (home / "desktop").exists() or (home / "desktop-data").exists() or (menu and menu.exists())

    # Windows locks the old Electron files and DLLs while the processes run.
    # On Unix leave both running until we know the new package works.
    stopped = {}

    def stop_running(kind, process):
        if process:
            stopped[kind] = bool(step(f"Stop old {kind}", lambda: runtime.stop(process)))

    def install():
        uv = lifecycle.find_uv()
        if sys.platform == "win32":
            stop_running("server", server)
            stop_running("desktop", desktop)
            if False in stopped.values():
                raise lifecycle.LifecycleError("Close the old Kova processes before updating on Windows.")
            _check_windows_tool_idle()
        return _install(uv, source, target, launcher, env, verbose)

    installed = step("Install and verify CLI, terminal UI and web assets", install)
    if installed:
        emit(f"✓ Installed Kova {current.label()} → {installed.label()}")
        if sys.platform != "win32":
            stop_running("server", server)
            stop_running("desktop", desktop)

        def new_python(action, *, detail, timeout=300):
            _command([build.tool_python(), "-m", "kova.updater", action],
                     env, verbose, label=detail, timeout=timeout)

        if not server or stopped.get("server"):
            step("Migrate config and data", lambda: new_python("migrate", detail="Config/data migration"))
        else:
            skipped("Migrate config and data", "old server is still active")

        if wants_desktop and (not desktop or stopped.get("desktop")):
            staged = step("Re-stage Electron files and dependencies",
                          lambda: new_python("stage", detail="Electron preparation", timeout=600))
            if staged:
                step("Refresh app-menu entry, icons and caches",
                     lambda: new_python("register", detail="Desktop integration"))
            else:
                skipped("Refresh app-menu entry, icons and caches", "previous desktop kept")
            step("Clear the desktop app's cached web files", lambda: clear_desktop_web_cache(home))
        else:
            skipped("Re-stage Electron and refresh app menu", "old app is still active" if desktop else "desktop not installed")

        enabled = step("Check whether start-at-login is enabled", lambda: (_login_enabled(login),),
                       "kova autostart status") if login else None
        if enabled and enabled[0]:
            step("Refresh start-at-login entry", lambda: new_python("login", detail="Start-at-login refresh"),
                 "kova autostart on")
        else:
            skipped("Refresh start-at-login entry", "not enabled" if enabled else "state unavailable")
    else:
        skipped("Migrate data and refresh desktop integration", "CLI install failed; do not change the previous install")

    if stopped.get("server"):
        def start():
            return runtime.start_server(load_config(home), installed or current,
                                        start=_managed_start(login))
        step("Restart server and wait for /health", start, "kova restart")
    elif not server:
        skipped("Restart server", "not running before the update")

    if stopped.get("desktop"):
        step("Reopen desktop", lambda: runtime.start_desktop(home, launcher), "kova app")
    elif not desktop:
        skipped("Reopen desktop", "not running before the update")

    if installed and server:
        step("Verify running server build", lambda: runtime.verify_server(load_config(home, read_only=True), installed),
             "kova restart")
    if installed and wants_desktop:
        step("Verify desktop launch entry and icons", lambda: new_python("verify-desktop", detail="Desktop verification"))

    emit("\nUpdate summary")
    for name, ok, detail, repair in results:
        symbol = "✓" if ok else "✗" if ok is False else "–"
        emit(f"{symbol} {name}" + (f": {detail}" if detail else "") + (f" → Run: {repair}" if repair else ""))
    if any(ok is False for _, ok, _, _ in results):
        raise lifecycle.LifecycleError("Update finished with errors; see the repair commands above.")


def _managed_start(login):
    if not login or not login.running:
        return None
    if login.mechanism == "systemd":
        def start():
            if autostart._systemctl(None, "start", autostart.SYSTEMD_UNIT):
                raise lifecycle.LifecycleError("systemd could not start the Kova user service.")
        return start
    if login.mechanism == "launchagent":
        def start():
            if autostart._launchctl(None, "kickstart", f"gui/{os.getuid()}/{autostart.LABEL}"):
                raise lifecycle.LifecycleError("LaunchServices could not start the Kova LaunchAgent.")
        return start
    return None


def _check_windows_tool_idle() -> None:
    tool = build.tool_dir().resolve()
    for process in psutil.process_iter(["exe", "cmdline"]):
        for arg in [process.info["exe"], *(process.info["cmdline"] or [])[:2]]:
            if arg and Path(arg).is_absolute() and Path(arg).is_relative_to(tool):
                raise lifecycle.LifecycleError("Close other Kova terminal sessions before updating on Windows.")


def _defer_windows(ref, verbose):
    # The console shim and environment DLLs can be in use even after uv has
    # fetched the replacement. Start using a copy outside the tool env first.
    dist = build.tool_distribution()
    if dist is None:
        raise lifecycle.LifecycleError("Could not locate the current Kova tool environment.")
    worker = Path(tempfile.mkdtemp(prefix="kova-update-"))
    packages = worker / "packages"
    try:
        shutil.copytree(dist.parent, packages)
        parents = [str(os.getpid())]
        parent = psutil.Process().parent()
        if parent and Path(parent.name()).name.lower() == "kova.exe":
            parents.append(str(parent.pid))
        def quote(value):
            return "'" + str(value).replace("'", "''") + "'"
        bootstrap = ("import runpy, site, sys; site.addsitedir(sys.argv.pop(1)); "
                     "runpy.run_module('kova.updater', run_name='__main__')")
        script = (f"try {{ & {quote(sys._base_executable)} -I -c {quote(bootstrap)} "
                  f"{quote(packages)} worker {quote(ref)} {quote(','.join(parents))} {int(verbose)}; "
                  "$code = $LASTEXITCODE } "
                  f"finally {{ Remove-Item -LiteralPath {quote(worker)} -Recurse -Force }}; "
                  "$null = Read-Host 'Update finished. Press Enter to close'; exit $code")
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        subprocess.Popen(["powershell.exe", "-NoProfile", "-EncodedCommand", encoded],
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
    except BaseException:
        shutil.rmtree(worker, ignore_errors=True)
        raise


def main() -> None:
    action, *args = sys.argv[1:]
    try:
        if action == "worker":
            ref, pids, verbose = args
            os.environ.pop("PYTHONPATH", None)  # children must import the new install
            for pid in pids.split(","):
                with suppress(psutil.NoSuchProcess):
                    psutil.Process(int(pid)).wait(timeout=20)
            from kova.cli import console, _lifecycle_emit
            update_installation(ref, _lifecycle_emit, verbose=verbose == "1", worker=True,
                                progress=None if verbose == "1" else console.status)
            return
        home = kova_home().resolve()
        env = lifecycle.tool_env()
        launcher = Path(env["UV_TOOL_BIN_DIR"]) / ("kova.exe" if os.name == "nt" else "kova")
        if action == "migrate":
            _migrate(home)
        elif action == "stage":
            stage_desktop(home)
        elif action in ("register", "login"):
            desktop_integration.refresh_desktop_integration(home, launcher, login=action == "login")
        elif action == "verify-desktop":
            desktop_integration.verify_desktop_integration(home, launcher)
    except FAILURES as exc:
        print(_details(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
