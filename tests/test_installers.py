"""Run the POSIX installers with disposable homes and a recording uv executable."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX shell installers")


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
    # Only the commands the script needs; no git or uv leaks from the real PATH.
    for command in ("rm", "dirname"):
        (path / command).symlink_to(shutil.which(command))
    env = {**os.environ, "HOME": str(home), "PATH": str(path)}
    for key in ("SUDO_USER", "SUDO_UID", "KOVA_SOURCE", "KOVA_REF", "KOVA_HOME"):
        env.pop(key, None)

    def run(script, *args, content=None):
        command = ["/bin/sh", str(REPO / script), *args] if content is None else ["/bin/sh"]
        return subprocess.run(command, input=content, capture_output=True, text=True, env=env)

    return home, path, env, run


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
    assert "Done. Run: kova setup" in result.stdout


def test_installer_editable_checkout(installer):
    home, path, env, run = installer
    env["KOVA_SOURCE"] = str(REPO)
    result = run("install.sh", "--dev")
    assert result.returncode == 0, result.stderr
    assert (home / "uv-args").read_text().splitlines()[-2:] == ["--editable", str(REPO)]


@pytest.mark.parametrize("script", ["install.sh", "uninstall.sh"])
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


def test_installer_does_not_claim_success_on_uv_failure(installer):
    home, path, env, run = installer
    env["FAKE_UV_EXIT"] = "1"
    result = run("install.sh")
    assert result.returncode == 1
    assert "Done." not in result.stdout


@pytest.mark.parametrize("purge", [True, False])
def test_standalone_uninstall_without_launcher(installer, purge):
    home, path, env, run = installer
    data = home / ".kova"
    data.mkdir()
    (data / "chat.db").write_text("keep unless purge")
    result = run("uninstall.sh", "--purge" if purge else "--keep-data")
    assert result.returncode == 0, result.stderr
    assert (home / "uv-args").read_text().splitlines() == ["tool", "uninstall", "kova-ai"]
    assert data.exists() is not purge


def test_standalone_uninstall_refuses_purging_home(installer):
    home, path, env, run = installer
    env["KOVA_HOME"] = str(home)
    result = run("uninstall.sh", "--purge")
    assert result.returncode == 1
    assert "Refusing" in result.stderr
    assert not (home / "uv-args").exists()
