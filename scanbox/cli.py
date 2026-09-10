"""Argument parsing for scanbox commands.

Scanning moves paper, so it needs an explicit `scan` rather than being what you
get for running the command with no arguments.
"""
import argparse
import signal
import sys
from typing import List, Optional

from . import config, discover, paths, scan, selection, ui, vm

USAGE = """\
scanbox -- scan from the network MFP

  scanbox scan [SOURCE]   scan, where SOURCE is one of:
                            auto    the feeder if loaded, else the bed (default)
                            feeder  force the document feeder
                            bed     force the flatbed
  scanbox scanners        discover, save, and manage scanners
  scanbox status          VM state and saved-scanner configuration
  scanbox stop            stop the VM now

Options (for scanners)
  --save [NAME]     discover, validate, and remember a scanner
  --saved           show remembered scanners without using the network
  --forget NAME     remove a remembered scanner
  --prefer NAME     choose the tie-breaker among saved scanners
  --preferred       make the scanner being saved the preferred one
  --host NAME       with --save, remember a manually configured host
  --backend B       filter discovery or set a manual host's backend

Options (for scan)
  --out DIR         where scans land       (default ~/Pictures/Scans)
  --name NAME       base filename          (default scan-YYYYMMDDHHMMSS)
  --dpi N           resolution             (default 300; --image: 600; 75..1200)
  --mode M          Color|Gray|Lineart     (default Color)
  --page P          auto|letter|legal|a4|max  (default auto)
  --image           save images, choosing TIFF/PNG/JPEG automatically
  --split           save one output file per page
  --format F        choose the exact pdf|png|tiff|jpeg format
  --lossless        disable the scanner's in-transit JPEG compression
  --keep-alive MIN  idle minutes before the VM stops (default 60)
  --scanner NAME    use a current-network scanner by name or stable ID;
                    use auto to select or prompt without changing config
  --backend B       override auto|wsd|hplip|imagecapture for this run"""

# What the user types, and what SANE calls it.
SOURCES = {"auto": "auto", "feeder": "ADF", "bed": "Flatbed", "flatbed": "Flatbed"}


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 and prints its own usage; scanbox exits 1 and says why."""

    def error(self, message: str) -> None:
        ui.die("{} (try --help)".format(message))


class _UsageAction(argparse.Action):
    """Print the one hand-written usage, whichever subcommand asked for it.

    Registered as a real argparse action rather than grepped out of argv, so
    that `--name -h` still means a file called "-h" -- argparse consumes it as
    the option's value, as it should.
    """

    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        print(USAGE)
        parser.exit(0)


def _add_help(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-h", "--help", action=_UsageAction, default=argparse.SUPPRESS)


def build_parser() -> _Parser:
    parser = _Parser(prog="scanbox", add_help=False)
    _add_help(parser)
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("scan", add_help=False)
    _add_help(p)
    p.add_argument("source", nargs="?", default="auto", choices=sorted(SOURCES))
    p.add_argument("--out", dest="out_dir")
    p.add_argument("--name")
    # No range check on dpi: the help says 75..1200 because that is what the
    # M276nw does, but this is meant to work on other pre-eSCL HP MFPs too, and
    # refusing a resolution some other model supports would be a guess. The
    # scanner's own error is the authority.
    # Keep omission distinct from an explicit 300: image output gets a higher
    # default, but an explicit --dpi always wins.
    p.add_argument("--dpi", type=int)
    p.add_argument("--mode", default="Color", choices=["Color", "Gray", "Lineart"])
    p.add_argument("--page", default="auto",
                   choices=["auto", "letter", "legal", "a4", "max"])
    output = p.add_mutually_exclusive_group()
    output.add_argument("--image", action="store_true")
    output.add_argument("--format", dest="fmt",
                        choices=["pdf", "png", "tiff", "jpeg"])
    p.add_argument("--split", action="store_true")
    p.add_argument("--lossless", action="store_true")
    p.add_argument("--keep-alive", dest="keep_alive", type=int, default=60)
    p.add_argument("--scanner")
    p.add_argument("--backend", choices=config.BACKENDS)

    _add_help(sub.add_parser("status", add_help=False))
    p = sub.add_parser("scanners", add_help=False)
    _add_help(p)
    operation = p.add_mutually_exclusive_group()
    operation.add_argument("--save", nargs="?", const="auto", metavar="NAME")
    operation.add_argument("--saved", action="store_true")
    operation.add_argument("--forget", metavar="NAME")
    operation.add_argument("--prefer", metavar="NAME")
    p.add_argument("--preferred", action="store_true")
    p.add_argument("--host")
    p.add_argument("--backend", choices=config.BACKENDS)
    _add_help(sub.add_parser("stop", add_help=False))

    p = sub.add_parser("__idle-timer", add_help=False)
    p.add_argument("minutes", nargs="?", type=int, default=60)
    return parser


def cmd_scan(args: argparse.Namespace) -> int:
    # Lossless strips the scanner's in-transit JPEG only to have it re-encoded
    # on disk -- not wrong, exactly, but not what anyone asking for lossless
    # output meant either.
    if args.lossless and args.fmt == "jpeg":
        ui.warn("--lossless with --format jpeg pays for an uncompressed transfer "
                "and then re-compresses it on disk anyway. Proceeding.")
    dpi = args.dpi if args.dpi is not None else (600 if args.image else 300)
    opts = scan.Options(
        source=SOURCES[args.source], mode=args.mode, dpi=dpi, page=args.page,
        lossless=args.lossless, name=args.name, fmt=args.fmt,
        image=args.image, split=args.split,
        out_dir=args.out_dir, keep_alive=args.keep_alive,
        scanner=args.scanner, backend=args.backend,
    )
    for path in scan.run(opts):
        print(path)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    vm.require_lima()
    print("VM          {}".format(vm.status() if vm.exists() else "not created"))
    print("config      {}".format(
        config.path() if config.exists() else "none -- run: scanbox scanners --save"))
    registry = config.load_registry()
    print("scanners    {} saved".format(len(registry.scanners)))
    if registry.preferred_scanner is not None:
        print("preferred   {}".format(registry.preferred_scanner.label))
    print("output      {}".format(paths.DEFAULT_OUT_DIR))
    if vm.timer_running():
        print("idle timer  running ({}s since last scan)".format(
            paths.seconds_since_use()))
    else:
        print("idle timer  not running")
    return 0


def cmd_scanners(args: argparse.Namespace) -> int:
    if args.preferred and args.save is None:
        ui.die("--preferred requires --save")
    if args.host and args.save is None:
        ui.die("--host requires --save")
    if args.saved:
        _print_saved(config.load_registry())
        return 0
    if args.forget:
        before = config.load_registry()
        removed = next(
            (scanner for scanner in before.scanners
             if args.forget.casefold() in {
                 scanner.key.casefold(), (scanner.id or "").casefold(),
                 scanner.label.casefold(), (scanner.locator or "").casefold(),
             }), None,
        )
        config.forget(args.forget)
        print("Forgot {}.".format(removed.label if removed else args.forget))
        return 0
    if args.prefer:
        registry = config.set_preferred(args.prefer)
        print("Preferred scanner: {}.".format(registry.preferred_scanner.label))
        return 0

    if args.host:
        if args.backend not in ("wsd", "hplip"):
            ui.die("a manual --host requires --backend wsd or --backend hplip")
        scanner = config.ConfiguredScanner(
            name=None if args.save == "auto" else args.save,
            host=None if discover.is_ipv4(args.host) else args.host,
            address=args.host if discover.is_ipv4(args.host) else None,
            backend=args.backend,
        )
        config.remember(scanner, preferred=args.preferred)
        print("Saved {} via {}.".format(scanner.label, scanner.backend))
        return 0

    catalog = selection.current_network_catalog()
    with ui.Spinner("searching for scanners on this network"):
        inventory = catalog.discover()
    for failure in inventory.failures:
        ui.warn("{} discovery: {}".format(failure.backend, failure.message))
    groups = inventory.physical
    if args.backend not in (None, "auto"):
        groups = tuple(
            selection.PhysicalCandidate(group.identity, tuple(
                candidate for candidate in group.candidates
                if candidate.backend.name == args.backend
            ))
            for group in groups
            if any(candidate.backend.name == args.backend for candidate in group.candidates)
        )
    if not groups:
        print("No scanners found on this network.")
        return 0

    if args.save is not None:
        group = selection.select_group(
            groups, args.save, interactive=ui.tty_readable(),
            ask=ui.ask, say=ui.say,
        )
        candidate = None
        try:
            with ui.Spinner("checking {} without scanning".format(group.name)):
                candidate = selection.validate(group, args.backend)
            scanner = selection.configured(candidate)
            config.remember(scanner, preferred=args.preferred)
        finally:
            if candidate is not None:
                release = getattr(candidate.backend, "release", None)
                if release is not None:
                    release(60)
        print("Saved {} via {}.".format(scanner.label, scanner.backend))
        return 0

    registry = config.load_registry()
    for index, group in enumerate(groups):
        if index:
            print("")
        saved = next(
            (item for item in registry.scanners
             if selection.matches_saved(group, item)), None,
        )
        marker = "* " if saved and saved.key == registry.preferred else "  "
        print(marker + group.name)
        print("    id        {}".format(group.display_id))
        print("    backends  {}".format(", ".join(group.backend_names)))
        if saved:
            print("    saved     yes{}".format(
                " (preferred)" if saved.key == registry.preferred else ""
            ))
    return 0


def _print_saved(registry: config.ScannerRegistry) -> None:
    if not registry.scanners:
        print("No saved scanners.")
        return
    for index, scanner in enumerate(registry.scanners):
        if index:
            print("")
        marker = "* " if scanner.key == registry.preferred else "  "
        print(marker + scanner.label)
        print("    id       {}".format(scanner.id or "<not advertised>"))
        print("    backend  {}".format(scanner.backend))
        print("    locator  {}".format(scanner.locator or "<discover by identity>"))


def cmd_stop(args: argparse.Namespace) -> int:
    vm.require_lima()
    vm.stop()
    ui.say("VM stopped")
    return 0


HANDLERS = {"scan": cmd_scan, "scanners": cmd_scanners,
            "status": cmd_status, "stop": cmd_stop}


def _on_term(signum, frame) -> None:
    """Route SIGTERM through the same teardown as Ctrl-C.

    Every context manager on the way out matters here: the spinner restores the
    cursor, the remote scan is stopped inside the VM, and only then is the lock
    released. An exception is what unwinds them, so raising is the handler.
    """
    raise KeyboardInterrupt()


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv:
        print(USAGE)
        return 0
    if argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        ui.warn("no command given -- did you mean 'scanbox scan {}'? "
                "(try --help)".format(argv[0]))
        return 1

    signal.signal(signal.SIGTERM, _on_term)
    try:
        args = build_parser().parse_args(argv)
        if args.cmd == "__idle-timer":
            vm.idle_timer_run(args.minutes)
            return 0
        return HANDLERS[args.cmd](args)
    except ui.ScanboxError as e:
        ui.warn(str(e))
        return 1
    except ValueError as e:
        ui.warn(str(e))
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # `scanbox scan | head` closes the pipe under us. Not an error, and
        # unlike the shell version -- where an untrapped SIGPIPE skipped the
        # cleanup and leaked the lock directory -- the context managers on the
        # way out still run.
        return 141
    finally:
        ui.show_cursor()
