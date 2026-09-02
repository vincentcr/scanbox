"""Driving one scan, from resolving the printer to the files on disk.

The guest speaks a small line protocol on stdout. Three kinds of line are for
the user and are consumed as they arrive -- PROGRESS (a percentage from
scanimage), PHASE (what it moved on to) and NOTE (a passing condition worth
saying out loud). Everything else is the machine-readable summary, read once
the scan is over: PAGE, SOURCE, PAGES, OUT and TRUNCATED.
"""
import os
import shlex
import tempfile
import time
from typing import Dict, List, Optional, Tuple

from . import config, discover, lock, paths, proc, ui, vm

# Bytes/sec, measured off an M276nw over vzNAT.
LOSSLESS_RATE = 550000

# Inches, width x height. `auto` and `max` both scan the whole bed and trim
# afterwards, so what crosses the wire is the bed, not the finished page.
#
# The bed figure is measured rather than assumed: a 1200dpi full-bed scan comes
# back 10198x14026 px, which is 8.50 x 11.69in. The shell version hardcoded 8.5
# as the width for *every* page size, which quietly overstated A4 by 3%.
PAGE_INCHES = {
    "letter": (8.5, 11.0),
    "legal": (8.5, 14.0),
    "a4": (8.27, 11.69),
}
BED_INCHES = (8.5, 11.69)

BITS_PER_PIXEL = {"Color": 24, "Gray": 8, "Lineart": 1}


def lossless_estimate(dpi: int, mode: str, page: str) -> Tuple[int, int]:
    """Megabytes on the wire for one page, and how many seconds that takes.

    --lossless turns off the scanner's in-transit JPEG, so the entire raster
    crosses the network uncompressed: at 1200dpi colour that is 429MB for a
    single sheet at about 550KB/s. Thirteen minutes behind a spinner saying
    only "scanning" is indistinguishable from a hang, which is how these scans
    end up cancelled halfway -- so say the number instead.
    """
    w_in, h_in = PAGE_INCHES.get(page, BED_INCHES)
    bpp = BITS_PER_PIXEL.get(mode, 1)
    total = (w_in * dpi) * (h_in * dpi) * bpp / 8.0
    return int(round(total / 1000000)), int(round(total / LOSSLESS_RATE))


def resolve_printer(override: Optional[str] = None) -> Optional[str]:
    """The configured scanner's IPv4 address, or None if it will not resolve.

    No guessing: if nothing is configured we stop and explain, even when
    exactly one scanner is present. Silent auto-selection is the kind of magic
    that becomes confusing the day a second device appears.
    """
    host = ip = ""
    if override:
        if discover.is_ipv4(override):
            ip = override
        else:
            host = override
    else:
        values = config.load()
        ip = values.get("PRINTER_IP", "")
        host = values.get("PRINTER_HOST", "")

    if not ip and not host:
        ui.die("no scanner configured yet. Run:\n\n    scanbox setup")
    if not ip:
        # The slowest silent step by far when the printer is not on this network.
        with ui.Spinner("looking up {}".format(host)):
            ip = discover.resolve_ipv4(host) or ""
    return ip or None


def device_uri(ip: str) -> str:
    """HPLIP's own hp-makeuri gives the canonical model string, which beats
    deriving it from the Bonjour name. Cached per address: it costs an SNMP
    round trip, and sits for the full timeout if the printer is unreachable.
    """
    cache = paths.uri_cache(ip)
    try:
        with open(cache) as f:
            cached = f.read().strip()
        if cached:
            return cached
    except OSError:
        pass

    with ui.Spinner("identifying the scanner at {}".format(ip)):
        res = proc.run(vm.shell_cmd("hp-makeuri", "-c", ip), timeout=30)
    uri = ""
    for line in res.out.splitlines():
        if line.startswith("hp:/net/"):
            uri = "hpaio:" + line[len("hp:"):]
            break
    if not uri:
        ui.die("the VM could not identify a scanner at {}.\n"
               "Check the printer is on and reachable from this network.".format(ip))
    paths.ensure_state_dir()
    with open(cache, "w") as f:
        f.write(uri + "\n")
    return uri


def clear_stale_scan() -> None:
    """Stop a scan left behind by an interrupted run.

    We hold the lock by this point, so no other scanbox run can legitimately be
    scanning -- anything still going is an orphan still owning the printer's
    one scan session, and clearing it is the difference between working and a
    cryptic device I/O error.
    """
    if not proc.run(vm.shell_cmd("pgrep", "-x", "scanimage"), timeout=20).ok:
        return
    ui.warn("a scan from an earlier run is still going inside the VM. Stopping it --\n"
            "      otherwise the printer refuses this one.")
    proc.run(vm.shell_cmd("pkill", "-x", "scanimage"), timeout=20)
    # Only wait for the process to actually go. The printer itself holds the
    # session for ~45s longer, but the guest retries through that window rather
    # than making everyone sit here for it.
    wait = ('for _ in $(seq 1 15); do pgrep -x scanimage >/dev/null || break; '
            'sleep 1; done')
    proc.run(vm.shell_cmd("bash", "-c", wait), timeout=20)


class RemoteScan:
    """A scan in flight inside the VM, and the ability to actually stop it.

    Killing the host process is not enough. `limactl shell` rides lima's
    persistent SSH ControlMaster, which belongs to the hostagent rather than to
    us, so it survives our death; and the session has no TTY, so sshd sends no
    SIGHUP. The guest keeps scanning, keeps holding the printer's one scan
    session, and the next scan dies with "sane_start: Error during device I/O"
    -- with the lock already released, so nothing even suggests a scan is still
    running.

    So the guest writes its process group id to a file named for this run, and
    we kill that group explicitly. The id is per-run, so an abort can only ever
    stop the scan it belongs to.
    """

    def __init__(self) -> None:
        # Built from integers, so it cannot carry anything the guest shell
        # would have to quote.
        self.run_id = "{:d}-{:d}".format(os.getpid(), int(time.time()))
        self._live = True

    def finished(self) -> None:
        self._live = False

    def abort(self, announce: bool = False) -> None:
        if not self._live:
            return
        self._live = False                     # never attempt this twice
        if announce:
            ui.say("stopping the scan inside the VM...")
        pgid_file = "/tmp/scanbox-run-{}.pgid".format(self.run_id)
        script = (
            'f={f}\n'
            '[ -f "$f" ] || exit 0\n'
            'g=$(cat "$f" 2>/dev/null) || exit 0\n'
            '[ -n "$g" ] || exit 0\n'
            'kill -TERM -"$g" 2>/dev/null || true\n'
            'for _ in 1 2 3 4 5; do kill -0 -"$g" 2>/dev/null || break; sleep 1; done\n'
            'kill -KILL -"$g" 2>/dev/null || true\n'
            'rm -f "$f"\n'
        ).format(f=shlex.quote(pgid_file))
        # Bounded and best-effort: the point of this path is that the user
        # wants out now, and the VM may itself be wedged. We are already exiting.
        proc.run(vm.shell_cmd("bash", "-c", script), timeout=20)

    def __enter__(self) -> "RemoteScan":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # An exception on the way out means Ctrl-C or a crash, where the user
        # is owed a word about the round trip. A scan that merely failed has
        # already stopped in the guest, so the abort is a silent no-op.
        self.abort(announce=exc_type is not None)


class ProgressReader:
    """Turns the guest's stdout into a live status line, as it arrives."""

    def __init__(self, spinner: ui.Spinner, base: str) -> None:
        self.spinner = spinner
        self.base = base
        self.summary: List[str] = []
        self.started = time.time()
        self._bucket = -1

    def __call__(self, line: str) -> None:
        if line.startswith("PROGRESS "):
            self._progress(line[len("PROGRESS "):].strip())
        elif line.startswith("PHASE "):
            self.spinner.msg = line[len("PHASE "):].strip()
        elif line.startswith("NOTE "):
            self.spinner.note(line[len("NOTE "):].strip())
        else:
            self.summary.append(line)

    def _progress(self, pct: str) -> None:
        whole_s = pct.split(".")[0].rstrip("%")
        if not whole_s.isdigit():
            return
        whole = int(whole_s)
        # An ETA measured from this scan beats the up-front guess, and it is
        # the thing that actually answers "is this stuck?". It needs both a few
        # percent and a few seconds behind it: extrapolating from 1% of a scan,
        # or from two seconds of one, produces a confident number that is
        # simply wrong, which is worse than showing none.
        eta = ""
        elapsed = int(time.time() - self.started)
        if whole >= 3 and elapsed >= 5:
            left = elapsed * (100 - whole) // whole
            if left > 60:
                eta = ", ~{} min left".format(left // 60)
            elif left > 0:
                eta = ", ~{}s left".format(left)
        self.spinner.msg = "{}  {}{}".format(self.base, pct, eta)
        # Piped output gets no animation, so mark every 10% instead of nothing.
        if not self.spinner.animating and whole // 10 != self._bucket:
            self._bucket = whole // 10
            ui.say("  {}{}".format(pct, eta))


def parse_summary(lines: List[str]) -> Dict[str, List[List[str]]]:
    fields: Dict[str, List[List[str]]] = {}
    for line in lines:
        parts = line.split()
        if parts:
            fields.setdefault(parts[0], []).append(parts[1:])
    return fields


class Options:
    def __init__(self, source: str = "auto", mode: str = "Color", dpi: int = 300,
                 page: str = "auto", lossless: bool = False,
                 name: Optional[str] = None, fmt: str = "pdf",
                 out_dir: Optional[str] = None, keep_alive: int = 60,
                 printer: Optional[str] = None) -> None:
        self.source = source
        self.mode = mode
        self.dpi = dpi
        self.page = page
        self.lossless = lossless
        self.name = name
        self.fmt = fmt
        self.out_dir = out_dir or paths.DEFAULT_OUT_DIR
        self.keep_alive = keep_alive
        self.printer = printer


def run(opts: Options) -> List[str]:
    ip = resolve_printer(opts.printer)
    if not ip:
        ui.die("could not reach the configured scanner.\n"
               "Is the printer on, and are you on its network? "
               "Run 'scanbox setup' to look again.")

    vm.ensure()

    # The lock is the outermost of the three, so it is still held while the
    # remote scan is being torn down: the scanner is busy until the guest
    # process is actually gone, and a lock that reads free before then is what
    # makes the *next* run fail.
    with lock.Lock():
        clear_stale_scan()
        uri = device_uri(ip)
        name = opts.name or time.strftime("scan-%Y%m%d%H%M%S")
        os.makedirs(opts.out_dir, exist_ok=True)

        msg = "scanning"
        if opts.lossless:
            est_mb, est_secs = lossless_estimate(opts.dpi, opts.mode, opts.page)
            if est_secs >= 120:
                ui.say("lossless at {}dpi is about {}MB per page uncompressed --"
                       .format(opts.dpi, est_mb))
                ui.say("expect roughly {} min a page. That is the transfer, not "
                       "a hang.".format(est_secs // 60))
                msg = "scanning (~{}MB/page, ~{} min)".format(est_mb, est_secs // 60)

        errf = tempfile.mktemp(prefix="scanbox-err-")
        try:
            with RemoteScan() as remote:
                args = [uri, opts.source, opts.mode, str(opts.dpi), opts.page,
                        "1" if opts.lossless else "0", name, remote.run_id, opts.fmt]
                with ui.Spinner(msg) as spinner:
                    reader = ProgressReader(spinner, msg)
                    # Keep the guest's stderr: it carries the reason a scan
                    # failed ("the feeder is empty") and any truncation detail.
                    # Swallowing it leaves a bare "scan failed", which tells you
                    # nothing about what to do next.
                    res = proc.run_streaming(
                        vm.shell_cmd("bash", "-s", "--", *args),
                        reader,
                        stdin_path=paths.GUEST_SCAN_SH,
                        stderr_path=errf,
                    )
                if res.ok:
                    # stdout reached EOF with a clean status, so the guest is
                    # done and there is nothing left to abort. On a failure we
                    # deliberately leave the abort armed: EOF alone does not
                    # prove the guest died, only that we stopped hearing it.
                    remote.finished()

            guest_err = _read_text(errf)
            if not res.ok:
                _echo_indented(guest_err)
                ui.die("scan failed")
            if guest_err.strip():
                _echo_indented(guest_err)
        finally:
            _unlink(errf)

        fields = parse_summary(reader.summary)
        for page in fields.get("PAGE", []):
            if len(page) >= 3:
                ui.say("  {}: {} (measured {}in)".format(page[0], page[1], page[2]))

        guest_outs = [p[0] for p in fields.get("OUT", []) if p]
        if not guest_outs:
            ui.die("the VM produced no output")
        used = fields.get("SOURCE", [[""]])[0]
        pages = fields.get("PAGES", [[""]])[0]

        # png/jpeg give one OUT line per page rather than one for the whole
        # scan, so copy however many the guest produced. It already names them
        # from `name`, so its basename is what we want on the host too.
        outs = []
        with ui.Spinner("saving"):
            for guest_out in guest_outs:
                out_path = os.path.join(opts.out_dir, os.path.basename(guest_out))
                copy = proc.run(
                    ["limactl", "copy", "{}:{}".format(vm.NAME, guest_out), out_path],
                    timeout=600)
                if not copy.ok:
                    ui.die("could not copy the scan out of the VM")
                outs.append(out_path)

    vm.idle_timer_arm(opts.keep_alive)

    where = "feeder" if (used and used[0] == "ADF") else "flatbed"
    ui.say("{}, {} page(s)".format(where, pages[0] if pages else "?"))
    # Never let a short batch look like a clean run.
    if "TRUNCATED" in fields:
        ui.say("")
        ui.say("WARNING: the feeder stopped early -- this scan may be missing pages.")
        ui.say("         Check the page count above against what you loaded.")
    return outs


def _read_text(path: str) -> str:
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _echo_indented(text: str) -> None:
    for line in text.splitlines():
        ui.say("  " + line)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
