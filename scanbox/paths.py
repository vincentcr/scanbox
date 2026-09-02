"""Where things live on the host.

The shell version resolved its own location through symlinks by hand, because
it is normally invoked via a symlink in ~/.local/bin and macOS has no
`readlink -f`. A Python package has no such problem: this module's file is
inside the package, so the repo root is two levels up from it.
"""
import os
import time

# .../scanbox/paths.py -> .../scanbox -> ...
PACKAGE_DIR = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(PACKAGE_DIR)

STATE_DIR = os.environ.get(
    "SCANBOX_STATE_DIR", os.path.expanduser("~/.local/state/scanbox")
)
DEFAULT_OUT_DIR = os.environ.get("SCANBOX_OUT", os.path.expanduser("~/Pictures/Scans"))

# Guest-side and provisioning scripts stay bash: they run inside the Debian VM,
# which has no reason to grow a Python dependency, and guest-scan.sh in
# particular is piped in over stdin so the VM never learns where this repo is.
GUEST_SCAN_SH = os.path.join(ROOT, "lib", "guest-scan.sh")
AUTOFIT_SH = os.path.join(ROOT, "lib", "autofit.sh")
PROVISION_PACKAGES_SH = os.path.join(ROOT, "provision", "10-packages.sh")
PROVISION_PLUGIN_SH = os.path.join(ROOT, "provision", "20-plugin.sh")
LIMA_CONFIG = os.path.join(ROOT, "scanbox.yaml")

VM_LOG = os.path.join(STATE_DIR, "lima.log")
IDLE_TIMER_LOG = os.path.join(STATE_DIR, "idle-timer.log")
IDLE_TIMER_PID = os.path.join(STATE_DIR, "idle-timer.pid")
LOCK_DIR = os.path.join(STATE_DIR, "lock")
LAST_USED = os.path.join(STATE_DIR, "last-used")


def ensure_state_dir() -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    return STATE_DIR


def tilde(path: str) -> str:
    """Shorten a path for display, the way the shell's ${x/#$HOME/~} did."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def uri_cache(ip: str) -> str:
    return os.path.join(STATE_DIR, "uri-{}".format(ip))


def mark_used() -> None:
    ensure_state_dir()
    with open(LAST_USED, "w") as f:
        f.write("{:d}\n".format(int(time.time())))


def seconds_since_use() -> int:
    """Large number when we have never scanned, so an idle check just fires."""
    try:
        with open(LAST_USED) as f:
            then = int(f.read().strip())
    except (OSError, ValueError):
        return 999999
    return int(time.time()) - then
