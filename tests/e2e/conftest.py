import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENTRYPOINT = Path(__file__).resolve().parent / "_live_server_app.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(isolated_env):
    """Boots the real Flask app (via _live_server_app.py) as a subprocess
    against the same tmp_path isolated_env already pointed this test
    process's agent.config at — so tests can seed data with store.save_plans()
    directly and the server (a separate process) reads/writes those same
    files. Heavy pipeline calls are faked inside the subprocess; see that
    script's docstring."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(ENTRYPOINT), str(isolated_env["tmp_path"]), str(port)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base_url = f"http://127.0.0.1:{port}"

    deadline = time.monotonic() + 15
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read()
            raise RuntimeError(f"live server exited early (code {proc.returncode}):\n{output}")
        try:
            requests.get(base_url + "/", timeout=0.5)
            ready = True
            break
        except requests.exceptions.RequestException:
            time.sleep(0.1)
    if not ready:
        proc.terminate()
        raise RuntimeError("live server did not become ready within 15s")

    try:
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
