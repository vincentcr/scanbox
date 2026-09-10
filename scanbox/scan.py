"""User-facing scan orchestration over discovered and remembered scanners."""
import os
import shutil
import time
from typing import List, Optional, Tuple

from . import config, output, paths, selection, ui
from .backends.hplip import HPLIPError
from .contracts import BackendError, ScanMode, ScanRequest, ScanSource
from .routing import Router

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


class ProgressDisplay:
    """Translate normalized backend events into the live display."""

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
                 scanner: Optional[str] = None,
                 backend: Optional[str] = None) -> None:
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
        self.scanner = scanner
        self.backend = backend


class _EventRelay:
    def __init__(self, target) -> None:
        self.target = target

    def __call__(self, kind: str, value: str) -> None:
        self.target(kind, value)


def _target_events():
    discovery_spinner = None

    def discovery_event(kind: str, value: str) -> None:
        nonlocal discovery_spinner
        if kind == "begin":
            discovery_spinner = ui.Spinner(value)
            discovery_spinner.__enter__()
        elif kind == "end" and discovery_spinner is not None:
            discovery_spinner.stop()
            discovery_spinner = None

    return discovery_event, lambda: discovery_spinner


def _discover(catalog=None):
    catalog = catalog or selection.current_network_catalog()
    with ui.Spinner("searching for scanners on this network"):
        inventory = catalog.discover()
    for failure in inventory.failures:
        ui.warn("{} discovery: {}".format(failure.backend, failure.message))
    return inventory


def _prepare_group(opts: Options, group: selection.PhysicalCandidate,
                   backend_preference: Optional[str] = None, on_event=None):
    diagnostics = []
    variants = selection.candidate_variants(group, backend_preference)
    if not variants:
        ui.die("{} is not available via {}".format(group.name, backend_preference))
    for candidate in variants:
        request = _request(opts, candidate.scanner.id)
        candidate.backend.on_event = on_event or (lambda _kind, _value: None)
        try:
            job = candidate.backend.prepare(candidate.scanner, request)
            for diagnostic in diagnostics:
                ui.say("  routing: " + diagnostic)
            ui.say("using {} via {}".format(
                candidate.scanner.name, candidate.backend.name
            ))
            return candidate.backend, candidate.scanner, job
        except (BackendError, ValueError) as error:
            diagnostics.append("rejected backend {}: {}".format(
                candidate.backend.name, error
            ))
    ui.die("; ".join(diagnostics))


def _current_network_target(opts: Options, catalog=None, on_event=None):
    if opts.backend == "imagecapture":
        ui.die("ImageCapture scanning is not available yet; use auto, wsd, or hplip")
    inventory = _discover(catalog)
    group = selection.select_group(
        inventory.physical,
        opts.scanner or "auto",
        interactive=ui.tty_readable(),
        ask=ui.ask,
        say=ui.say,
    )
    return _prepare_group(opts, group, opts.backend, on_event)


def _request(opts: Options, scanner_id: str) -> ScanRequest:
    return ScanRequest(
        scanner_id,
        source=ScanSource.parse(opts.source),
        mode=ScanMode.parse(opts.mode),
        resolution=opts.dpi,
        page_size=opts.page,
        lossless=opts.lossless,
    )


def _saved_target(opts: Options, *, router=None, on_event=None):
    registry = config.load_registry()
    if not registry.scanners:
        ui.die("no scanners saved yet. Run:\n\n    scanbox scanners --save")

    # Remembered scanners already have a concrete, validated backend. Preparing
    # them directly avoids paying for unrelated discovery on every scan. The
    # preferred entry is tried first; every failure happens before acquisition.
    active_router = router or Router(on_event=on_event)
    diagnostics = []
    for saved in registry.ordered():
        request = _request(opts, saved.id or saved.locator or saved.key)
        try:
            route = active_router.prepare(
                saved, request, backend_preference=opts.backend or saved.backend
            )
            for diagnostic in route.diagnostics:
                ui.say("  routing: " + diagnostic)
            ui.say("using {} via {}".format(route.scanner.name, route.backend_name))
            return route.backend, route.scanner, route.job
        except (BackendError, ValueError) as error:
            diagnostics.append("{}: {}".format(saved.label, error))
    ui.die("none of the saved scanners is reachable ({})".format(
        "; ".join(diagnostics)
    ))


def run(opts: Options, *, catalog=None, router=None) -> List[str]:
    backend = scanner = None
    result = None
    display = None
    target_event, active_spinner = _target_events()
    relay = _EventRelay(target_event)

    def on_event(kind: str, value: str) -> None:
        if display is not None:
            display(kind, value)

    try:
        try:
            if opts.scanner is not None:
                backend, scanner, job = _current_network_target(
                    opts, catalog, relay
                )
            else:
                backend, scanner, job = _saved_target(
                    opts, router=router, on_event=relay
                )
        finally:
            spinner = active_spinner()
            if spinner is not None:
                spinner.stop()

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

        relay.target = on_event
        backend.on_event = relay
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
