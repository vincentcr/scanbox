"""Deterministic, pre-acquisition routing for configured scanners.

The router may discover devices, resolve locators, inspect capabilities, and
prepare a job.  All of those operations are read-only by the backend contract.
It returns exactly one prepared job; once the caller invokes ``scan()``, this
module is no longer involved and cross-protocol retry is impossible.
"""
from dataclasses import dataclass, replace
import re
import uuid
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from . import config, discover
from .backends.hplip import HPLIPBackend, supports_configured as supports_hplip
from .backends.wsd import WSDBackend
from .contracts import (
    Backend, BackendError, ScanJob, ScanRequest, Scanner, UnsupportedRequest,
)

BACKENDS = config.BACKENDS

_UUID_SEARCH_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

EventHandler = Callable[[str, str], None]
HPLIPFactory = Callable[..., Backend]
Resolver = Callable[[str], Optional[str]]
HPLIPSupport = Callable[[config.ConfiguredScanner], bool]


class RoutingError(ValueError):
    """No backend can safely prepare a scan for the configured device."""


@dataclass(frozen=True)
class PhysicalScanner:
    """Advertisements which carry the same strong physical identity."""

    identity: str
    scanners: Tuple[Scanner, ...]


@dataclass(frozen=True)
class PreparedRoute:
    """One selected backend and job, with an audit trail of the decision."""

    backend_name: str
    backend: Backend
    scanner: Scanner
    job: ScanJob
    diagnostics: Tuple[str, ...]


def stable_identity(value: Optional[str]) -> Optional[str]:
    """Normalize cross-protocol UUID/serial spellings for physical grouping."""
    if not value:
        return None
    text = value.strip()
    match = _UUID_SEARCH_RE.search(text)
    if match:
        try:
            return "uuid:" + str(uuid.UUID(match.group(0)))
        except ValueError:
            return None
    lowered = text.casefold()
    marker = lowered.find("serial:")
    if marker >= 0:
        serial = text[marker + len("serial:"):].strip()
        if serial:
            return "serial:" + serial.casefold()
    return None


def group_scanners(scanners: Iterable[Scanner]) -> Tuple[PhysicalScanner, ...]:
    """Group only advertisements with a shared, strong persistent identity.

    Names and IP addresses are deliberately too weak to merge devices.  An
    advertisement without a UUID or serial remains its own group.
    """
    groups = {}
    for index, scanner in enumerate(scanners):
        identity = stable_identity(scanner.id)
        key = identity or "candidate:{}:{}:{}".format(
            scanner.backend, scanner.id, index
        )
        groups.setdefault(key, []).append(scanner)
    result = []
    for key, members in groups.items():
        ordered = tuple(sorted(
            members,
            key=lambda item: (
                item.backend.casefold(), item.name.casefold(), item.endpoint
            ),
        ))
        result.append(PhysicalScanner(key, ordered))
    return tuple(sorted(result, key=lambda item: (
        item.scanners[0].name.casefold(), item.identity
    )))


class Router:
    """Choose and prepare WSD or HPLIP acquisition."""

    def __init__(self, *,
                 wsd_backend: Optional[Backend] = None,
                 hplip_factory: HPLIPFactory = HPLIPBackend,
                 hplip_support: HPLIPSupport = supports_hplip,
                 resolver: Resolver = discover.resolve_ipv4,
                 on_event: Optional[EventHandler] = None) -> None:
        self.wsd_backend = wsd_backend or WSDBackend()
        self.hplip_factory = hplip_factory
        self.hplip_support = hplip_support
        self.resolver = resolver
        self.on_event = on_event or (lambda _kind, _value: None)
        # Backends copy their event handler into a prepared job.  The CLI may
        # pass a relay whose target changes from routing UI to scan progress.
        if hasattr(self.wsd_backend, "on_event"):
            self.wsd_backend.on_event = self.on_event

    def prepare(self, configured: config.ConfiguredScanner,
                request: ScanRequest, *,
                backend_preference: Optional[str] = None) -> PreparedRoute:
        if not isinstance(configured, config.ConfiguredScanner):
            raise ValueError("configured must be a ConfiguredScanner")
        if not isinstance(request, ScanRequest):
            raise ValueError("request must be a ScanRequest")
        selected = (backend_preference or configured.backend).strip().lower()
        if selected not in BACKENDS:
            raise RoutingError("unknown scanner backend: {!r}".format(selected))
        if selected == "imagecapture":
            raise RoutingError(
                "ImageCapture scanning is not available yet; use auto, wsd, or hplip"
            )

        diagnostics: List[str] = []
        wsd_error: Optional[BaseException] = None
        if selected in ("auto", "wsd"):
            try:
                scanner, reason = self._wsd_scanner(configured, diagnostics)
                if scanner is None:
                    raise RoutingError("no WSD advertisement matched the configured scanner")
                prepared_request = replace(request, scanner_id=scanner.id)
                job = self.wsd_backend.prepare(scanner, prepared_request)
                diagnostics.append(
                    "selected backend wsd: {}".format(reason)
                )
                return PreparedRoute(
                    "wsd", self.wsd_backend, scanner, job, tuple(diagnostics)
                )
            except (BackendError, RoutingError, UnsupportedRequest) as error:
                wsd_error = error
                diagnostics.append("rejected backend wsd before acquisition: {}".format(error))
                if selected == "wsd":
                    raise RoutingError("; ".join(diagnostics))

        if selected in ("auto", "hplip"):
            if not self.hplip_support(configured):
                diagnostics.append(
                    "rejected backend hplip: the configured device is not supported "
                    "by HPLIP"
                )
                raise RoutingError("; ".join(diagnostics))
            try:
                address = self._address(configured)
                backend = self.hplip_factory(address, on_event=self.on_event)
                scanners = tuple(backend.discover())
                if not scanners:
                    raise RoutingError("HPLIP found no scanner")
                scanner = sorted(
                    scanners,
                    key=lambda item: (item.name.casefold(), item.id, item.endpoint),
                )[0]
                prepared_request = replace(request, scanner_id=scanner.id)
                capabilities = backend.inspect(scanner)
                capabilities.compatible_sources(prepared_request)
                job = backend.prepare(scanner, prepared_request)
                reason = "explicit preference" if selected == "hplip" else (
                    "WSD was unavailable before acquisition"
                )
                diagnostics.append(
                    "selected backend hplip: {}".format(reason)
                )
                return PreparedRoute(
                    "hplip", backend, scanner, job, tuple(diagnostics)
                )
            except (BackendError, RoutingError, UnsupportedRequest) as error:
                diagnostics.append(
                    "rejected backend hplip before acquisition: {}".format(error)
                )
                if wsd_error is not None or selected == "hplip":
                    raise RoutingError("; ".join(diagnostics))
                raise

        raise RoutingError("no usable scanner backend")

    def _wsd_scanner(self, configured: config.ConfiguredScanner,
                     diagnostics: List[str]) -> Tuple[Optional[Scanner], str]:
        self.on_event("begin", "looking for the configured scanner over WSD")
        try:
            discovered = tuple(self.wsd_backend.discover())
        finally:
            self.on_event("end", "looking for the configured scanner over WSD")

        # Grouping makes repeated or future multi-implementation advertisements
        # deterministic.  This route currently asks the WSD backend only, while
        # preserving the physical-device boundary for native support later.
        scanners = tuple(
            scanner for group in group_scanners(discovered)
            for scanner in group.scanners
        )
        configured_identity = stable_identity(configured.id)
        if configured_identity is not None:
            matches = tuple(
                scanner for scanner in scanners
                if stable_identity(scanner.id) == configured_identity
            )
            if matches:
                chosen = self._first(matches)
                return chosen, "stable identity {} matched".format(configured_identity)
            diagnostics.append(
                "WSD stable identity {} was not advertised; trying the saved locator".format(
                    configured_identity
                )
            )

        locators = self._locators(configured)
        matcher = getattr(self.wsd_backend, "matches_locator", None)
        if matcher is not None and locators:
            matches = tuple(
                scanner for scanner in scanners if matcher(scanner, locators)
            )
            if matches:
                return self._first(matches), "fallback locator matched"
        return None, ""

    @staticmethod
    def _first(scanners: Sequence[Scanner]) -> Scanner:
        return sorted(
            scanners,
            key=lambda item: (item.name.casefold(), item.id, item.endpoint),
        )[0]

    def _locators(self, configured: config.ConfiguredScanner) -> Tuple[str, ...]:
        values = []
        if configured.host:
            values.append(configured.host)
            if discover.is_ipv4(configured.host):
                resolved = configured.host
            else:
                self.on_event("begin", "looking up {}".format(configured.host))
                try:
                    resolved = self.resolver(configured.host)
                finally:
                    self.on_event("end", "looking up {}".format(configured.host))
            if resolved:
                values.append(resolved)
        if configured.address:
            values.append(configured.address)
        return tuple(dict.fromkeys(values))

    def _address(self, configured: config.ConfiguredScanner) -> str:
        locators = self._locators(configured)
        for locator in locators:
            if discover.is_ipv4(locator):
                return locator
        raise RoutingError(
            "could not resolve the configured scanner; check that it is on this network"
        )
