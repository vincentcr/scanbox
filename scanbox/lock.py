"""The inter-process scan lock.

macOS has no `flock`, and this has to be an *inter-process* mutex -- two
`scanbox scan` invocations are two processes, so a threading.Lock would not
even be in the conversation. mkdir is atomic on every filesystem we care
about, so it is the portable primitive; the pid written inside is what lets a
later run tell a live holder from one that died mid-scan.
"""
import os
import time
from typing import Optional

from . import paths, ui


def _pid_file(directory: str) -> str:
    return os.path.join(directory, "pid")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                            # exists, just not ours


def is_held(directory: Optional[str] = None) -> bool:
    return os.path.isdir(directory or paths.LOCK_DIR)


class Lock:
    """Context manager around the lock directory.

    Held for the whole scan, including the teardown of an interrupted one: the
    scanner is still busy until the guest process is actually gone, so
    releasing early is what makes the *next* run fail with a bare device I/O
    error. Order matters -- abort the remote scan, then release.
    """

    def __init__(self, directory: Optional[str] = None):
        self.dir = directory or paths.LOCK_DIR
        self.owned = False

    def acquire(self, limit: int = 600) -> "Lock":
        waited = 0
        paths.ensure_state_dir()
        while True:
            try:
                os.mkdir(self.dir)
                break
            except FileExistsError:
                holder = self._holder()
                if holder is not None and not _alive(holder):
                    ui.warn("clearing stale lock from dead process {}".format(holder))
                    self._force_clear()
                    continue
                if waited == 0:
                    ui.say("another scan is in progress; waiting...")
                time.sleep(2)
                waited += 2
                if waited >= limit:
                    ui.die("timed out waiting for the lock held by {}".format(
                        holder if holder is not None else "unknown"))
        with open(_pid_file(self.dir), "w") as f:
            f.write("{:d}\n".format(os.getpid()))
        self.owned = True
        return self

    def _holder(self) -> Optional[int]:
        try:
            with open(_pid_file(self.dir)) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def _force_clear(self) -> None:
        try:
            os.unlink(_pid_file(self.dir))
        except OSError:
            pass
        try:
            os.rmdir(self.dir)
        except OSError:
            pass

    def release(self) -> None:
        """Only ever release a lock we actually hold.

        A blanket remove would let one process's cleanup delete the lock
        another process is scanning under.
        """
        if not self.owned:
            return
        if self._holder() == os.getpid():
            self._force_clear()
        self.owned = False

    def __enter__(self) -> "Lock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
