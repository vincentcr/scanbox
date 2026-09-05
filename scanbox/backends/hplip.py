"""Legacy HP acquisition through HPLIP's ``hpaio`` SANE backend.

This module is the compatibility boundary around the original scanbox path.
It deliberately owns every HP-specific detail: ``hp-makeuri``, the Lima
guest, HPLIP's source/mode spellings, the guest line protocol, stale-session
cleanup, and remote cancellation.  Callers receive the same normalized PNG
pages as every other backend.

Discovery here is intentionally configured-address-only.  Constructing this
backend is an explicit request to use the legacy HP path; it never contributes
candidates to native or current-network discovery.
"""
import os
import re
import shlex
import shutil
import tempfile
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .. import lock, paths, proc, vm
from ..contracts import (
    Backend,
    BackendError,
    BackendErrorCode,
    Capabilities,
    ScanJob,
    ScanMode,
    ScanPage,
    ScanRequest,
    ScanResult,
    Scanner,
    ScanSource,
    SourceCapabilities,
    UnsupportedRequest,
)

BACKEND_NAME = "hplip-legacy"

EventHandler = Callable[[str, str], None]
Runner = Callable[..., proc.Result]
StreamingRunner = Callable[..., proc.Result]

_OPTION_RE = re.compile(r"^\s*(?:-[A-Za-z],\s*)?--([\w-]+)\s+(.+?)\s*$")
_RANGE_RE = re.compile(r"^(\d+)\.\.(\d+)(?:dpi)?(?:\s+\(in steps of\s+(\d+))?.*$")
_HP_NAME_RE = re.compile(r"\b(?:hp|hewlett(?:[ -]packard)?)\b", re.IGNORECASE)


class HPLIPError(BackendError):
    """Backend failure plus diagnostics already produced by scanimage."""

    def __init__(self, code: BackendErrorCode, message: str, *,
                 diagnostics: Sequence[str] = (), retryable: bool = False) -> None:
        super().__init__(code, message, backend=BACKEND_NAME, retryable=retryable)
        self.diagnostics = tuple(line for line in diagnostics if line)


def hpaio_uri(output: str) -> Optional[str]:
    """Extract and convert hp-makeuri's scanner URI."""
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("hp:/net/"):
            return "hpaio:" + line[len("hp:"):]
    return None


def parse_summary(lines: Iterable[str]) -> Dict[str, List[List[str]]]:
    fields: Dict[str, List[List[str]]] = {}
    for line in lines:
        parts = line.split()
        if parts:
            fields.setdefault(parts[0], []).append(parts[1:])
    return fields


def _option_values(output: str, option: str) -> Tuple[str, ...]:
    for line in output.splitlines():
        match = _OPTION_RE.match(line)
        if not match or match.group(1) != option:
            continue
        value = re.sub(r"\s+\[[^]]*\]\s*$", "", match.group(2)).strip()
        if "|" in value:
            return tuple(part.strip() for part in value.split("|") if part.strip())
        return (value,) if value else ()
    return ()


def _resolutions(output: str) -> Tuple[int, ...]:
    values = _option_values(output, "resolution")
    result = []
    for value in values:
        match = re.match(r"^(\d+)", value)
        if match:
            result.append(int(match.group(1)))
    if len(values) == 1:
        match = _RANGE_RE.match(values[0])
        if match:
            low, high = int(match.group(1)), int(match.group(2))
            step = int(match.group(3) or 1)
            if step > 0 and high >= low and (high - low) // step <= 2400:
                result = list(range(low, high + 1, step))
    return tuple(sorted(set(result)))


def _source_name(source: ScanSource) -> str:
    try:
        return {
            ScanSource.AUTO: "auto",
            ScanSource.FLATBED: "Flatbed",
            ScanSource.FEEDER: "ADF",
        }[source]
    except KeyError:
        raise UnsupportedRequest(
            "the legacy HPLIP backend does not support source {}".format(source.value)
        )


def _mode_name(mode: ScanMode) -> str:
    return {
        ScanMode.COLOR: "Color",
        ScanMode.GRAYSCALE: "Gray",
        ScanMode.LINEART: "Lineart",
    }[mode]


def _scanner_name(uri: str) -> str:
    model = uri.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return model.replace("_", " ") or "HP scanner"


def supports_configured(scanner) -> bool:
    """Whether a saved physical identity is a plausible HPLIP target.

    Old configurations contain only a hostname or address, so they must be
    probed to preserve compatibility.  New configurations normally include a
    model name; a positively identified non-HP device must not trigger HPLIP
    provisioning merely because another protocol failed.  Keeping this vendor
    rule here prevents HP knowledge from leaking into the shared router.
    """
    identity = (getattr(scanner, "id", None) or "").strip().lower()
    name = (getattr(scanner, "name", None) or "").strip()
    if identity.startswith("hpaio:"):
        return True
    if not name:
        return True
    return bool(_HP_NAME_RE.search(name))


class HPLIPBackend(Backend):
    """One explicitly configured HP scanner accessed through the Lima guest."""

    def __init__(self, address: str, *,
                 on_event: Optional[EventHandler] = None,
                 ensure_guest: Callable[[], None] = vm.ensure,
                 runner: Runner = proc.run,
                 streaming_runner: StreamingRunner = proc.run_streaming) -> None:
        if not isinstance(address, str) or not address.strip():
            raise ValueError("address must be a non-empty string")
        self.address = address.strip()
        self.on_event = on_event or (lambda _kind, _value: None)
        self._ensure_guest = ensure_guest
        self._run = runner
        self._run_streaming = streaming_runner
        self._scanner: Optional[Scanner] = None
        self._capabilities: Optional[Capabilities] = None

    @property
    def name(self) -> str:
        return BACKEND_NAME

    def discover(self) -> Sequence[Scanner]:
        if self._scanner is not None:
            return (self._scanner,)
        self._ensure_guest()
        uri = self._device_uri()
        self._scanner = Scanner(
            id=uri,
            name=_scanner_name(uri),
            backend=self.name,
            endpoint=uri,
            manufacturer="HP",
            transport="network-hplip",
        )
        return (self._scanner,)

    def _device_uri(self) -> str:
        cache = paths.uri_cache(self.address)
        try:
            with open(cache) as stream:
                cached = stream.read().strip()
            if cached:
                return cached
        except OSError:
            pass

        self.on_event("begin", "identifying the scanner at {}".format(self.address))
        try:
            result = self._run(
                vm.shell_cmd("hp-makeuri", "-c", self.address), timeout=30
            )
        finally:
            self.on_event("end", "identifying the scanner at {}".format(self.address))
        uri = hpaio_uri(result.out)
        if not result.ok or not uri:
            raise HPLIPError(
                BackendErrorCode.UNAVAILABLE,
                "the VM could not identify a scanner at {}.\n"
                "Check the printer is on and reachable from this network.".format(
                    self.address
                ),
                retryable=True,
            )
        paths.ensure_state_dir()
        with open(cache, "w") as stream:
            stream.write(uri + "\n")
        return uri

    def _check_scanner(self, scanner: Scanner) -> None:
        if not isinstance(scanner, Scanner) or scanner.backend != self.name:
            raise ValueError("scanner does not belong to the HPLIP backend")
        if not scanner.endpoint.startswith("hpaio:/"):
            raise ValueError("scanner has an invalid hpaio endpoint")

    def inspect(self, scanner: Scanner) -> Capabilities:
        self._check_scanner(scanner)
        if self._capabilities is not None:
            return self._capabilities
        self._ensure_guest()
        result = self._run(
            vm.shell_cmd("scanimage", "-d", scanner.endpoint, "--all-options"),
            timeout=45,
        )
        if not result.ok:
            raise HPLIPError(
                BackendErrorCode.UNAVAILABLE,
                result.err.strip() or "HPLIP could not inspect the scanner",
                retryable=True,
            )
        modes = []
        for value in _option_values(result.out, "mode"):
            try:
                modes.append(ScanMode.parse(value))
            except ValueError:
                continue
        resolutions = _resolutions(result.out)
        sources = []
        for value in _option_values(result.out, "source"):
            try:
                source = ScanSource.parse(value)
            except ValueError:
                continue
            if source not in (ScanSource.FLATBED, ScanSource.FEEDER):
                continue
            if modes and resolutions:
                sources.append(SourceCapabilities(
                    source=source,
                    modes=tuple(modes),
                    resolutions=resolutions,
                    supports_lossless=True,
                ))
        if not sources:
            raise HPLIPError(
                BackendErrorCode.PROTOCOL,
                "HPLIP returned incomplete scanner capabilities",
            )
        self._capabilities = Capabilities(scanner.id, tuple(sources))
        return self._capabilities

    def prepare(self, scanner: Scanner, request: ScanRequest) -> ScanJob:
        self._check_scanner(scanner)
        if not isinstance(request, ScanRequest):
            raise ValueError("request must be a ScanRequest")
        if request.scanner_id != scanner.id:
            raise UnsupportedRequest("request targets a different scanner")
        _source_name(request.source)
        return _HPLIPScanJob(
            scanner,
            request,
            on_event=self.on_event,
            runner=self._run,
            streaming_runner=self._run_streaming,
        )

    def release(self, keep_alive: int) -> None:
        """Keep the legacy guest warm for the configured idle period."""
        vm.idle_timer_arm(keep_alive)


class _HPLIPScanJob(ScanJob):
    def __init__(self, scanner: Scanner, request: ScanRequest, *,
                 on_event: EventHandler, runner: Runner,
                 streaming_runner: StreamingRunner) -> None:
        self.scanner = scanner
        self.request = request
        self.on_event = on_event
        self._run = runner
        self._run_streaming = streaming_runner
        self.run_id = "{:d}-{:d}".format(os.getpid(), int(time.time() * 1000))
        self._result: Optional[ScanResult] = None
        self._active = False
        self._cancelled = False
        self._state_lock = threading.Lock()
        self.diagnostics: Tuple[str, ...] = ()
        self.measurements: Tuple[str, ...] = ()

    @property
    def result(self) -> Optional[ScanResult]:
        return self._result

    def scan(self) -> ScanResult:
        with self._state_lock:
            if self._result is not None:
                return self._result
            if self._cancelled:
                raise HPLIPError(BackendErrorCode.CANCELLED, "scan cancelled")
            if self._active:
                raise HPLIPError(BackendErrorCode.BUSY, "this scan job is already running")
            self._active = True

        err_file = tempfile.NamedTemporaryFile(prefix="scanbox-err-", delete=False)
        err_path = err_file.name
        err_file.close()
        summary: List[str] = []
        completed_remote = False
        try:
            with lock.Lock():
                try:
                    self._clear_stale_scan()
                    args = [
                        self.scanner.endpoint,
                        _source_name(self.request.source),
                        _mode_name(self.request.mode),
                        str(self.request.resolution),
                        self.request.page_size.value,
                        "1" if self.request.lossless else "0",
                        "scan",
                        self.run_id,
                        "pdf", "0", "0", "1",
                    ]

                    def read_line(line: str) -> None:
                        if line.startswith("PROGRESS "):
                            self.on_event("progress", line[len("PROGRESS "):].strip())
                        elif line.startswith("PHASE "):
                            self.on_event("phase", line[len("PHASE "):].strip())
                        elif line.startswith("NOTE "):
                            self.on_event("note", line[len("NOTE "):].strip())
                        else:
                            summary.append(line)

                    process = self._run_streaming(
                        vm.shell_cmd("bash", "-s", "--", *args),
                        read_line,
                        stdin_path=paths.GUEST_SCAN_SH,
                        stderr_path=err_path,
                    )
                    completed_remote = process.ok
                    self.diagnostics = tuple(_read_text(err_path).splitlines())
                    if not process.ok:
                        raise HPLIPError(
                            _error_code(self.diagnostics),
                            "scan failed",
                            diagnostics=self.diagnostics,
                            retryable=True,
                        )

                    result = self._result_from_summary(summary)
                    with self._state_lock:
                        self._result = result
                    return result
                except BaseException:
                    if not completed_remote:
                        self.cancel()
                    raise
        finally:
            _unlink(err_path)
            with self._state_lock:
                self._active = False

    def _result_from_summary(self, lines: Sequence[str]) -> ScanResult:
        fields = parse_summary(lines)
        guest_pages = [values[0] for values in fields.get("RASTER", ()) if values]
        if not guest_pages:
            raise HPLIPError(
                BackendErrorCode.PROTOCOL, "the VM produced no acquired pages"
            )
        used = fields.get("SOURCE", ())
        counts = fields.get("PAGES", ())
        if not used or len(used[0]) != 1 or used[0][0] not in ("ADF", "Flatbed"):
            raise HPLIPError(
                BackendErrorCode.PROTOCOL,
                "the VM did not report which source it used",
            )
        if (not counts or len(counts[0]) != 1 or not counts[0][0].isdigit()
                or int(counts[0][0]) < 1):
            raise HPLIPError(
                BackendErrorCode.PROTOCOL,
                "the VM did not report a valid acquired page count",
            )
        if int(counts[0][0]) != len(guest_pages):
            raise HPLIPError(
                BackendErrorCode.PROTOCOL,
                "the VM returned an incomplete set of acquired pages",
            )

        host_dir = tempfile.mkdtemp(prefix="scanbox-legacy-")
        pages = []
        try:
            self.on_event("copy", "copying acquired pages")
            for index, guest_page in enumerate(guest_pages, 1):
                host_page = os.path.join(host_dir, "page-{:04d}.png".format(index))
                copied = self._run(
                    ["limactl", "copy",
                     "{}:{}".format(vm.NAME, guest_page), host_page],
                    timeout=600,
                )
                if not copied.ok:
                    raise HPLIPError(
                        BackendErrorCode.IO,
                        "could not copy an acquired page out of the VM",
                    )
                pages.append(ScanPage(
                    index=index,
                    path=host_page,
                    media_type="image/png",
                    resolution=self.request.resolution,
                ))
        except BaseException:
            shutil.rmtree(host_dir, ignore_errors=True)
            raise

        measurements = []
        for values in fields.get("PAGE", ()):
            if len(values) >= 3:
                measurements.append(
                    "{}: {} (measured {}in)".format(values[0], values[1], values[2])
                )
        self.measurements = tuple(measurements)
        source = ScanSource.FEEDER if used[0][0] == "ADF" else ScanSource.FLATBED
        return ScanResult(
            scanner_id=self.scanner.id,
            backend=BACKEND_NAME,
            source=source,
            pages=tuple(pages),
            truncated="TRUNCATED" in fields,
        )

    def _clear_stale_scan(self) -> None:
        if not self._run(vm.shell_cmd("pgrep", "-x", "scanimage"), timeout=20).ok:
            return
        self.on_event(
            "warning",
            "a scan from an earlier run is still going inside the VM. Stopping it --\n"
            "      otherwise the printer refuses this one.",
        )
        self._run(vm.shell_cmd("pkill", "-x", "scanimage"), timeout=20)
        wait = (
            "for _ in $(seq 1 15); do pgrep -x scanimage >/dev/null || break; "
            "sleep 1; done"
        )
        self._run(vm.shell_cmd("bash", "-c", wait), timeout=20)

    def cancel(self) -> None:
        with self._state_lock:
            if self._cancelled:
                return
            self._cancelled = True
            active = self._active
        if not active:
            return
        pgid_file = "/tmp/scanbox-run-{}.pgid".format(self.run_id)
        script = (
            "f={f}\n"
            '[ -f "$f" ] || exit 0\n'
            'g=$(cat "$f" 2>/dev/null) || exit 0\n'
            '[ -n "$g" ] || exit 0\n'
            'kill -TERM -"$g" 2>/dev/null || true\n'
            'for _ in 1 2 3 4 5; do kill -0 -"$g" 2>/dev/null || break; sleep 1; done\n'
            'kill -KILL -"$g" 2>/dev/null || true\n'
            'rm -f "$f"\n'
        ).format(f=shlex.quote(pgid_file))
        self._run(vm.shell_cmd("bash", "-c", script), timeout=20)


def _error_code(lines: Sequence[str]) -> BackendErrorCode:
    text = "\n".join(lines).lower()
    if "out of documents" in text or "feeder is empty" in text:
        return BackendErrorCode.EMPTY_FEEDER
    if "jam" in text:
        return BackendErrorCode.JAMMED
    if "error during device i/o" in text or "busy" in text:
        return BackendErrorCode.BUSY
    return BackendErrorCode.IO


def _read_text(path: str) -> str:
    try:
        with open(path, "r", errors="replace") as stream:
            return stream.read()
    except OSError:
        return ""


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
