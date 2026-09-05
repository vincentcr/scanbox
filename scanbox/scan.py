"""User-facing scan orchestration over normalized acquisition backends.

The configured-default path remains the legacy HPLIP compatibility behavior.
``--scanner`` instead builds a temporary current-network inventory and never
reads or writes that default. Intelligent protocol preference remains the
router's job; at this stage each discovered candidate already names its usable
backend.
"""
import os
import shutil
import time
from typing import List, Optional, Tuple

from . import config, discover, output, paths, selection, ui
from .backends.hplip import HPLIPBackend, HPLIPError
from .contracts import BackendError, ScanMode, ScanRequest, ScanSource

LOSSLESS_RATE = 550000
PAGE_INCHES = {
    "letter": (8.5, 11.0),
    "legal": (8.5, 14.0),
    "a4": (8.27, 11.69),
}
BED_INCHES = (8.5, 11.69)
BITS_PER_PIXEL = {"Color": 24, "Gray": 8, "Lineart": 1}


def lossless_estimate(dpi: int, mode: str, page: str) -> Tuple[int, int]:
    """Megabytes on the wire for one page, and how many seconds that takes."""
    w_in, h_in = PAGE_INCHES.get(page, BED_INCHES)
    bpp = BITS_PER_PIXEL.get(mode, 1)
    total = (w_in * dpi) * (h_in * dpi) * bpp / 8.0
    return int(round(total / 1000000)), int(round(total / LOSSLESS_RATE))


def resolve_printer(override: Optional[str] = None) -> Optional[str]:
    """The configured scanner's IPv4 address, or None if it will not resolve."""
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
        with ui.Spinner("looking up {}".format(host)):
            ip = discover.resolve_ipv4(host) or ""
    return ip or None


class ProgressDisplay:
    """Translate normalized backend events into the legacy live display."""

    def __init__(self, spinner: ui.Spinner, base: str) -> None:
        self.spinner = spinner
        self.base = base
        self.started = time.time()
        self._bucket = -1

    def __call__(self, kind: str, value: str) -> None:
        if kind == "progress":
            self._progress(value)
        elif kind == "phase":
            self.spinner.msg = value
        elif kind == "copy":
            self.spinner.msg = value
            if not self.spinner.animating:
                ui.say(value + "...")
        elif kind in ("note", "warning"):
            self.spinner.note(value)

    def _progress(self, pct: str) -> None:
        whole_s = pct.split(".")[0].rstrip("%")
        if not whole_s.isdigit():
            return
        whole = int(whole_s)
        eta = ""
        elapsed = int(time.time() - self.started)
        if whole >= 3 and elapsed >= 5:
            left = elapsed * (100 - whole) // whole
            if left > 60:
                eta = ", ~{} min left".format(left // 60)
            elif left > 0:
                eta = ", ~{}s left".format(left)
        self.spinner.msg = "{}  {}{}".format(self.base, pct, eta)
        if not self.spinner.animating and whole // 10 != self._bucket:
            self._bucket = whole // 10
            ui.say("  {}{}".format(pct, eta))


class Options:
    def __init__(self, source: str = "auto", mode: str = "Color", dpi: int = 300,
                 page: str = "auto", lossless: bool = False,
                 name: Optional[str] = None, fmt: Optional[str] = None,
                 image: bool = False, split: bool = False,
                 out_dir: Optional[str] = None, keep_alive: int = 60,
                 printer: Optional[str] = None,
                 scanner: Optional[str] = None) -> None:
        self.source = source
        self.mode = mode
        self.dpi = dpi
        self.page = page
        self.lossless = lossless
        self.name = name
        self.fmt = fmt or ("auto" if image else "pdf")
        self.image = image
        self.split = split
        self.out_dir = out_dir or paths.DEFAULT_OUT_DIR
        self.keep_alive = keep_alive
        self.printer = printer
        self.scanner = scanner


def _legacy_target(opts: Options):
    discovery_spinner = None

    def discovery_event(kind: str, value: str) -> None:
        nonlocal discovery_spinner
        if kind == "begin":
            discovery_spinner = ui.Spinner(value)
            discovery_spinner.__enter__()
        elif kind == "end" and discovery_spinner is not None:
            discovery_spinner.stop()
            discovery_spinner = None

    ip = resolve_printer(opts.printer)
    if not ip:
        ui.die("could not reach the configured scanner.\n"
               "Is the printer on, and are you on its network? "
               "Run 'scanbox setup' to look again, or use "
               "'scanbox scan --scanner auto' on this network.")
    backend = HPLIPBackend(ip, on_event=discovery_event)
    try:
        scanner = backend.discover()[0]
        return backend, scanner
    finally:
        if discovery_spinner is not None:
            discovery_spinner.stop()


def _current_network_target(opts: Options, catalog=None):
    catalog = catalog or selection.current_network_catalog()
    with ui.Spinner("searching for usable scanners on this network"):
        inventory = catalog.discover()
    for failure in inventory.failures:
        ui.warn("{} discovery: {}".format(failure.backend, failure.message))
    candidate = selection.select(
        inventory.candidates,
        opts.scanner or "auto",
        interactive=ui.tty_readable(),
        ask=ui.ask,
        say=ui.say,
    )
    ui.say("using {} via {}".format(
        candidate.scanner.name, candidate.scanner.backend
    ))
    return candidate.backend, candidate.scanner


def run(opts: Options, *, catalog=None) -> List[str]:
    backend = scanner = None
    result = None
    try:
        if opts.scanner is not None:
            backend, scanner = _current_network_target(opts, catalog)
        else:
            backend, scanner = _legacy_target(opts)

        name = opts.name or time.strftime("scan-%Y%m%d%H%M%S")
        msg = "scanning"
        if opts.lossless:
            est_mb, est_secs = lossless_estimate(opts.dpi, opts.mode, opts.page)
            if est_secs >= 120:
                ui.say("lossless at {}dpi is about {}MB per page uncompressed --"
                       .format(opts.dpi, est_mb))
                ui.say("expect roughly {} min a page. That is the transfer, not "
                       "a hang.".format(est_secs // 60))
                msg = "scanning (~{}MB/page, ~{} min)".format(
                    est_mb, est_secs // 60
                )

        request = ScanRequest(
            scanner.id,
            source=ScanSource.parse(opts.source),
            mode=ScanMode.parse(opts.mode),
            resolution=opts.dpi,
            page_size=opts.page,
            lossless=opts.lossless,
        )
        display = None

        def on_event(kind: str, value: str) -> None:
            if display is not None:
                display(kind, value)

        backend.on_event = on_event
        job = backend.prepare(scanner, request)
        with ui.Spinner(msg) as spinner:
            display = ProgressDisplay(spinner, msg)
            result = job.scan()
        display = None

        for diagnostic in getattr(job, "diagnostics", ()):
            ui.say("  " + diagnostic)
        for measurement in getattr(job, "measurements", ()):
            ui.say("  " + measurement)

        output_options = output.OutputOptions(
            out_dir=opts.out_dir,
            name=name,
            fmt=None if opts.fmt == "auto" else opts.fmt,
            image=opts.image,
            split=opts.split,
            lossless=opts.lossless,
            mode=opts.mode,
        )
        with ui.Spinner("saving") as spinner:
            def on_output_event(kind: str, value: str) -> None:
                spinner.msg = value
                if kind == "progress" and not spinner.animating:
                    ui.say("  " + value)

            outs = list(output.assemble(
                result, output_options, on_event=on_output_event
            ))

        release = getattr(backend, "release", None)
        if release is not None:
            release(opts.keep_alive)
        where = result.source.value
        ui.say("{}, {} page(s)".format(where, len(result.pages)))
        if result.truncated:
            ui.say("")
            ui.say("WARNING: the feeder stopped early -- this scan may be missing pages.")
            ui.say("         Check the page count above against what you loaded.")
        return outs
    except HPLIPError as error:
        for diagnostic in error.diagnostics:
            ui.say("  " + diagnostic)
        ui.die(str(error))
    except (BackendError, output.OutputError, ValueError) as error:
        ui.die(str(error))
    finally:
        if result is not None and result.pages:
            shutil.rmtree(os.path.dirname(result.pages[0].path), ignore_errors=True)
