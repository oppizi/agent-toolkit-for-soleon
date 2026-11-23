"""The background workers the hooks start never open console windows on Windows.

Receipt (2026-11-23): the freshness worker was started with DETACHED_PROCESS |
CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW. Windows ignores CREATE_NO_WINDOW beside
DETACHED_PROCESS, so the worker had no console, and each of the five `git` calls it makes
per check opened a new visible console window: bursts of windows on the desktop every time
a session's check ran.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

from _local_emulation_fixtures import BIN

sys.path.insert(0, str(BIN))
import soleon_freshness as fr  # noqa: E402
import soleon_progress as pg  # noqa: E402

DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


class _Popen:
    calls: list = []

    def __init__(self, cmd, **kwargs):
        _Popen.calls.append((cmd, kwargs))


def _spawned_kwargs(monkeypatch, spawn) -> dict:
    _Popen.calls = []
    monkeypatch.setattr(subprocess, "Popen", _Popen)
    spawn()
    assert len(_Popen.calls) == 1
    return _Popen.calls[0][1]


def test_both_workers_start_with_a_hidden_console_never_detached(monkeypatch, tmp_path):
    for module, spawn in ((fr, lambda: fr.spawn_worker(tmp_path, "s1")),
                          (pg, lambda: pg._spawn_worker(tmp_path / "job.json"))):
        assert module.WORKER_CREATIONFLAGS & DETACHED_PROCESS == 0, module.__name__
        assert module.WORKER_CREATIONFLAGS & CREATE_NO_WINDOW, module.__name__
        kwargs = _spawned_kwargs(monkeypatch, spawn)
        if os.name == "nt":
            assert kwargs["creationflags"] == module.WORKER_CREATIONFLAGS, module.__name__
        else:
            assert kwargs["start_new_session"] is True, module.__name__


def test_a_command_the_worker_runs_gets_no_visible_window(tmp_path):
    """The real Windows behaviour, end to end: a worker started with the flags runs a child
    the plain way (as the worker's `git` calls are run) and the child reports its window.
    Elsewhere there are no console windows, and the flags are not used."""
    if os.name != "nt":
        assert fr.WORKER_CREATIONFLAGS == pg.WORKER_CREATIONFLAGS
        return
    out = tmp_path / "probe.txt"
    script = tmp_path / "probe.py"
    script.write_text(textwrap.dedent("""
        import ctypes, subprocess, sys
        out = sys.argv[2]
        if sys.argv[1] == "worker":
            subprocess.run([sys.executable, __file__, "child", out], stdin=subprocess.DEVNULL, timeout=30)
        else:
            h = ctypes.windll.kernel32.GetConsoleWindow()
            open(out, "w").write("visible" if h and ctypes.windll.user32.IsWindowVisible(h) else "hidden")
    """), encoding="utf-8")
    subprocess.Popen([sys.executable, "-I", str(script), "worker", str(out)], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                     creationflags=fr.WORKER_CREATIONFLAGS)
    deadline = time.time() + 30
    while not out.exists() and time.time() < deadline:
        time.sleep(0.1)
    assert out.read_text() == "hidden"
