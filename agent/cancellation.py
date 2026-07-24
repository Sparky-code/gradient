"""Cooperative cancellation for the background pipeline pass — what the web
dashboard's Cancel (X) button actually does.

A `main.py once`/`reevaluate_plan()` pass spends nearly all its wall-clock
time inside local-model subprocess calls (reclassify, tagging, taxonomy
naming, embedding) — there's no natural "checkpoint" to interrupt between
those, so cancellation works by actually terminating whichever subprocess is
in flight right now, plus a flag every multi-stage loop (loop.py's per-file
loop, reevaluator.py) checks before starting its next stage. This is
deliberately coarse: a cancel request kills the current subprocess almost
immediately, but a stage that was mid-way through non-subprocess work
(e.g. a Senso HTTP call) finishes that one call before the next checkpoint
notices the request.
"""

import subprocess
import threading

_cancel_event = threading.Event()
_active_process_lock = threading.Lock()
_active_process: subprocess.Popen | None = None


class Cancelled(Exception):
    """Raised by run_cancellable() when a cancellation was requested — every
    caller already catches subprocess.CalledProcessError/TimeoutExpired and
    degrades to a safe stub/no-op result, so callers catch this alongside
    those rather than needing new handling."""


def reset() -> None:
    """Called once at the start of a fresh run/reevaluation — clears any
    cancellation left over from a previous pass."""
    _cancel_event.clear()


def is_cancelled() -> bool:
    return _cancel_event.is_set()


def request_cancel() -> None:
    """What the Cancel button's route calls. Sets the flag every loop
    checkpoint watches, and best-effort terminates whatever subprocess is
    currently running (the thing actually holding up the pass)."""
    _cancel_event.set()
    with _active_process_lock:
        if _active_process is not None:
            _active_process.terminate()


def run_cancellable(args: list[str], timeout: float, **kwargs) -> subprocess.CompletedProcess:
    """Drop-in replacement for subprocess.run(check=True, timeout=timeout,
    capture_output=True) that registers the live process so request_cancel()
    can terminate it, and raises Cancelled if a cancellation is/was in
    flight — whether or not termination actually caught it in time."""
    global _active_process
    if is_cancelled():
        raise Cancelled()

    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    with _active_process_lock:
        _active_process = proc
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    finally:
        with _active_process_lock:
            _active_process = None

    if is_cancelled():
        raise Cancelled()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, args, stdout, stderr)
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
