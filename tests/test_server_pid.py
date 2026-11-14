import pytest
import subprocess
import sys

from kova.lifecycle import stop_server


def test_stale_pid_does_not_kill_unrelated_process(tmp_path):
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        (tmp_path / 'server.pid').write_text(str(process.pid))
        stop_server(tmp_path)
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0.1)
        assert not (tmp_path / 'server.pid').exists()
    finally:
        process.terminate()
        process.wait()


@pytest.mark.parametrize('args,owner,expected', [
    (['python', '-m', 'kova', 'serve'], 'same', True),
    (['/bin/kova', 'serve'], 'same', True),
    (['python', '-m', 'kova', 'serve'], 'other', False),
    (['python', '-c', 'kova serve'], 'same', False),
])
def test_server_identity_includes_home(tmp_path, monkeypatch, args, owner, expected):
    from types import SimpleNamespace
    from kova import lifecycle
    process = SimpleNamespace(cmdline=lambda: args,
                              environ=lambda: {'KOVA_HOME': str(tmp_path if owner == 'same' else tmp_path / 'other')})
    monkeypatch.setattr(lifecycle.psutil, 'Process', lambda pid: process)
    assert (lifecycle.verified_server(123, tmp_path) is process) == expected
