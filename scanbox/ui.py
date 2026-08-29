"""Terminal output: messages and the progress spinner.

The shell version's spinner was a background subshell, which caused a genuine
bug: started from inside `$( )` its pid was invisible to the parent, background
jobs inherit SIGINT ignored, and it survived Ctrl-C drawing over the prompt.

A daemon thread cannot outlive its process, so that entire class of failure is
gone by construction rather than by careful signal handling.
"""
import os
import sys
import threading
import time
from typing import Optional

FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class ScanboxError(Exception):
    """Fatal, already-explained failure. main() prints it and exits 1."""


def say(msg: str = "") -> None:
    print(msg, file=sys.stderr)


def warn(msg: str) -> None:
    print("scanbox: {}".format(msg), file=sys.stderr)


def die(msg: str) -> "ScanboxError":
    raise ScanboxError(msg)


def _tty() -> bool:
    return sys.stderr.isatty()


class Spinner:
    """Progress indicator for steps that are slow with nothing to show.

    Use as a context manager. Off a terminal it prints a single plain line
    instead of animating, so piped output and CI logs stay readable.
    """

    def __init__(self, msg: str):
        self.msg = msg
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "Spinner":
        if not _tty():
            say("{}...".format(self.msg))
            return self
        sys.stderr.write("\033[?25l")          # hide cursor
        sys.stderr.flush()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def _spin(self) -> None:
        i = 0
        while not self._stop.is_set():
            sys.stderr.write("\r  {} {} ".format(FRAMES[i % len(FRAMES)], self.msg))
            sys.stderr.flush()
            i += 1
            self._stop.wait(0.1)

    def stop(self) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=1)
            self._thread = None
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()
        show_cursor()


def show_cursor() -> None:
    """Unconditional: a hidden cursor left behind is a nasty parting gift."""
    if _tty():
        sys.stderr.write("\033[?25h")
        sys.stderr.flush()


def tty_readable() -> bool:
    """Whether we can prompt.

    /dev/tty passes a readability check on permissions even with no controlling
    terminal, so actually opening it is the only reliable test.
    """
    try:
        with open("/dev/tty"):
            return True
    except OSError:
        return False


def ask(prompt: str) -> str:
    """Read one line from the terminal, not stdin, so prompting survives piping."""
    with open("/dev/tty", "r+") as tty:
        tty.write(prompt)
        tty.flush()
        return (tty.readline() or "").strip()


def confirm(prompt: str) -> bool:
    return ask("{} [y/N] ".format(prompt)).lower() in ("y", "yes")
