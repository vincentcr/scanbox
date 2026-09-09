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
from enum import Enum
from typing import List, Optional, Sequence

from . import lock, paths, proc, ui

NAME = os.environ.get("SCANBOX_VM", "scanbox")
GUEST_LIB = "/usr/local/lib/scanbox"


class Capability(Enum):
    """Software capabilities that can be added to an existing guest."""

    CORE = "core"
    WSD = "wsd"
    HPLIP = "hplip"
    HP_PLUGIN = "hp-plugin"


_CAPABILITY_ORDER = (
    Capability.CORE,
    Capability.WSD,
    Capability.HPLIP,
    Capability.HP_PLUGIN,
)
_DEPENDENCIES = {
    Capability.WSD: (Capability.CORE,),
    Capability.HPLIP: (Capability.CORE,),
    Capability.HP_PLUGIN: (Capability.HPLIP,),
}
_PROVISION_SCRIPTS = {
    Capability.CORE: paths.PROVISION_CORE_SH,
    Capability.WSD: paths.PROVISION_AIRSCAN_SH,
    Capability.HPLIP: paths.PROVISION_HPLIP_SH,
    Capability.HP_PLUGIN: paths.PROVISION_PLUGIN_SH,
}
_CAPABILITY_LABELS = {
    Capability.CORE: "core SANE tools",
    Capability.WSD: "sane-airscan WSD backend",
    Capability.HPLIP: "HPLIP hpaio backend",
    Capability.HP_PLUGIN: "proprietary HP scan plugin",
}


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


def _packages_probe(*packages: str) -> str:
    quoted = " ".join(packages)
    return (
        "for package in {}; do ".format(quoted)
        + "dpkg-query -W -f='${Status}\\n' \"$package\" 2>/dev/null | "
        + "grep -qx 'install ok installed' || exit 1; done"
    )


def is_capability_provisioned(capability: Capability) -> bool:
    """Probe installed behavior, not a stale global provisioned marker."""
    if capability == Capability.CORE:
        check = (
            "command -v scanimage >/dev/null && "
            + _packages_probe("sane-utils", "ca-certificates")
        )
    elif capability == Capability.WSD:
        check = _packages_probe("sane-airscan")
    elif capability == Capability.HPLIP:
        check = (
            "command -v hp-makeuri >/dev/null && "
            "command -v convert >/dev/null && "
            + _packages_probe("hplip", "libsane-hpaio", "imagemagick")
            + " && grep -qx hpaio /etc/sane.d/dll.conf"
        )
    elif capability == Capability.HP_PLUGIN:
        # HP ships several model-specific backends and the installer lays down
        # all of them. Any one proves that the plugin payload is present.
        check = (
            "for f in /usr/share/hplip/scan/plugins/bb_*.so; "
            "do [ -e \"$f\" ] && exit 0; done; exit 1"
        )
    else:
        raise ValueError("unknown guest capability: {!r}".format(capability))
    return proc.run(shell_cmd("bash", "-c", check), timeout=20).ok


def is_hplip_provisioned() -> bool:
    return all(is_capability_provisioned(capability) for capability in (
        Capability.CORE, Capability.HPLIP, Capability.HP_PLUGIN,
    ))


# Kept for callers outside the package that used the old HP-specific name.
def is_provisioned() -> bool:
    return is_hplip_provisioned()


def is_wsd_provisioned() -> bool:
    return all(is_capability_provisioned(capability) for capability in (
        Capability.CORE, Capability.WSD,
    ))


def sync_lib() -> None:
    """Push the guest-side library every run.

    One small file over an existing SSH connection, and it removes a nasty
    failure mode: an edited autofit.sh on the host with a stale copy in the VM
    produces wrong page sizes and no error at all.
    """
    if not proc.run(
        shell_cmd("sudo", "install", "-d", GUEST_LIB), timeout=30
    ).ok:
        ui.die(
            "guest provisioning failed for the HPLIP scan helper "
            "(component: hplip-helper)"
        )
    with open(paths.AUTOFIT_SH) as f:
        installed = proc.run(
            shell_cmd("sudo", "tee", GUEST_LIB + "/autofit.sh"),
            timeout=30, stdin_text=f.read(),
        )
    executable = proc.run(
        shell_cmd("sudo", "chmod", "+x", GUEST_LIB + "/autofit.sh"),
        timeout=30,
    )
    if not installed.ok or not executable.ok:
        ui.die(
            "guest provisioning failed for the HPLIP scan helper "
            "(component: hplip-helper)"
        )


def _capability_closure(requested: Sequence[Capability]) -> Sequence[Capability]:
    wanted = set()

    def add(capability: Capability) -> None:
        if not isinstance(capability, Capability):
            raise ValueError("unknown guest capability: {!r}".format(capability))
        for dependency in _DEPENDENCIES.get(capability, ()):
            add(dependency)
        wanted.add(capability)

    for capability in requested:
        add(capability)
    return tuple(item for item in _CAPABILITY_ORDER if item in wanted)


def provision_capability(capability: Capability) -> None:
    """Install and validate exactly one guest capability."""
    label = _CAPABILITY_LABELS[capability]
    ui.say("provisioning {}".format(label))
    if not _logged(
        shell_cmd("sudo", "bash", "-s"), _PROVISION_SCRIPTS[capability]
    ):
        ui.die(
            "guest provisioning failed for {} (component: {})".format(
                label, capability.value
            )
        )
    if not is_capability_provisioned(capability):
        ui.die(
            "guest provisioning completed but {} is still unavailable "
            "(component: {})".format(label, capability.value)
        )
    ui.say("{} ready".format(label))


def ensure_capabilities(*requested: Capability) -> None:
    """Start the guest, then add only the requested backend dependencies."""
    ensure_runtime()
    for capability in _capability_closure(requested):
        if not is_capability_provisioned(capability):
            provision_capability(capability)


def provision() -> None:
    """Compatibility entry point for provisioning the complete HPLIP path."""
    for capability in _capability_closure((Capability.HP_PLUGIN,)):
        if not is_capability_provisioned(capability):
            provision_capability(capability)
    sync_lib()


def provision_wsd() -> None:
    """Compatibility entry point for provisioning the complete WSD path."""
    for capability in _capability_closure((Capability.WSD,)):
        if not is_capability_provisioned(capability):
            provision_capability(capability)


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

def ensure_hplip() -> None:
    """Ensure HPLIP support, including its proprietary plugin."""
    ensure_capabilities(Capability.HP_PLUGIN)
    sync_lib()


def ensure() -> None:
    """Compatibility alias for the original HPLIP-only guest entry point."""
    ensure_hplip()


def ensure_wsd() -> None:
    """Ensure WSD support without installing or initializing HPLIP."""
    ensure_capabilities(Capability.WSD)


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
