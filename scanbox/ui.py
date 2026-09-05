"""Terminal output: messages and the progress spinner.

The shell version's spinner was a background subshell, which caused a genuine
bug: started from inside `$( )` its pid was invisible to the parent, background
jobs inherit SIGINT ignored, and it survived Ctrl-C drawing over the prompt.

A daemon thread cannot outlive its process, so that entire class of failure is
gone by construction rather than by careful signal handling.

For the same reason the shell's spinner message *file* is deliberately not
ported. That file existed solely because bash runs a pipeline in a subshell,
where assigning to a variable could never reach the animation. A thread reads
the attribute the caller writes, so `spinner.msg = ...` is the whole mechanism.
"""
import sys
import threading
from typing import Optional

FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class ScanboxError(Exception):
    """Fatal, already-explained failure. main() prints it and exits 1."""


def say(msg: str = "") -> None:
    print(msg, file=sys.stderr)


def warn(msg: str) -> None:
    print("scanbox: {}".format(msg), file=sys.stderr)


def die(msg: str) -> "ScanboxError":
    """Raises. Returns a value only so `raise die(...)` reads naturally too."""
    raise ScanboxError(msg)


def is_tty() -> bool:
    return sys.stderr.isatty()


class Spinner:
    """Progress indicator for steps that are slow with nothing to show.

    Use as a context manager. Off a terminal it prints a single plain line
    instead of animating, so piped output and CI logs stay readable -- callers
    with real progress to report should check `animating` and print their own
    periodic marks in that case.
    """

    def __init__(self, msg: str):
        self.msg = msg
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Every write to stderr from here goes through one lock. The animation
        # runs on its own thread, so an unguarded note() could interleave with a
        # half-written frame and leave spinner glyphs inside the message.
        self._io = threading.Lock()
        self._drawn: Optional[str] = None

    @property
    def animating(self) -> bool:
        """Whether frames are being drawn -- false when stderr is not a tty."""
        return self._thread is not None

    def __enter__(self) -> "Spinner":
        if not is_tty():
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
            with self._io:
                cur = self.msg
                # A message that gets shorter must not leave the tail of the
                # previous one on screen -- and it does get shorter, going from
                # "scanning  43.8%, ~6s left" to "building the PDF".
                if cur != self._drawn:
                    sys.stderr.write("\r\033[K")
                    self._drawn = cur
                sys.stderr.write("\r  {} {} ".format(FRAMES[i % len(FRAMES)], cur))
                sys.stderr.flush()
            i += 1
            self._stop.wait(0.1)

    def note(self, msg: str) -> None:
        """Print a line without the animation scribbling over it.

        The guest emits these as it goes -- a device-busy retry, say -- and they
        have to survive to the user. The next tick redraws the spinner below.
        """
        with self._io:
            if is_tty():
                sys.stderr.write("\r\033[K")
            sys.stderr.write("scanbox: {}\n".format(msg))
            sys.stderr.flush()
            self._drawn = None                 # force a full redraw next tick

    def stop(self) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=1)
            self._thread = None
            with self._io:
                sys.stderr.write("\r\033[K")
                sys.stderr.flush()
        show_cursor()


def show_cursor() -> None:
    """Unconditional: a hidden cursor left behind is a nasty parting gift."""
    if is_tty():
        sys.stderr.write("\033[?25h")
        sys.stderr.flush()


def tty_readable() -> bool:
    """Whether we can read and write a prompt on the controlling terminal.

    /dev/tty passes a readability check on permissions even with no controlling
    terminal, so actually opening it is the only reliable test. Use the same
    read/write mode as ask(): a sandbox can permit reading the device while
    refusing the write needed to display a prompt.
    """
    try:
        with open("/dev/tty", "r+"):
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
