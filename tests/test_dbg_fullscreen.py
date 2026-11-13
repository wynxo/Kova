import pytest, sys
from tests.test_tui import IDLE, write_config
from tests.fake_kova import Terminal, FakeKova


@pytest.fixture
def kova_home(tmp_path):
    fake = FakeKova()
    home = tmp_path / ".kova"
    write_config(home, fake.port)
    project = tmp_path / "project"
    (project / "sub").mkdir(parents=True)
    (project / "notes.md").write_text("alpha\nbeta\n")
    yield fake, home, project
    fake.close()


def test_dbg(kova_home):
    fake, home, project = kova_home
    term = Terminal(home, project, "--new", columns=90, rows=30)
    try:
        term.screen(IDLE, 30)
        term.type(b"/mo")
        term.screen("List models; /model <name> switches")
        term.type(b"\x15/model\r")
        term.screen("Select model")
        term.type(b"\x1b[B\r")
        term.screen("Now using other")
        term.type(b"hello\r")
        term.screen("Hello from the fake server.")
        term.screen("Worked for")
        term.wait(lambda: IDLE in term.vt.text(), "the idle prompt")
        term.type(b"\x1b[Z")
        term.screen("auto permissions")
        term.type(b"run it\r")
        term.screen("Do you want to proceed?")
        import time
        time.sleep(1.0)
        term.read(0.5)
        print("=====FULL SCREEN=====")
        print(term.vt.text())
        print("=====END=====")
        print("PROC:", term.process.poll())
        import time as _t
        while term.process.poll() is None and _t.time() - _t.time() < 0.1:
            break
        term.read(0.3)
        print("RAW TAIL:", term.raw[-2000:])
    finally:
        term.close()
