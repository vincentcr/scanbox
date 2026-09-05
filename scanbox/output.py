"""Backend-neutral assembly of acquired raster pages.

Backends stop at producing :class:`~scanbox.contracts.ScanPage` files. This
module applies the user's output policy once, on the host, so native, WSD, and
legacy acquisition cannot drift in naming or container behavior.

The implementation uses tools shipped with macOS: ``sips`` for one-page
conversion, ``tiffutil`` for lossless TIFF compression/joining, and the system
Combine PDF Pages action for PDF joining. All work is staged beside the final
destination and installed only after every page succeeds.
"""
from dataclasses import dataclass
import os
import shutil
import tempfile
from typing import Callable, List, Optional, Sequence, Tuple

from . import proc
from .contracts import ScanMode, ScanPage, ScanResult, ScanSource

SIPS = "/usr/bin/sips"
TIFFUTIL = "/usr/bin/tiffutil"
PDF_JOIN = (
    "/System/Library/Automator/Combine PDF Pages.action/Contents/MacOS/join"
)

FORMATS = ("pdf", "png", "tiff", "jpeg")

EventHandler = Callable[[str, str], None]
Runner = Callable[..., proc.Result]


class OutputError(RuntimeError):
    """Output could not be assembled; no new final result was kept."""


@dataclass(frozen=True)
class OutputOptions:
    """Final-file choices, independent of how pages were acquired."""

    out_dir: str
    name: str
    fmt: Optional[str] = None
    image: bool = False
    split: bool = False
    lossless: bool = False
    mode: ScanMode = ScanMode.COLOR

    def __post_init__(self) -> None:
        if not isinstance(self.out_dir, str) or not self.out_dir:
            raise ValueError("out_dir must be a non-empty string")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name must be a non-empty string")
        if os.path.basename(self.name) != self.name:
            raise ValueError("name must not contain a path")
        if self.fmt is not None:
            value = self.fmt.strip().lower()
            if value == "jpg":
                value = "jpeg"
            if value not in FORMATS:
                raise ValueError("unknown output format: {!r}".format(self.fmt))
            object.__setattr__(self, "fmt", value)
        for field in ("image", "split", "lossless"):
            if not isinstance(getattr(self, field), bool):
                raise ValueError("{} must be a bool".format(field))
        object.__setattr__(self, "mode", ScanMode.parse(self.mode))


def choose_format(source: ScanSource, options: OutputOptions) -> str:
    """Resolve the smart image format after the actual source is known."""
    source = ScanSource.parse(source)
    if source is ScanSource.AUTO:
        raise ValueError("completed output needs the source actually used")
    if options.fmt:
        return options.fmt
    if not options.image:
        return "pdf"
    if (source in (ScanSource.FEEDER, ScanSource.FEEDER_DUPLEX)
            and not options.split):
        return "tiff"
    if options.lossless or options.mode is ScanMode.LINEART:
        return "png"
    return "jpeg"


def output_paths(result: ScanResult, options: OutputOptions) -> Tuple[str, ...]:
    """Return final names without touching the filesystem."""
    fmt = choose_format(result.source, options)
    extension = "jpg" if fmt == "jpeg" else fmt
    per_page = options.split or fmt in ("png", "jpeg")
    count = len(result.pages) if per_page else 1
    paths = []
    for index in range(1, count + 1):
        suffix = "" if count == 1 else "-p{:03d}".format(index)
        paths.append(os.path.join(options.out_dir,
                                  options.name + suffix + "." + extension))
    return tuple(paths)


def assemble(result: ScanResult, options: OutputOptions, *,
             on_event: Optional[EventHandler] = None,
             runner: Runner = proc.run) -> Tuple[str, ...]:
    """Build and atomically install final output, then remove staged pages.

    Conversion failure happens entirely in a hidden staging directory. The
    original acquired pages are removed in all cases; they are temporary
    backend artifacts, not a second user-visible result.
    """
    if not isinstance(result, ScanResult):
        raise ValueError("result must be a ScanResult")
    if not isinstance(options, OutputOptions):
        raise ValueError("options must be OutputOptions")
    event = on_event or (lambda _kind, _message: None)
    pages = tuple(result.pages)
    stage = None
    try:
        for page in pages:
            if not os.path.isfile(page.path):
                raise OutputError("acquired page is missing: {}".format(page.path))

        final_paths = output_paths(result, options)
        source_paths = {os.path.abspath(page.path) for page in pages}
        if any(os.path.abspath(path) in source_paths for path in final_paths):
            raise OutputError("output path would overwrite an acquired page")

        os.makedirs(options.out_dir, exist_ok=True)
        stage = tempfile.mkdtemp(prefix=".scanbox-output-", dir=options.out_dir)
        fmt = choose_format(result.source, options)
        _announce(event, fmt, len(pages))
        staged = _build(stage, pages, final_paths, fmt, options.split, event, runner)
        _install(staged, final_paths, stage)
        return final_paths
    except OutputError:
        raise
    except (OSError, ValueError) as error:
        raise OutputError("could not save scan: {}".format(error))
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        _cleanup_pages(pages)


def _announce(event: EventHandler, fmt: str, count: int) -> None:
    label = {"pdf": "PDF", "png": "PNG", "tiff": "TIFF", "jpeg": "JPEG"}[fmt]
    noun = "page" if count == 1 else "pages"
    event("phase", "building the {} ({} {})".format(label, count, noun))


def _build(stage: str, pages: Sequence[ScanPage], final_paths: Sequence[str],
           fmt: str, split: bool, event: EventHandler,
           runner: Runner) -> Tuple[str, ...]:
    per_page = split or fmt in ("png", "jpeg")
    extension = "jpg" if fmt == "jpeg" else fmt
    converted: List[str] = []
    for index, page in enumerate(pages, 1):
        event("progress", "assembling page {} of {}".format(index, len(pages)))
        target = os.path.join(stage, "page-{:04d}.{}".format(index, extension))
        if fmt == "png":
            if page.media_type == "image/png":
                shutil.copyfile(page.path, target)
            else:
                _sips(page.path, target, "png", runner)
        elif fmt == "jpeg":
            _sips(page.path, target, "jpeg", runner, quality="92")
        elif fmt == "pdf":
            _sips(page.path, target, "pdf", runner)
        else:
            raw = os.path.join(stage, "raw-{:04d}.tiff".format(index))
            _sips(page.path, raw, "tiff", runner)
            _command([TIFFUTIL, "-lzw", raw, "-out", target], runner)
        converted.append(target)

    if per_page:
        staged = []
        for source, final in zip(converted, final_paths):
            target = os.path.join(stage, os.path.basename(final))
            os.replace(source, target)
            staged.append(target)
        return tuple(staged)

    target = os.path.join(stage, os.path.basename(final_paths[0]))
    if fmt == "pdf":
        _command([PDF_JOIN, "--output", target, *converted], runner, timeout=600)
    elif fmt == "tiff":
        _command([TIFFUTIL, "-cat", *converted, "-out", target], runner, timeout=600)
    else:  # png/jpeg are always per-page
        raise OutputError("{} cannot contain multiple pages".format(fmt))
    return (target,)


def _sips(source: str, target: str, fmt: str, runner: Runner,
          quality: Optional[str] = None) -> None:
    command = [SIPS, "--setProperty", "format", fmt]
    if quality is not None:
        command.extend(["--setProperty", "formatOptions", quality])
    command.extend([source, "--out", target])
    _command(command, runner, timeout=600)


def _command(command: List[str], runner: Runner,
             timeout: float = 300) -> None:
    result = runner(command, timeout=timeout)
    if result.ok:
        return
    detail = (result.err or result.out).strip().splitlines()
    message = detail[-1] if detail else "exit {}".format(result.code)
    raise OutputError("{} failed: {}".format(os.path.basename(command[0]), message))


def _install(staged: Sequence[str], final: Sequence[str], stage: str) -> None:
    """Install all files, restoring pre-existing outputs if a rename fails."""
    backups: List[Tuple[str, str]] = []
    installed: List[str] = []
    try:
        for index, path in enumerate(final):
            if os.path.exists(path):
                backup = os.path.join(stage, "backup-{:04d}".format(index))
                os.replace(path, backup)
                backups.append((backup, path))
        for source, path in zip(staged, final):
            os.replace(source, path)
            installed.append(path)
    except OSError:
        for path in installed:
            try:
                os.unlink(path)
            except OSError:
                pass
        for backup, path in backups:
            try:
                os.replace(backup, path)
            except OSError:
                pass
        raise


def _cleanup_pages(pages: Sequence[ScanPage]) -> None:
    parents = set()
    for page in pages:
        parents.add(os.path.dirname(page.path))
        try:
            os.unlink(page.path)
        except OSError:
            pass
    # Only remove empty staging directories. Never recursively remove a path
    # supplied by a backend contract.
    for parent in parents:
        try:
            os.rmdir(parent)
        except OSError:
            pass
