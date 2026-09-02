"""Subprocess helpers.

The shell version needed perl to provide timeouts, because macOS ships no
`timeout` -- and it had to be careful to reap rather than exec, or the shell
printed "Alarm clock: 14" and lost the exit status. subprocess handles all of
that natively.
"""
import os
import signal
import subprocess
import threading
import time
from typing import Callable, Optional, Sequence


class Result:
    def __init__(self, code: int, out: str, err: str, timed_out: bool = False):
        self.code = code
        self.out = out
        self.err = err
        self.timed_out = timed_out

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out


def run(cmd: Sequence[str], timeout: Optional[float] = None,
        stdin_text: Optional[str] = None, check: bool = False) -> Result:
    """Run to completion and collect everything it printed."""
    try:
        p = subprocess.run(
            list(cmd),
            input=stdin_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,   # 3.9 spelling of text=True
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        err = e.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return Result(124, out, err, timed_out=True)
    except FileNotFoundError:
        return Result(127, "", "{}: not found".format(cmd[0]))
    res = Result(p.returncode, p.stdout or "", p.stderr or "")
    if check and not res.ok:
        raise RuntimeError("{} failed: {}".format(" ".join(cmd), res.err.strip()))
    return res


def collect_until_timeout(cmd: Sequence[str], seconds: float) -> str:
    """Run a command that never exits on its own and collect what it printed.

    Collects; it does not stream -- nothing here sees a line before the command
    is stopped. `dns-sd` browses forever by design, so hitting the timeout is
    the expected ending rather than an error: the shell version had to work
    around perl's SIGALRM exit status propagating through `set -o pipefail` and
    killing the caller.

    For output that has to be *seen as it arrives*, use run_streaming.
    """
    return run(cmd, timeout=seconds).out


def _group_alive(pgid: int) -> bool:
    """Whether anything at all remains in the process group."""
    try:
        os.killpg(pgid, 0)
        return True
    except OSError:
        return False


def _kill_group(p: "subprocess.Popen", grace: float = 3.0) -> None:
    """Signal the child's whole process group, not just the child.

    Killing the direct child is not enough, and the reason is the one that bit
    us in the VM: a process that forks leaves its children running, and a
    surviving grandchild still holds the stdout pipe open -- so the reader
    waiting for EOF goes on waiting. Measured: killing `bash -c "sleep 30"`
    alone made a 1s timeout take the full 30s.

    One sweep is not enough either. killpg signals the processes that exist at
    that instant, so a child forked in the window between the signal and its
    parent dying was never in that set and survives -- measured, again with
    `bash -c "echo go; sleep 30"`, which leaked the sleep. Hence: sweep, wait
    for the leader (after which nothing new can appear), sweep again, and
    escalate to SIGKILL only if something is still standing.
    """
    try:
        pgid = os.getpgid(p.pid)
    except OSError:
        return                                 # already reaped
    if pgid == os.getpgid(0):
        return                                 # never signal ourselves

    def sweep(sig: int) -> bool:
        try:
            os.killpg(pgid, sig)
            return True
        except OSError:
            return False                       # nothing left in the group

    if not sweep(signal.SIGTERM):
        return
    deadline = time.time() + grace
    # poll(), not wait(): this may run on the timer thread while the main
    # thread is about to reap, and only one of them may block. Reaping also
    # matters for the probe below -- an unreaped zombie still answers kill(0).
    while time.time() < deadline and p.poll() is None:
        time.sleep(0.05)
    sweep(signal.SIGTERM)
    while time.time() < deadline:
        p.poll()
        if not _group_alive(pgid):
            return
        time.sleep(0.05)
    sweep(signal.SIGKILL)


def run_streaming(cmd: Sequence[str], on_line: Callable[[str], None],
                  stdin_path: Optional[str] = None,
                  stderr_path: Optional[str] = None,
                  timeout: Optional[float] = None) -> Result:
    """Run a command, handing each stdout line to `on_line` as it arrives.

    This is what a live progress display needs, and `run` cannot provide it:
    `run` returns only once the child has exited, by which point a thirteen
    minute scan has been over for a while.

    Arriving *as it arrives* is the entire contract, so the buffering deserves a
    word. `readline()` on the pipe returns as soon as a newline shows up -- the
    underlying buffered reader does not wait to fill itself first -- so the
    obvious loop does stream. The failure this replaces was one layer further
    out: mawk buffers its *input* when RS is not a newline, so every percentage
    in a scan arrived in a single burst at the end, all of them perfectly
    correct. Anything asserting on the set of values it received would have
    passed. Test this by timing.

    stderr goes to a file rather than a pipe on purpose: reading only stdout
    while the child fills an unread stderr pipe is a deadlock, and the scan path
    wants stderr kept for diagnosis anyway. Without a path it is discarded.

    The child gets its own process group, so tearing it down is explicit and
    complete (see _kill_group). The trade-off is that Ctrl-C at the terminal no
    longer reaches it for free -- which is fine here, because the teardown has
    to be ordered anyway: kill the local child, abort the scan still running in
    the guest, and only then drop the lock.
    """
    stdin_f = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL
    stderr_f = open(stderr_path, "wb") if stderr_path else subprocess.DEVNULL
    try:
        try:
            p = subprocess.Popen(
                list(cmd),
                stdin=stdin_f,
                stdout=subprocess.PIPE,
                stderr=stderr_f,
                universal_newlines=True,
                bufsize=1,                     # line buffered
                start_new_session=True,        # its own process group
            )
        except FileNotFoundError:
            return Result(127, "", "{}: not found".format(cmd[0]))

        expired = threading.Event()
        timer = None
        if timeout:
            def _expire() -> None:
                expired.set()
                _kill_group(p)

            timer = threading.Timer(timeout, _expire)
            timer.daemon = True
            timer.start()

        try:
            assert p.stdout is not None
            for line in iter(p.stdout.readline, ""):
                on_line(line.rstrip("\n"))
            code = p.wait()
        except BaseException:
            # Ctrl-C, or on_line raising. The child must not be left running:
            # for the scan path it is holding the printer's only scan session.
            _kill_group(p)
            p.wait()
            raise
        finally:
            if timer is not None:
                timer.cancel()
            if p.stdout is not None:
                p.stdout.close()
    finally:
        for f in (stdin_f, stderr_f):
            if hasattr(f, "close"):
                f.close()

    if expired.is_set():
        return Result(124, "", "", timed_out=True)
    return Result(code, "", "")
