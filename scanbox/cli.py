"""Argument parsing and the four verbs.

Scanning moves paper, so it needs an explicit `scan` rather than being what you
get for running the command with no arguments.
"""
import argparse
import signal
import sys
from typing import List, Optional

from . import config, discover, paths, scan, ui, vm

USAGE = """\
scanbox -- scan from the network MFP

  scanbox scan [SOURCE]   scan, where SOURCE is one of:
                            auto    the feeder if loaded, else the bed (default)
                            feeder  force the document feeder
                            bed     force the flatbed
  scanbox setup           find a scanner and save it as your config
  scanbox status          VM state, config, resolved printer
  scanbox stop            stop the VM now

Options (for setup)
  --host=NAME       skip discovery and use this scanner
  --overwrite       replace an existing config without asking

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
  --printer HOST    override the configured scanner for this run"""

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
    p.add_argument("--printer")

    p = sub.add_parser("setup", add_help=False)
    _add_help(p)
    p.add_argument("--host")
    p.add_argument("--overwrite", action="store_true")

    _add_help(sub.add_parser("status", add_help=False))
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
        out_dir=args.out_dir, keep_alive=args.keep_alive, printer=args.printer,
    )
    for path in scan.run(opts):
        print(path)
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    # An existing config is confirmed before anything else happens, so a
    # mistyped `setup` cannot cost you a working configuration. Nothing is
    # written until the very end regardless.
    if config.exists() and not args.overwrite:
        ui.say("This is already configured, at {}:".format(config.display_path()))
        ui.say("")
        for line in config.read_raw().splitlines():
            ui.say("    " + line)
        ui.say("")
        if not ui.tty_readable():
            ui.die("Replace it? -- but there is no terminal to ask on. "
                   "Pass --overwrite.")
        if not ui.confirm("Replace it?"):
            ui.die("setup cancelled -- nothing was changed.")
        ui.say("")

    host = args.host or ""
    if not host:
        with ui.Spinner("searching for scanners on the network"):
            names = discover.instances(5)
        found = []
        for name in names:
            with ui.Spinner("resolving {}".format(name)):
                found.append(discover.resolve_instance(name))
        if not found:
            ui.die("no scanners found on this network.\n"
                   "Check the printer is switched on and on the same network "
                   "as this Mac.")

        count = len(found)
        ui.say("Found {} scanner{}:".format(count, "" if count == 1 else "s"))
        ui.say("")
        for i, inst in enumerate(found, 1):
            ui.say("  {}) {}".format(i, inst.model))
            ui.say("     {}{}".format(
                inst.host or "<unresolved>",
                "  (has a document feeder)" if inst.has_feeder else ""))
        ui.say("")

        if not ui.tty_readable():
            ui.die("no terminal to choose on. Re-run with --host=<hostname>.")
        while True:
            prompt = ("Which one? [1] " if count == 1
                      else "Which one? [1-{}] ".format(count))
            choice = ui.ask(prompt)
            # A lone Enter takes the only candidate, but never guesses between
            # several.
            if not choice and count == 1:
                choice = "1"
            if choice.isdigit() and 1 <= int(choice) <= count:
                break
            ui.say("  please enter a number between 1 and {}".format(count))
        host = found[int(choice) - 1].host or ""
        if not host:
            ui.die("that scanner did not resolve to a hostname; "
                   "re-run with --host=<hostname>.")
        ui.say("")

    # Confirm it is actually reachable, but do not refuse to save if it is not
    # -- setting this up while away from the printer's network is legitimate.
    with ui.Spinner("checking {}".format(host)):
        ip = discover.resolve_ipv4(host)
    if ip:
        ui.say("{} resolves to {}".format(host, ip))
    else:
        ui.warn("note: {} does not resolve from here. Saving anyway -- it should "
                "work\n      once you are back on the printer's network.".format(host))

    config.save(host)
    ui.say("")
    ui.say("Saved to {}. Scan with:".format(config.display_path()))
    ui.say("")
    ui.say("    scanbox scan")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    vm.require_lima()
    print("VM          {}".format(vm.status() if vm.exists() else "not created"))
    print("config      {}".format(
        config.path() if config.exists() else "none -- run: scanbox setup"))
    if config.exists():
        print("printer     {}".format(config.printer_label() or "unset"))
        # Not being able to resolve is a normal thing for status to report, not
        # a reason to abort before printing the rest.
        try:
            ip = scan.resolve_printer()
        except ui.ScanboxError:
            ip = None
        print("address     {}".format(
            ip or "<unresolved> (not on this network?)"))
    print("output      {}".format(paths.DEFAULT_OUT_DIR))
    if vm.timer_running():
        print("idle timer  running ({}s since last scan)".format(
            paths.seconds_since_use()))
    else:
        print("idle timer  not running")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    vm.require_lima()
    vm.stop()
    ui.say("VM stopped")
    return 0


HANDLERS = {"scan": cmd_scan, "setup": cmd_setup,
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
