"""The VM as a runtime resource: created on demand, started on demand, stopped
when idle.

Nothing here is install-time. lima itself is a prerequisite the caller checks
for and reports; it is never installed for you.
"""
import os
import shutil
import subprocess
import sys
import time
from typing import List, Optional, Sequence

from . import lock, paths, proc, ui

NAME = os.environ.get("SCANBOX_VM", "scanbox")
GUEST_LIB = "/usr/local/lib/scanbox"


def require_lima() -> None:
    if shutil.which("limactl") is None:
        ui.die(
            "lima is not installed. It is a prerequisite, not something this "
            "tool installs:\n\n    brew install lima"
        )


def shell_cmd(*args: str) -> List[str]:
    return ["limactl", "shell", NAME] + list(args)


def _logged(cmd: Sequence[str], stdin_path: Optional[str] = None) -> bool:
    """Run a lima command, keeping its narration in a log.

    limactl reports its whole boot to stderr. Keeping that in a log and
    surfacing it only when something actually failed is what keeps normal use
    quiet without throwing away the one thing you need when it does fail.
    """
    paths.ensure_state_dir()
    stdin_f = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL
    try:
        with open(paths.VM_LOG, "ab") as log:
            code = subprocess.call(list(cmd), stdin=stdin_f, stdout=log, stderr=log)
    finally:
        if hasattr(stdin_f, "close"):
            stdin_f.close()
    if code != 0:
        ui.say("lima failed; last lines of {}:".format(paths.VM_LOG))
        try:
            with open(paths.VM_LOG) as f:
                for line in f.read().splitlines()[-15:]:
                    ui.say(line)
        except OSError:
            pass
        return False
    return True


def exists() -> bool:
    res = proc.run(["limactl", "list", "--format", "{{.Name}}"], timeout=15)
    return NAME in res.out.split()


def status() -> str:
    res = proc.run(["limactl", "list", NAME, "--format", "{{.Status}}"], timeout=15)
    return res.out.strip()


def running() -> bool:
    return status() == "Running"


def responsive() -> bool:
    """lima's cached status lies after a lid-close or reboot, so ask the guest."""
    return proc.run(shell_cmd("true"), timeout=15).ok


def is_hplip_provisioned() -> bool:
    if not proc.run(shell_cmd("test", "-x", GUEST_LIB + "/autofit.sh"), timeout=20).ok:
        return False
    # Any scan backend will do. HP ships bb_soap / bb_soapht / bb_marvell /
    # bb_escl and the installer lays down all of them; which one a given model
    # needs varies, so testing for one specific file would wrongly fail on
    # other printers.
    probe = ('for f in /usr/share/hplip/scan/plugins/bb_*.so; '
             'do [ -e "$f" ] && exit 0; done; exit 1')
    return proc.run(shell_cmd("bash", "-c", probe), timeout=20).ok


# Kept for callers outside the package that used the old HP-specific name.
def is_provisioned() -> bool:
    return is_hplip_provisioned()


def is_wsd_provisioned() -> bool:
    """Whether the guest has the SANE frontend and WSD-capable backend."""
    check = (
        "command -v scanimage >/dev/null && "
        "dpkg-query -W -f='${Status}\\n' sane-airscan 2>/dev/null | "
        "grep -qx 'install ok installed'"
    )
    return proc.run(shell_cmd("bash", "-c", check), timeout=20).ok


def sync_lib() -> None:
    """Push the guest-side library every run.

    One small file over an existing SSH connection, and it removes a nasty
    failure mode: an edited autofit.sh on the host with a stale copy in the VM
    produces wrong page sizes and no error at all.
    """
    proc.run(shell_cmd("sudo", "install", "-d", GUEST_LIB), timeout=30)
    with open(paths.AUTOFIT_SH) as f:
        proc.run(shell_cmd("sudo", "tee", GUEST_LIB + "/autofit.sh"),
                 timeout=30, stdin_text=f.read())
    proc.run(shell_cmd("sudo", "chmod", "+x", GUEST_LIB + "/autofit.sh"), timeout=30)


def provision() -> None:
    ui.say("provisioning the VM (installs HPLIP and HP's scan plugin; a few minutes)")
    if not _logged(shell_cmd("sudo", "bash", "-s"), paths.PROVISION_PACKAGES_SH):
        ui.die("package installation failed")
    if not _logged(shell_cmd("sudo", "bash", "-s"), paths.PROVISION_PLUGIN_SH):
        ui.die("HPLIP plugin installation failed")
    sync_lib()
    ui.say("provisioning complete")


def provision_wsd() -> None:
    ui.say("provisioning WSD scanning support (a minute or two)")
    if not _logged(shell_cmd("sudo", "bash", "-s"), paths.PROVISION_AIRSCAN_SH):
        ui.die("WSD package installation failed")
    ui.say("WSD provisioning complete")


def ensure_runtime() -> None:
    """Create or start the guest without choosing or installing a backend."""
    require_lima()
    if not exists():
        ui.say("no scanbox VM yet -- creating it "
               "(first run downloads ~400MB; several minutes)")
        if not _logged(["limactl", "start", "--name=" + NAME, "--tty=false",
                        paths.LIMA_CONFIG]):
            ui.die("could not create the VM")
        return

    if running():
        if not responsive():
            ui.say("VM claims to be running but is not responding "
                   "(stale after sleep); restarting")
            proc.run(["limactl", "stop", "-f", NAME], timeout=60)
            if not _logged(["limactl", "start", NAME, "--tty=false"]):
                ui.die("could not restart the VM")
    else:
        with ui.Spinner("starting the scanbox VM (~26s)"):
            if not _logged(["limactl", "start", NAME, "--tty=false"]):
                ui.die("could not start the VM")

def ensure() -> None:
    """Ensure the existing HPLIP backend is available."""
    ensure_runtime()
    if is_hplip_provisioned():
        sync_lib()
    else:
        provision()


def ensure_wsd() -> None:
    """Ensure WSD support without installing or initializing HPLIP."""
    ensure_runtime()
    if not is_wsd_provisioned():
        provision_wsd()


def stop() -> None:
    if not exists() or not running():
        return
    if not proc.run(["limactl", "stop", NAME], timeout=120).ok:
        proc.run(["limactl", "stop", "-f", NAME], timeout=60)


# ---------------------------------------------------------------------------
# Idle shutdown.
#
# Deliberately not a launchd agent: a permanently-registered background job is
# the exact thing dropping AirSane was meant to avoid. `scan` is the only thing
# that ever starts the VM, so it also owns stopping it -- one detached timer,
# guarded by a pidfile, that re-reads the last-used timestamp each tick instead
# of respawning.
# ---------------------------------------------------------------------------

def timer_running() -> bool:
    try:
        with open(paths.IDLE_TIMER_PID) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def idle_timer_arm(keep_alive_min: int) -> None:
    paths.mark_used()
    if timer_running():
        return
    paths.ensure_state_dir()
    # start_new_session gives us setsid semantics, which macOS has no binary
    # for -- the shell version had to make do with nohup. The timer has to
    # outlive the shell that started the scan.
    env = dict(os.environ)
    env["PYTHONPATH"] = paths.ROOT + os.pathsep + env.get("PYTHONPATH", "")
    with open(paths.IDLE_TIMER_LOG, "ab") as log:
        p = subprocess.Popen(
            [sys.executable, "-m", "scanbox", "__idle-timer", str(keep_alive_min)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, env=env, cwd=paths.ROOT,
        )
    with open(paths.IDLE_TIMER_PID, "w") as f:
        f.write("{:d}\n".format(p.pid))


def idle_timer_run(keep_alive_min: int) -> None:
    """The detached loop itself."""
    limit = keep_alive_min * 60
    try:
        while True:
            time.sleep(60)
            if not exists() or not running():
                break
            if lock.is_held():
                continue                       # never stop mid-scan
            if paths.seconds_since_use() >= limit:
                print("{} stopping idle VM after {} min".format(
                    time.strftime("%F %T"), keep_alive_min), flush=True)
                stop()
                break
    finally:
        try:
            os.unlink(paths.IDLE_TIMER_PID)
        except OSError:
            pass
