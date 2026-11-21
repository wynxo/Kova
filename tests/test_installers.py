"""Run the installers and checkout updaters with disposable homes and recording uv executables.

The POSIX half needs ``/bin/sh``; the Windows half needs PowerShell 7 and the
Windows registry provider. Each half skips on the platform where it cannot run,
and never falls back to a real ``uv`` or a real home.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

requires_posix = pytest.mark.skipif(os.name == "nt", reason="POSIX shell installers")
requires_windows = pytest.mark.skipif(
    os.name != "nt", reason="Windows installers need PowerShell and the Windows registry"
)


@pytest.fixture
def installer(tmp_path):
    home = tmp_path / "home"
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True)
    uv = bin_dir / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$HOME/uv-args"\n'
                  'printf "%s\\n" "$UV_TOOL_BIN_DIR" "$UV_TOOL_DIR" > "$HOME/uv-paths"\n'
                  'exit "${FAKE_UV_EXIT:-0}"\n')
    uv.chmod(0o755)
    path = tmp_path / "path"
    path.mkdir()
    fake_id = path / "id"
    fake_id.write_text('#!/bin/sh\nprintf "%s\\n" "${FAKE_UID:-1000}"\n')
    fake_id.chmod(0o755)
    # Only the commands the scripts need; no git, curl or uv leaks from the real PATH.
    for command in ("rm", "dirname", "mkdir", "cp", "basename", "uname", "chmod", "cat"):
        (path / command).symlink_to(shutil.which(command))
    node = path / "node"
    node.write_text('#!/bin/sh\nexit "${FAKE_NODE_EXIT:-0}"\n')
    node.chmod(0o755)
    npm = path / "npm"
    npm.write_text("""#!/bin/sh
printf '%s\\n' "$@" > "$HOME/npm-args"
mkdir -p node_modules/electron/dist
cat > node_modules/electron/dist/electron <<'ELECTRON'
#!/bin/sh
printf '%s\\n' "$KOVA_HOME" > "$HOME/launched"
ELECTRON
chmod +x node_modules/electron/dist/electron
mkdir -p node_modules/electron/dist/Electron.app/Contents/MacOS node_modules/electron/dist/Electron.app/Contents/Resources
cp node_modules/electron/dist/electron node_modules/electron/dist/Electron.app/Contents/MacOS/Electron
printf icon > node_modules/electron/dist/Electron.app/Contents/Resources/electron.icns
exit "${FAKE_NPM_EXIT:-0}"
""")
    npm.chmod(0o755)
    env = {**os.environ, "HOME": str(home), "PATH": str(path)}
    for key in ("SUDO_USER", "SUDO_UID", "KOVA_SOURCE", "KOVA_REF", "KOVA_HOME"):
        env.pop(key, None)

    def run(script, *args, content=None, cwd=None):
        command = ["/bin/sh", str(REPO / script), *args] if content is None else ["/bin/sh"]
        return subprocess.run(command, input=content, capture_output=True, text=True, env=env, cwd=cwd)

    return home, path, env, run


@requires_posix
@pytest.mark.parametrize("git", [True, False])
def test_installer_ref_and_no_git_archive(installer, git):
    home, path, env, run = installer
    env["KOVA_REF"] = "ignored-env-ref"
    if git:
        (path / "git").symlink_to(shutil.which("true"))
    result = run("install.sh", "--ref", "v0.2.0")
    assert result.returncode == 0, result.stderr
    source = ("kova-ai @ git+https://github.com/wynxo/Kova@v0.2.0" if git else
              "kova-ai @ https://github.com/wynxo/Kova/archive/v0.2.0.tar.gz")
    assert (home / "uv-args").read_text().splitlines() == [
        "tool", "install", "--force", "--refresh", "--python", "3.12", source,
    ]
    assert (home / "uv-paths").read_text().splitlines() == [
        str(home / ".local/bin"), str(home / ".local/share/uv/tools"),
    ]
    assert "Done. Open Kova" in result.stdout


@requires_posix
def test_installer_editable_checkout(installer):
    home, path, env, run = installer
    env["KOVA_SOURCE"] = str(REPO)
    result = run("install.sh", "--dev")
    assert result.returncode == 0, result.stderr
    assert (home / "uv-args").read_text().splitlines()[-2:] == ["--editable", str(REPO)]


@requires_posix
def test_installer_stages_the_desktop_app_and_names_both_clients(installer):
    home, path, env, run = installer
    result = run("install.sh")
    assert result.returncode == 0, result.stderr
    staged = home / ".kova/desktop"
    assert (staged / "package.json").is_file()
    assert (staged / "main.js").is_file()
    assert (staged / "node_modules/electron/dist/electron").is_file()
    assert (home / "npm-args").read_text().splitlines() == ["ci", "--include=dev"]
    assert "Desktop app: kova-app" in result.stdout
    assert "Web UI in your browser: kova-web" in result.stdout


@requires_posix
def test_cli_only_piped_install_needs_no_download_tools(installer, tmp_path):
    home, path, env, run = installer
    # Piped from a directory that is not a checkout, with no curl/tar/mktemp on PATH.
    result = run("install.sh", content=(REPO / "install.sh").read_text().replace('main "$@"', 'main --no-desktop'), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert not (home / ".kova/desktop").exists()
    assert "without the desktop app" in result.stdout
    assert "Web UI in your browser: kova-web" in result.stdout


@requires_posix
@pytest.mark.skipif(sys.platform != "linux", reason="Linux desktop registration")
def test_installer_registers_the_installed_desktop_command(installer):
    home, path, env, run = installer
    result = run("install.sh")
    assert result.returncode == 0, result.stderr
    entry = (home / ".local/share/applications/kova.desktop").read_text()
    assert f'Exec="{home}/.kova/desktop/launch"' in entry
    launched = subprocess.run([str(home / ".kova/desktop/launch")], env={**env, "PATH": "/usr/bin:/bin"})
    assert launched.returncode == 0
    assert (home / "launched").read_text().strip() == str(home / ".kova")
    assert (home / ".local/share/icons/hicolor/512x512/apps/kova.png").is_file()


@requires_posix
@pytest.mark.parametrize("script", ["install.sh", "scripts/uninstall.sh"])
@pytest.mark.parametrize("context", ["root", "sudo", "truncated"])
def test_installer_does_not_run_in_unsafe_context(installer, script, context):
    home, path, env, run = installer
    if context == "root":
        env["FAKE_UID"] = "0"
    elif context == "sudo":
        env["SUDO_UID"] = "1000"
    content = (REPO / script).read_text().rsplit('main "$@"', 1)[0] if context == "truncated" else None
    result = run(script, content=content)
    assert result.returncode == (0 if context == "truncated" else 1)
    assert not (home / "uv-args").exists()


@requires_posix
def test_installer_does_not_claim_success_on_uv_failure(installer):
    home, path, env, run = installer
    env["FAKE_UV_EXIT"] = "1"
    result = run("install.sh")
    assert result.returncode == 1
    assert "Done." not in result.stdout


@requires_posix
@pytest.mark.parametrize("purge", [True, False])
def test_standalone_uninstall_without_launcher(installer, purge):
    home, path, env, run = installer
    data = home / ".kova"
    data.mkdir()
    (data / "chat.db").write_text("keep unless purge")
    result = run("scripts/uninstall.sh", "--purge" if purge else "--keep-data")
    assert result.returncode == 0, result.stderr
    assert (home / "uv-args").read_text().splitlines() == ["tool", "uninstall", "kova-ai"]
    assert data.exists() is not purge


@requires_posix
def test_standalone_uninstall_removes_the_staged_desktop_app(installer):
    home, path, env, run = installer
    data = home / ".kova"
    staged = data / "desktop"
    (staged / "node_modules").mkdir(parents=True)
    (staged / "package.json").write_text("{}")
    (data / "chat.db").write_text("chats")
    result = run("scripts/uninstall.sh", "--keep-data")
    assert result.returncode == 0, result.stderr
    assert (data / "chat.db").is_file()
    assert not staged.exists()
    assert "Removed the desktop app files" in result.stdout


@requires_posix
def test_standalone_uninstall_refuses_purging_home(installer):
    home, path, env, run = installer
    env["KOVA_HOME"] = str(home)
    result = run("scripts/uninstall.sh", "--purge")
    assert result.returncode == 1
    assert "Refusing" in result.stderr
    assert not (home / "uv-args").exists()


# Windows: install.ps1, uninstall.ps1 and the .bat wrappers.
#
# GitHub's windows-latest image runs elevated with UAC disabled, and both
# installers deliberately refuse that ("Run as your normal user"). The tests
# therefore (a) assert that refusal when the current shell is elevated and
# (b) exercise everything after the guard from a copy whose only difference is
# that one throw. On a normal-user shell the checkout's scripts run unmodified.
# --------------------------------------------------------------------------

ADMIN_GUARD = "throw 'Run as your normal user, not in an Administrator terminal.'"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Kova"


def _elevated():
    """True when this process holds an administrator token (not merely an admin user)."""
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def _runnable_script(script, workdir, raw=False):
    """The checkout's script, or a copy with only the administrator guard relaxed."""
    source = REPO / script
    if raw or not _elevated():
        return source
    text = source.read_text(encoding="utf-8")
    if text.count(ADMIN_GUARD) != 1:
        raise AssertionError(f"{script} no longer contains the administrator guard this test relaxes")
    copy = workdir / script
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(
        text.replace(ADMIN_GUARD, "Write-Host 'test: administrator guard relaxed.'"),
        encoding="utf-8",
    )
    return copy


def _read_user_path():
    """The persisted current-user PATH (HKCU\\Environment) as (value, kind), or None."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        try:
            return winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return None


def _restore_user_path(previous):
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        if previous is None:
            try:
                winreg.DeleteValue(key, "Path")
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, "Path", 0, previous[1], previous[0])


def _user_path_entries():
    stored = _read_user_path()
    value = "" if stored is None else (stored[0] or "")
    return [entry.rstrip("\\") for entry in value.split(";") if entry]


def _read_uninstall_key():
    """Every value of the Kova uninstall key as {name: (value, kind)}, or None."""
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except FileNotFoundError:
        return None
    values = {}
    with key:
        for index in range(64):
            try:
                name, value, kind = winreg.EnumValue(key, index)
            except OSError:
                break
            values[name] = (value, kind)
    return values


def _restore_uninstall_key(previous):
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except FileNotFoundError:
        pass
    if previous is None:
        return
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        for name, (value, kind) in previous.items():
            winreg.SetValueEx(key, name, 0, kind, value)


def _write_console_stub(dest):
    """Stand in for the installed ``kova.exe``: the installer runs ``& $Kova --version``.

    Any console program that prints and exits 0 will do; curl.exe ships with Windows.
    """
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
    candidate = str(system) if system.is_file() else shutil.which("curl.exe")
    if not candidate:
        pytest.fail("could not find curl.exe to stand in for the installed kova.exe")
    shutil.copy2(candidate, dest)


def _write_uv_stub(stub):
    """A ``uv`` on PATH that records its arguments and never installs anything."""
    (stub / "fake_uv.ps1").write_text(
        "Set-Content -LiteralPath $env:KOVA_TEST_UV_LOG -Value @($args) -Encoding utf8\n"
        "Add-Content -LiteralPath $env:KOVA_TEST_UV_LOG -Value $env:UV_TOOL_BIN_DIR -Encoding utf8\n"
        "Add-Content -LiteralPath $env:KOVA_TEST_UV_LOG -Value $env:UV_TOOL_DIR -Encoding utf8\n"
        "$code = 0\n"
        "if ($env:FAKE_UV_EXIT) { $code = [int]$env:FAKE_UV_EXIT }\n"
        "exit $code\n",
        encoding="utf-8",
    )
    (stub / "uv.cmd").write_bytes(
        b"@echo off\r\n"
        b'pwsh -NoProfile -File "%~dp0fake_uv.ps1" %*\r\n'
        b"exit /b %errorlevel%\r\n"
    )


def _write_git_stub(stub):
    """A ``git`` on PATH that records any use; the tests expect none."""
    (stub / "git.cmd").write_bytes(
        b"@echo off\r\n"
        b'>>"%KOVA_TEST_GIT_LOG%" echo %*\r\n'
        b"exit /b 0\r\n"
    )


@pytest.fixture
def windows_installer(tmp_path):
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip(
            "pwsh (PowerShell 7) is not installed, so install.ps1 and uninstall.ps1 "
            "cannot be exercised here; GitHub's windows-latest image ships pwsh"
        )
    home = tmp_path / "home"
    home.mkdir()
    (home / ".local" / "bin").mkdir(parents=True)
    stub = tmp_path / "path"
    stub.mkdir()
    _write_console_stub(home / ".local" / "bin" / "kova.exe")
    _write_uv_stub(stub)
    _write_git_stub(stub)
    (stub / "node.cmd").write_text('@exit /b 0\n')
    (stub / "npm.cmd").write_text(
        '@echo off\n'
        'echo %* > "%USERPROFILE%\\npm-args"\n'
        'mkdir node_modules\\electron\\dist 2>nul\n'
        'copy /y "%USERPROFILE%\\.local\\bin\\kova.exe" node_modules\\electron\\dist\\electron.exe >nul\n'
        'exit /b 0\n'
    )
    env = {
        **os.environ,
        # The installer reads USERPROFILE; HOME is set too so the test mirrors the POSIX one.
        "APPDATA": str(home / "AppData/Roaming"),
        "USERPROFILE": str(home),
        "HOME": str(home),
        "KOVA_SOURCE": str(REPO),
        "KOVA_TEST_UV_LOG": str(home / "uv-args"),
        "KOVA_TEST_GIT_LOG": str(tmp_path / "git-called"),
        "PATH": os.pathsep.join([str(stub), os.environ.get("PATH", "")]),
    }
    for key in (
        "KOVA_HOME", "KOVA_REF", "FAKE_UV_EXIT",
        "UV_TOOL_BIN_DIR", "UV_TOOL_DIR", "UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR",
    ):
        env.pop(key, None)

    def run(script, *args, raw=False):
        script_path = _runnable_script(script, tmp_path, raw=raw)
        command = [
            pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path), *args,
        ]
        return subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, cwd=tmp_path,
        )

    # The installers persist a user PATH entry and a Settings > Apps key: snapshot the
    # real ones so a test run leaves the machine exactly as it found it.
    path_before = _read_user_path()
    key_before = _read_uninstall_key()
    try:
        yield home, stub, env, run
    finally:
        _restore_user_path(path_before)
        _restore_uninstall_key(key_before)


@requires_windows
def test_windows_installer_stages_the_desktop_app_path_and_uninstall_entry(windows_installer):
    home, stub, env, run = windows_installer
    result = run("install.ps1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Done. Open Kova" in result.stdout
    assert "Downloading" not in result.stdout  # KOVA_SOURCE and a stub git: no network
    assert "Desktop app: kova-app    Web UI in your browser: kova-web" in result.stdout

    staged = home / ".kova" / "desktop"
    assert (staged / "package.json").is_file()
    assert (staged / "main.js").is_file()
    assert (staged / "node_modules/electron/dist/electron.exe").is_file()
    shortcut = home / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Kova.lnk"
    assert shortcut.is_file()
    assert (home / "npm-args").read_text().strip() == "ci --include=dev"
    assert (home / ".local" / "share" / "kova" / "uninstall.ps1").is_file()

    args = (home / "uv-args").read_text(encoding="utf-8").splitlines()
    assert args[:6] == ["tool", "install", "--force", "--refresh", "--python", "3.12"]
    assert Path(args[6]) == REPO
    assert args[7:] == [str(home / ".local" / "bin"), str(home / ".local" / "share" / "uv" / "tools")]
    assert not Path(env["KOVA_TEST_GIT_LOG"]).exists()

    bin_dir = str(home / ".local" / "bin").rstrip("\\")
    assert bin_dir in _user_path_entries()

    values = _read_uninstall_key()
    assert values is not None
    assert values["DisplayName"][0] == "Kova"
    assert values["Publisher"][0] == "wynxo"
    assert values["InstallLocation"][0] == str(home / ".local" / "share" / "uv" / "tools" / "kova-ai")
    assert str(home / ".local" / "share" / "kova" / "uninstall.ps1") in values["UninstallString"][0]
    assert values["NoModify"][0] == 1


@requires_windows
def test_windows_installer_does_not_claim_success_on_uv_failure(windows_installer):
    home, stub, env, run = windows_installer
    path_before = _read_user_path()
    key_before = _read_uninstall_key()
    env["FAKE_UV_EXIT"] = "1"
    result = run("install.ps1")
    assert result.returncode != 0
    assert "Done." not in result.stdout
    assert "uv tool install failed" in result.stdout + result.stderr
    assert not (home / ".kova" / "desktop").exists()
    assert _read_user_path() == path_before
    assert _read_uninstall_key() == key_before


@requires_windows
@pytest.mark.parametrize("purge", [False, True])
def test_windows_uninstall_keeps_or_purges_user_data(windows_installer, purge):
    home, stub, env, run = windows_installer
    data = home / ".kova"
    staged = data / "desktop"
    (staged / "node_modules").mkdir(parents=True)
    (staged / "package.json").write_text("{}", encoding="utf-8")
    (data / "chat.db").write_text("chats", encoding="utf-8")
    saved = home / ".local" / "share" / "kova" / "uninstall.ps1"
    saved.parent.mkdir(parents=True)
    saved.write_text("# saved uninstaller\n", encoding="utf-8")

    result = run("scripts/uninstall.ps1", "--purge" if purge else "--keep-data")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (home / "uv-args").read_text(encoding="utf-8").splitlines()[:3] == [
        "tool", "uninstall", "kova-ai",
    ]
    assert not staged.exists()
    assert data.exists() is not purge
    assert not saved.exists()
    assert _read_uninstall_key() is None
    assert "Removed the desktop app files" in result.stdout
    assert ("Deleted" if purge else "Keeping your data") in result.stdout


@requires_windows
def test_windows_uninstaller_refuses_purging_the_home_folder(windows_installer):
    home, stub, env, run = windows_installer
    env["KOVA_HOME"] = str(home)
    result = run("scripts/uninstall.ps1", "--purge")
    assert result.returncode != 0
    assert "Refusing" in result.stdout + result.stderr
    assert not (home / "uv-args").exists()


@requires_windows
def test_windows_installers_refuse_an_administrator_shell(windows_installer):
    home, stub, env, run = windows_installer
    if not _elevated():
        pytest.skip("this shell is already a normal user; there is nothing to refuse")
    for script in ("install.ps1", "scripts/uninstall.ps1"):
        result = run(script, raw=True)
        assert result.returncode != 0, result.stdout
        assert "Run as your normal user" in result.stdout + result.stderr
    assert not (home / "uv-args").exists()


@requires_windows
@pytest.mark.parametrize("script", ["scripts/install.bat", "scripts/uninstall.bat"])
def test_windows_batch_wrappers_forward_arguments_and_exit_codes(windows_installer, script):
    home, stub, env, run = windows_installer
    # --help leaves the scripts early, so the wrappers run as the real install would.
    command = [os.environ.get("COMSPEC", "cmd.exe"), "/c", str(Path(script)), "--help"]
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=REPO,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Usage" in result.stdout
    command[-1] = "--not-an-option"
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=REPO,
    )
    assert result.returncode != 0
    assert not (home / ".local" / "share" / "kova" / "uninstall.ps1").exists()


@requires_posix
@pytest.mark.parametrize("missing", ["node", "npm", "old-node", "npm-failure"])
def test_desktop_failure_still_finishes_the_cli_install(installer, missing):
    home, path, env, run = installer
    if missing in ("node", "npm"):
        (path / missing).unlink()
    elif missing == "old-node":
        env["FAKE_NODE_EXIT"] = "1"
    else:
        env["FAKE_NPM_EXIT"] = "1"
    result = run("install.sh")
    # The desktop app is optional: the CLI install finishes with a clear note.
    assert result.returncode == 0, result.stderr
    assert "Done (without the desktop app)" in result.stdout
    expected = "npm ci" if missing == "npm-failure" else "nodejs.org"
    assert expected in result.stdout
    assert "kova-web" in result.stdout
    assert not (home / ".local/share/applications/kova.desktop").exists()


@requires_posix
def test_cli_only_install_needs_no_node_or_menu_entry(installer):
    home, path, env, run = installer
    (path / "node").unlink()
    (path / "npm").unlink()
    result = run("install.sh", "--no-desktop")
    assert result.returncode == 0, result.stderr
    assert not (home / ".local/share/applications/kova.desktop").exists()
    assert not (home / "npm-args").exists()


@requires_posix
@pytest.mark.skipif(sys.platform != "linux", reason="Linux launcher caches")
def test_installer_refreshes_caches_and_uninstaller_removes_registration(installer):
    home, path, env, run = installer
    for name in ("update-desktop-database", "gtk-update-icon-cache"):
        tool = path / name
        tool.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$HOME/' + name + '"\n')
        tool.chmod(0o755)
    assert run("install.sh").returncode == 0
    assert (home / "update-desktop-database").exists()
    assert (home / "gtk-update-icon-cache").exists()
    assert run("scripts/uninstall.sh", "--keep-data").returncode == 0
    assert not (home / ".local/share/applications/kova.desktop").exists()
    assert not (home / ".local/share/icons/hicolor/512x512/apps/kova.png").exists()


@requires_windows
def test_windows_cli_only_skips_dependencies_and_shortcut(windows_installer):
    home, stub, env, run = windows_installer
    result = run("install.ps1", "--no-desktop")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (home / "npm-args").exists()
    assert not (home / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Kova.lnk").exists()


@requires_windows
def test_windows_uninstall_removes_start_menu_shortcut(windows_installer):
    home, stub, env, run = windows_installer
    shortcut = home / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Kova.lnk"
    shortcut.parent.mkdir(parents=True)
    shortcut.write_text("shortcut")
    result = run("scripts/uninstall.ps1", "--keep-data")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not shortcut.exists()


@requires_posix
def test_macos_installer_registers_and_removes_spotlight_app(installer):
    import plistlib

    home, path, env, run = installer
    (path / "uname").unlink()
    (path / "uname").write_text('#!/bin/sh\nprintf Darwin\n')
    (path / "uname").chmod(0o755)
    result = run("install.sh")
    assert result.returncode == 0, result.stderr
    bundle = home / "Applications/Kova.app/Contents"
    info = plistlib.loads((bundle / "Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "Kova"
    assert (bundle / "MacOS/Kova").stat().st_mode & 0o111
    launched = subprocess.run([str(bundle / "MacOS/Kova")], env=env)
    assert launched.returncode == 0
    assert (home / "launched").read_text().strip() == str(home / ".kova")
    result = run("scripts/uninstall.sh", "--keep-data")
    assert result.returncode == 0, result.stderr
    assert not bundle.exists()


@requires_posix
def test_piped_install_keeps_downloaded_source_until_staging_finishes(installer, tmp_path):
    import tarfile

    home, path, env, run = installer
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in ("package.json", "package-lock.json", "main.js", "assets/icon.png"):
            tar.add(REPO / "src/kova/desktop" / name, arcname="Kova-main/desktop/" + name)
    env["KOVA_TEST_ARCHIVE"] = str(archive)
    for command in ("tar", "gzip", "mktemp"):
        (path / command).symlink_to(shutil.which(command))
    curl = path / "curl"
    curl.write_text('#!/bin/sh\ncp "$KOVA_TEST_ARCHIVE" "$4"\n')
    curl.chmod(0o755)
    result = run("install.sh", content=(REPO / "install.sh").read_text(), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert (home / ".kova/desktop/main.js").is_file()
    assert (home / "npm-args").is_file()


@requires_windows
@pytest.mark.parametrize("failure", ["old-node", "npm-failure"])
def test_windows_desktop_failure_still_finishes_the_cli_install(windows_installer, failure):
    home, stub, env, run = windows_installer
    if failure == "old-node":
        (stub / "node.cmd").write_text('@exit /b 1\n')
    else:
        (stub / "npm.cmd").write_text('@exit /b 1\n')
    result = run("install.ps1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Done (without the desktop app)" in result.stdout
    expected = "nodejs.org" if failure == "old-node" else "npm ci failed"
    assert expected in result.stdout + result.stderr
    assert not (home / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Kova.lnk").exists()
