"""WSD scanning through sane-airscan in the existing Lima guest.

WS-Discovery must run on the macOS host because multicast does not cross
Lima's vzNAT boundary. Each discovered scanner carries the explicit WSD HTTP
endpoint returned by the device. The guest receives that endpoint through
``SANE_AIRSCAN_DEVICE`` with protocol ``wsd`` forced, so neither multicast nor
sane-airscan's eSCL/WSD protocol preference participates in a job.

Discovery, inspection, and preparation do not move paper. Only ``scan()`` on
the prepared job invokes scanimage.
"""
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import uuid
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from .. import paths, proc, vm
from ..contracts import (
    Backend,
    BackendError,
    BackendErrorCode,
    Capabilities,
    PageSize,
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

BACKEND_NAME = "sane-airscan-wsd"
MULTICAST_ADDRESS = ("239.255.255.250", 3702)
DISCOVERY_ACTION = "http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe"
DISCOVERY_TO = "urn:schemas-xmlsoap-org:ws:2005:04:discovery"
SCAN_NAMESPACE = "http://schemas.microsoft.com/windows/2006/08/wdp/scan"

_DEVICE_RE = re.compile(r"device [`']([^`']+)[`']")
_OPTION_RE = re.compile(r"^\s*(?:-[A-Za-z],\s*)?--([\w-]+)\s+(.+?)\s*$")
_RANGE_RE = re.compile(
    r"^(-?\d+(?:\.\d+)?)\.\.(-?\d+(?:\.\d+)?)"
    r"(?:\s*[^\d\s][^\s]*)?(?:\s+\(in steps of\s+(-?\d+(?:\.\d+)?)"
    r"[^)]*\))?$"
)
_UUID_RE = re.compile(
    r"^(?:urn:uuid:)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

EventHandler = Callable[[str, str], None]
Runner = Callable[..., proc.Result]
StreamingRunner = Callable[..., proc.Result]

_PAGE_SIZE_MM = {
    PageSize.LETTER: (215.9, 279.4),
    PageSize.LEGAL: (215.9, 355.6),
    PageSize.A4: (210.0, 297.0),
}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _element_text(parent: ElementTree.Element, name: str) -> str:
    for element in parent.iter():
        if _local_name(element.tag) == name and element.text:
            return element.text.strip()
    return ""


def _stable_wsd_id(address: str) -> Optional[str]:
    """Return a physical identity, never a current-LAN IP address."""
    address = address.strip().lower()
    if not _UUID_RE.match(address):
        return None
    value = address if address.startswith("urn:uuid:") else "urn:uuid:" + address
    return "wsd:" + value


def _valid_endpoint(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def _friendly_name(scopes: str, endpoint: str) -> str:
    for scope in scopes.split():
        decoded = unquote(scope).rstrip("/")
        lowered = decoded.lower()
        for marker in ("/friendlyname/", "/name/"):
            if marker in lowered:
                name = decoded[lowered.rfind(marker) + len(marker):]
                if name:
                    return name.replace("_", " ")
    host = urlsplit(endpoint).hostname or "unknown host"
    return "WSD scanner at {}".format(host)


def parse_probe_response(data: bytes) -> Tuple[Scanner, ...]:
    """Parse one WS-Discovery datagram into normalized WSD scan candidates."""
    try:
        root = ElementTree.fromstring(data.rstrip(b"\0"))
    except ElementTree.ParseError:
        return ()

    found = []
    for match in root.iter():
        if _local_name(match.tag) != "ProbeMatch":
            continue
        types = _element_text(match, "Types").split()
        if not any(value.rsplit(":", 1)[-1] == "ScanDeviceType" for value in types):
            continue

        stable_id = _stable_wsd_id(_element_text(match, "Address"))
        if stable_id is None:
            # Persisting an IP-based fallback would silently bind a configured
            # scanner to whichever device DHCP gives that address next.
            continue
        endpoints = [
            value for value in _element_text(match, "XAddrs").split()
            if _valid_endpoint(value)
        ]
        if not endpoints:
            continue
        endpoint = endpoints[0]
        found.append(Scanner(
            id=stable_id,
            name=_friendly_name(_element_text(match, "Scopes"), endpoint),
            backend=BACKEND_NAME,
            endpoint=endpoint,
            transport="network-wsd",
        ))
    return tuple(found)


def _probe_message(message_id: uuid.UUID) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:scan="{scan}">'
        '<s:Header>'
        '<a:Action>{action}</a:Action>'
        '<a:MessageID>urn:uuid:{message}</a:MessageID>'
        '<a:ReplyTo><a:Address>'
        'http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous'
        '</a:Address></a:ReplyTo>'
        '<a:To>{to}</a:To>'
        '</s:Header>'
        '<s:Body><d:Probe><d:Types>scan:ScanDeviceType</d:Types>'
        '</d:Probe></s:Body></s:Envelope>'
    ).format(
        scan=SCAN_NAMESPACE, action=DISCOVERY_ACTION,
        message=message_id, to=DISCOVERY_TO,
    ).encode("utf-8")


def discover_wsd(seconds: float = 3.0) -> Tuple[Scanner, ...]:
    """Probe for WSD scanners on the host's current multicast-capable LAN."""
    if seconds <= 0:
        return ()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.bind(("", 0))
        message = _probe_message(uuid.uuid4())
        started = time.monotonic()
        deadline = started + seconds
        send_at = [started, started + min(0.25, seconds / 3),
                   started + min(0.75, seconds * 2 / 3)]
        sent = 0
        by_id: Dict[str, Scanner] = {}

        while time.monotonic() < deadline:
            now = time.monotonic()
            while sent < len(send_at) and now >= send_at[sent]:
                sock.sendto(message, MULTICAST_ADDRESS)
                sent += 1
            next_event = send_at[sent] if sent < len(send_at) else deadline
            sock.settimeout(max(0.01, min(deadline, next_event) - now))
            try:
                data, _peer = sock.recvfrom(65535)
            except socket.timeout:
                continue
            for scanner in parse_probe_response(data):
                by_id.setdefault(scanner.id, scanner)
        return tuple(sorted(by_id.values(), key=lambda item: (item.name, item.id)))
    except OSError as error:
        raise BackendError(
            BackendErrorCode.UNAVAILABLE,
            "WSD discovery failed: {}".format(error),
            backend=BACKEND_NAME,
            retryable=True,
        )
    finally:
        sock.close()


def _airscan_environment(scanner: Scanner) -> str:
    # The fixed name contains no colon, which is sane-airscan's field separator.
    return "SANE_AIRSCAN_DEVICE=wsd:scanbox-wsd:{}".format(scanner.endpoint)


def _device_name(output: str) -> Optional[str]:
    match = _DEVICE_RE.search(output)
    return match.group(1) if match else None


def _option_values(output: str, option: str) -> Tuple[str, ...]:
    for line in output.splitlines():
        match = _OPTION_RE.match(line)
        if match and match.group(1) == option:
            raw_value = match.group(2)
        elif option in ("x", "y"):
            short = re.match(r"^\s*-{}\s+(.+?)\s*$".format(option), line)
            if not short:
                continue
            raw_value = short.group(1)
        else:
            continue
        value = re.sub(r"\s+\[[^]]*\]\s*$", "", raw_value).strip()
        if "|" in value:
            return tuple(part.strip() for part in value.split("|") if part.strip())
        return (value,) if value else ()
    return ()


def _resolutions(output: str) -> Tuple[int, ...]:
    values = _option_values(output, "resolution")
    if not values:
        return ()
    if len(values) > 1:
        result = []
        for value in values:
            match = re.match(r"^(\d+)", value)
            if match:
                result.append(int(match.group(1)))
        return tuple(sorted(set(result)))

    match = _RANGE_RE.match(values[0])
    if match:
        low, high = int(float(match.group(1))), int(float(match.group(2)))
        step = int(float(match.group(3) or 1))
        if step > 0 and high >= low and (high - low) // step <= 2400:
            return tuple(range(low, high + 1, step))
    match = re.match(r"^(\d+)", values[0])
    return (int(match.group(1)),) if match else ()


def _max_size(output: str) -> Optional[Tuple[float, float]]:
    dimensions = []
    for option in ("br-x", "br-y"):
        values = _option_values(output, option)
        if not values:
            # scanimage prints its standard geometry aliases as -x/-y.
            short = "x" if option == "br-x" else "y"
            values = _option_values(output, short)
        if not values:
            return None
        numbers = re.findall(r"-?\d+(?:\.\d+)?", values[0].split("[")[0])
        if not numbers:
            return None
        dimensions.append(float(numbers[-1]))
    if dimensions[0] <= 0 or dimensions[1] <= 0:
        return None
    return dimensions[0], dimensions[1]


def _source_name(source: ScanSource) -> str:
    return {
        ScanSource.FLATBED: "Flatbed",
        ScanSource.FEEDER: "ADF",
        ScanSource.FEEDER_DUPLEX: "ADF Duplex",
    }[source]


def _mode_name(mode: ScanMode) -> str:
    return {
        ScanMode.COLOR: "Color",
        ScanMode.GRAYSCALE: "Gray",
        ScanMode.LINEART: "Lineart",
    }[mode]


def _backend_error(text: str, *, cancelled: bool = False) -> BackendError:
    detail = text.strip() or "WSD scan failed"
    lowered = detail.lower()
    if cancelled or "cancel" in lowered:
        code, retryable = BackendErrorCode.CANCELLED, False
    elif "out of documents" in lowered or "no documents" in lowered:
        code, retryable = BackendErrorCode.EMPTY_FEEDER, False
    elif "jam" in lowered:
        code, retryable = BackendErrorCode.JAMMED, False
    elif "busy" in lowered:
        code, retryable = BackendErrorCode.BUSY, True
    elif "invalid argument" in lowered or "unsupported" in lowered:
        code, retryable = BackendErrorCode.UNSUPPORTED, False
    elif "could not connect" in lowered or "not found" in lowered:
        code, retryable = BackendErrorCode.UNAVAILABLE, True
    else:
        code, retryable = BackendErrorCode.IO, True
    return BackendError(code, detail, backend=BACKEND_NAME, retryable=retryable)


class WSDBackend(Backend):
    """Normalized WSD backend with host discovery and guest acquisition."""

    def __init__(self, *, discovery_seconds: float = 3.0,
                 on_event: Optional[EventHandler] = None,
                 ensure_guest: Callable[[], None] = vm.ensure_wsd,
                 runner: Runner = proc.run,
                 streaming_runner: StreamingRunner = proc.run_streaming) -> None:
        self.discovery_seconds = discovery_seconds
        self.on_event = on_event or (lambda _kind, _value: None)
        self._ensure_guest = ensure_guest
        self._run = runner
        self._run_streaming = streaming_runner
        self._cache: Dict[Tuple[str, str], Tuple[Capabilities, str]] = {}

    @property
    def name(self) -> str:
        return BACKEND_NAME

    def discover(self) -> Sequence[Scanner]:
        return discover_wsd(self.discovery_seconds)

    def _check_scanner(self, scanner: Scanner) -> None:
        if not isinstance(scanner, Scanner) or scanner.backend != self.name:
            raise ValueError("scanner does not belong to the WSD backend")
        if not _valid_endpoint(scanner.endpoint):
            raise ValueError("scanner has an invalid WSD endpoint")

    def _guest_device(self, scanner: Scanner) -> str:
        result = self._run(
            vm.shell_cmd("env", _airscan_environment(scanner), "scanimage", "-L"),
            timeout=45,
        )
        device = _device_name(result.out)
        if not result.ok or not device:
            raise _backend_error(result.err or result.out or
                                 "sane-airscan did not expose the WSD scanner")
        if not device.startswith("airscan:"):
            raise BackendError(
                BackendErrorCode.PROTOCOL,
                "the explicit WSD endpoint resolved to a non-airscan SANE device",
                backend=self.name,
            )
        return device

    def inspect(self, scanner: Scanner) -> Capabilities:
        self._check_scanner(scanner)
        cache_key = scanner.id, scanner.endpoint
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached[0]

        self._ensure_guest()
        device = self._guest_device(scanner)
        base = self._run(
            vm.shell_cmd(
                "env", _airscan_environment(scanner), "scanimage",
                "-d", device, "--all-options",
            ),
            timeout=45,
        )
        if not base.ok:
            raise _backend_error(base.err or base.out)

        sources = []
        for raw_source in _option_values(base.out, "source"):
            try:
                source = ScanSource.parse(raw_source)
            except ValueError:
                continue
            if source not in (
                ScanSource.FLATBED, ScanSource.FEEDER, ScanSource.FEEDER_DUPLEX,
            ):
                continue
            detail = self._run(
                vm.shell_cmd(
                    "env", _airscan_environment(scanner), "scanimage",
                    "-d", device, "--source", raw_source, "--all-options",
                ),
                timeout=45,
            )
            if not detail.ok:
                raise _backend_error(detail.err or detail.out)
            modes = []
            for raw_mode in _option_values(detail.out, "mode"):
                try:
                    modes.append(ScanMode.parse(raw_mode))
                except ValueError:
                    continue
            resolutions = _resolutions(detail.out)
            if not modes or not resolutions:
                raise BackendError(
                    BackendErrorCode.PROTOCOL,
                    "sane-airscan returned incomplete capabilities for {}".format(
                        raw_source
                    ),
                    backend=self.name,
                )
            sources.append(SourceCapabilities(
                source=source,
                modes=tuple(modes),
                resolutions=resolutions,
                # sane-airscan chooses the WSD document encoding internally;
                # it exposes no SANE option that guarantees lossless transfer.
                supports_lossless=False,
                max_size_mm=_max_size(detail.out),
            ))
        if not sources:
            raise BackendError(
                BackendErrorCode.PROTOCOL,
                "sane-airscan returned no supported scan sources",
                backend=self.name,
            )
        capabilities = Capabilities(scanner.id, tuple(sources))
        self._cache[cache_key] = capabilities, device
        return capabilities

    def prepare(self, scanner: Scanner, request: ScanRequest) -> ScanJob:
        self._check_scanner(scanner)
        capabilities = self.inspect(scanner)
        compatible = capabilities.compatible_sources(request)
        requested_size = _PAGE_SIZE_MM.get(request.page_size)
        if requested_size is not None:
            compatible = tuple(
                source for source in compatible
                if source.max_size_mm is None
                or (
                    source.max_size_mm[0] + 0.1 >= requested_size[0]
                    and source.max_size_mm[1] + 0.1 >= requested_size[1]
                )
            )
            if not compatible:
                raise UnsupportedRequest(
                    "no selected source supports {} paper".format(
                        request.page_size.value
                    )
                )
        cache_key = scanner.id, scanner.endpoint
        device = self._cache[cache_key][1]
        return _WSDScanJob(
            scanner, request, compatible, device,
            on_event=self.on_event, runner=self._run,
            streaming_runner=self._run_streaming,
        )


class _WSDScanJob(ScanJob):
    def __init__(self, scanner: Scanner, request: ScanRequest,
                 compatible: Sequence[SourceCapabilities], device: str, *,
                 on_event: EventHandler, runner: Runner,
                 streaming_runner: StreamingRunner) -> None:
        self.scanner = scanner
        self.request = request
        self.compatible = tuple(compatible)
        self.device = device
        self.on_event = on_event
        self._run = runner
        self._run_streaming = streaming_runner
        self.run_id = "{:d}-{:d}".format(os.getpid(), int(time.time() * 1000))
        self.guest_dir = "/tmp/scanbox-wsd-{}".format(self.run_id)
        self._result: Optional[ScanResult] = None
        self._cancelled = False
        self._active = False
        self._state_lock = threading.Lock()

    @property
    def result(self) -> Optional[ScanResult]:
        return self._result

    def _source_arguments(self) -> Tuple[str, str]:
        if self.request.source is not ScanSource.AUTO:
            return _source_name(self.request.source), ""
        available = tuple(item.source for item in self.compatible)
        feeder = ""
        if ScanSource.FEEDER in available:
            feeder = _source_name(ScanSource.FEEDER)
        elif ScanSource.FEEDER_DUPLEX in available:
            feeder = _source_name(ScanSource.FEEDER_DUPLEX)
        return "auto", feeder

    def scan(self) -> ScanResult:
        with self._state_lock:
            if self._result is not None:
                return self._result
            if self._cancelled:
                raise _backend_error("WSD scan cancelled", cancelled=True)
            if self._active:
                raise BackendError(
                    BackendErrorCode.BUSY,
                    "this WSD scan job is already running",
                    backend=BACKEND_NAME,
                )
            self._active = True

        source, auto_feeder = self._source_arguments()
        summary: List[str] = []
        err_file = tempfile.NamedTemporaryFile(prefix="scanbox-wsd-err-", delete=False)
        err_path = err_file.name
        err_file.close()
        try:
            args = [
                _airscan_environment(self.scanner), self.device, source,
                auto_feeder, _mode_name(self.request.mode),
                str(self.request.resolution), self.request.page_size.value,
                self.run_id, self.guest_dir,
            ]

            def read_line(line: str) -> None:
                if line.startswith("PROGRESS "):
                    self.on_event("progress", line[len("PROGRESS "):].strip())
                elif line.startswith("PHASE "):
                    self.on_event("phase", line[len("PHASE "):].strip())
                else:
                    summary.append(line)

            result = self._run_streaming(
                vm.shell_cmd("bash", "-s", "--", *args),
                read_line,
                stdin_path=paths.GUEST_WSD_SCAN_SH,
                stderr_path=err_path,
            )
            error_text = _read_text(err_path)
            with self._state_lock:
                cancelled = self._cancelled
            if not result.ok:
                raise _backend_error(error_text, cancelled=cancelled)

            fields = _summary_fields(summary)
            guest_pages = [values[0] for values in fields.get("PAGE", ()) if values]
            source_values = fields.get("SOURCE", ())
            if not guest_pages or not source_values or not source_values[0]:
                raise BackendError(
                    BackendErrorCode.PROTOCOL,
                    "the WSD guest returned an incomplete scan summary",
                    backend=BACKEND_NAME,
                )
            used_source = ScanSource.parse(" ".join(source_values[0]))
            host_dir = tempfile.mkdtemp(prefix="scanbox-wsd-pages-")
            pages = []
            try:
                for index, guest_page in enumerate(guest_pages, 1):
                    host_page = os.path.join(host_dir, "page-{:04d}.png".format(index))
                    copied = self._run(
                        ["limactl", "copy", "{}:{}".format(vm.NAME, guest_page),
                         host_page],
                        timeout=600,
                    )
                    if not copied.ok:
                        raise BackendError(
                            BackendErrorCode.IO,
                            "could not copy WSD page {} out of the VM".format(index),
                            backend=BACKEND_NAME,
                        )
                    width, height = _png_dimensions(host_page)
                    pages.append(ScanPage(
                        index=index,
                        path=host_page,
                        media_type="image/png",
                        width_px=width,
                        height_px=height,
                        resolution=self.request.resolution,
                    ))
            except BaseException:
                shutil.rmtree(host_dir, ignore_errors=True)
                raise

            warnings = tuple(
                " ".join(values) for values in fields.get("WARNING", ()) if values
            )
            completed = ScanResult(
                scanner_id=self.scanner.id,
                backend=BACKEND_NAME,
                source=used_source,
                pages=tuple(pages),
                truncated="TRUNCATED" in fields,
                warnings=warnings,
            )
            with self._state_lock:
                self._result = completed
            return completed
        except BaseException:
            self.cancel()
            raise
        finally:
            with self._state_lock:
                self._active = False
            self._run(
                vm.shell_cmd("rm", "-rf", self.guest_dir,
                             "/tmp/scanbox-run-{}.pgid".format(self.run_id)),
                timeout=20,
            )
            _unlink(err_path)

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
            "f=$1; "
            "for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s \"$f\" ] && break; sleep .1; done; "
            "[ -s \"$f\" ] || exit 0; "
            "g=$(cat \"$f\" 2>/dev/null) || exit 0; "
            "case $g in (*[!0-9]*|'') exit 0;; esac; "
            "kill -TERM -\"$g\" 2>/dev/null || true; "
            "for _ in 1 2 3 4 5; do kill -0 -\"$g\" 2>/dev/null || exit 0; sleep 1; done; "
            "kill -KILL -\"$g\" 2>/dev/null || true"
        )
        self._run(vm.shell_cmd("bash", "-c", script, "--", pgid_file), timeout=10)


def _summary_fields(lines: Iterable[str]) -> Dict[str, List[List[str]]]:
    fields: Dict[str, List[List[str]]] = {}
    for line in lines:
        parts = line.split()
        if parts:
            fields.setdefault(parts[0], []).append(parts[1:])
    return fields


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


def _png_dimensions(path: str) -> Tuple[Optional[int], Optional[int]]:
    try:
        with open(path, "rb") as stream:
            header = stream.read(24)
    except OSError:
        return None, None
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        return None, None
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
