"""Subprocess helpers.

The shell version needed perl to provide timeouts, because macOS ships no
`timeout` -- and it had to be careful to reap rather than exec, or the shell
printed "Alarm clock: 14" and lost the exit status. subprocess handles all of
that natively.
"""
import subprocess
from typing import List, Optional, Sequence


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


def stream_until_timeout(cmd: Sequence[str], seconds: float) -> str:
    """Run a command that never exits on its own and collect what it printed.

    `dns-sd` browses forever by design, so hitting the timeout is the expected
    ending, not an error -- the shell version had to work around perl's SIGALRM
    exit status propagating through `set -o pipefail` and killing the caller.
    """
    return run(cmd, timeout=seconds).out
