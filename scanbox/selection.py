"""Unified current-network discovery, grouping, and scanner selection."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence, Tuple

from . import config
from .backends.hplip import BonjourHPLIPBackend
from .backends.wsd import WSDBackend
from .contracts import Backend, BackendError, Scanner
from .identity import stable_identity


class SelectionError(ValueError):
    """A scanner cannot be discovered, selected, or validated unambiguously."""


@dataclass(frozen=True)
class Candidate:
    scanner: Scanner
    backend: Backend

    def __post_init__(self) -> None:
        if not isinstance(self.scanner, Scanner):
            raise ValueError("scanner must be a Scanner")
        if not isinstance(self.backend, Backend):
            raise ValueError("backend must implement Backend")
        if self.scanner.backend != self.backend.name:
            raise ValueError("scanner and backend names do not match")


@dataclass(frozen=True)
class PhysicalCandidate:
    """Every available backend advertisement for one physical scanner."""

    identity: str
    candidates: Tuple[Candidate, ...]

    def __post_init__(self) -> None:
        if not self.identity:
            raise ValueError("physical scanner identity must not be empty")
        if not self.candidates:
            raise ValueError("physical scanner must have at least one candidate")

    @property
    def name(self) -> str:
        return self.candidates[0].scanner.name

    @property
    def backend_names(self) -> Tuple[str, ...]:
        return tuple(candidate.backend.name for candidate in self.candidates)

    @property
    def display_id(self) -> str:
        identity = stable_identity(self.candidates[0].scanner.id)
        return identity or "<not advertised>"


@dataclass(frozen=True)
class DiscoveryFailure:
    backend: str
    message: str


@dataclass(frozen=True)
class Inventory:
    candidates: Tuple[Candidate, ...]
    failures: Tuple[DiscoveryFailure, ...] = ()

    @property
    def physical(self) -> Tuple[PhysicalCandidate, ...]:
        return group_candidates(self.candidates)


class Catalog:
    """Discover all available backends concurrently without moving paper."""

    def __init__(self, backends: Iterable[Backend]) -> None:
        self.backends = tuple(backends)

    def discover(self) -> Inventory:
        if not self.backends:
            return Inventory(())
        with ThreadPoolExecutor(max_workers=len(self.backends)) as pool:
            results = tuple(pool.map(_discover_backend, self.backends))

        candidates = []
        failures = []
        seen = set()
        for backend, scanners, error in results:
            if error is not None:
                failures.append(DiscoveryFailure(backend.name, str(error)))
                continue
            for scanner in scanners:
                try:
                    candidate = Candidate(scanner, backend)
                except ValueError as candidate_error:
                    failures.append(DiscoveryFailure(backend.name, str(candidate_error)))
                    continue
                key = (scanner.backend, scanner.id, scanner.endpoint)
                if key not in seen:
                    seen.add(key)
                    candidates.append(candidate)

        candidates.sort(key=lambda item: (
            item.scanner.name.casefold(), item.scanner.id.casefold(),
            item.scanner.backend, item.scanner.endpoint,
        ))
        return Inventory(tuple(candidates), tuple(failures))


def _discover_backend(backend: Backend):
    try:
        return backend, tuple(backend.discover()), None
    except (BackendError, OSError, ValueError) as error:
        return backend, (), error


def current_network_catalog(*, discovery_seconds: float = 3.0) -> Catalog:
    """Build the production catalog for every implemented network backend."""
    return Catalog((
        WSDBackend(discovery_seconds=discovery_seconds),
        BonjourHPLIPBackend(discovery_seconds=discovery_seconds),
    ))


def group_candidates(candidates: Iterable[Candidate]) -> Tuple[PhysicalCandidate, ...]:
    groups = {}
    for index, candidate in enumerate(candidates):
        scanner = candidate.scanner
        identity = stable_identity(scanner.id)
        key = identity or "candidate:{}:{}:{}".format(
            scanner.backend, scanner.id, index
        )
        groups.setdefault(key, []).append(candidate)

    backend_order = {"wsd": 0, "hplip": 1, "imagecapture": 2}
    result = []
    for key, members in groups.items():
        ordered = tuple(sorted(members, key=lambda item: (
            backend_order.get(item.backend.name, 99),
            item.scanner.name.casefold(), item.scanner.endpoint,
        )))
        result.append(PhysicalCandidate(key, ordered))
    return tuple(sorted(result, key=lambda item: (
        item.name.casefold(), item.identity
    )))


def describe(candidate) -> str:
    if isinstance(candidate, Candidate):
        return "{} [{}] via {}".format(
            candidate.scanner.name, candidate.scanner.id, candidate.scanner.backend
        )
    return "{} [{}] via {}".format(
        candidate.name,
        candidate.display_id,
        ", ".join(candidate.backend_names),
    )


def _group_matches(group: PhysicalCandidate, selector: str) -> bool:
    selector = selector.casefold()
    return any(
        selector in (candidate.scanner.id.casefold(), candidate.scanner.name.casefold())
        for candidate in group.candidates
    ) or group.identity.casefold() == selector


def select_group(groups: Sequence[PhysicalCandidate], selector: str, *,
                 interactive: bool = False,
                 ask: Optional[Callable[[str], str]] = None,
                 say: Optional[Callable[[str], None]] = None) -> PhysicalCandidate:
    selector = (selector or "").strip()
    if not selector:
        raise SelectionError("scanner selector must not be empty")
    matches = tuple(groups) if selector.casefold() == "auto" else tuple(
        group for group in groups if _group_matches(group, selector)
    )
    if not matches:
        if selector.casefold() == "auto":
            raise SelectionError("no usable scanners found on this network")
        raise SelectionError("no scanner found matching {!r}".format(selector))
    if len(matches) == 1:
        return matches[0]
    if not interactive or ask is None:
        choices = "\n".join("  - " + describe(item) for item in matches)
        raise SelectionError(
            "more than one scanner matches {!r}:\n{}".format(selector, choices)
        )
    emit = say or (lambda _message: None)
    emit("Found {} matching scanners:".format(len(matches)))
    emit("")
    for index, group in enumerate(matches, 1):
        emit("  {}) {}".format(index, describe(group)))
    emit("")
    while True:
        choice = ask("Which one? [1-{}] ".format(len(matches)))
        if choice.isdigit() and 1 <= int(choice) <= len(matches):
            return matches[int(choice) - 1]
        emit("  please enter a number between 1 and {}".format(len(matches)))


def select(candidates: Sequence[Candidate], selector: str, **kwargs) -> Candidate:
    """Select a physical scanner when callers require exactly one backend."""
    group = select_group(group_candidates(candidates), selector, **kwargs)
    if len(group.candidates) != 1:
        raise SelectionError("scanner has more than one available backend")
    return group.candidates[0]


def candidate_variants(group: PhysicalCandidate,
                       backend: Optional[str] = None) -> Tuple[Candidate, ...]:
    if backend in (None, "auto"):
        return group.candidates
    return tuple(
        candidate for candidate in group.candidates
        if candidate.backend.name == backend
    )


def validate(group: PhysicalCandidate,
             backend: Optional[str] = None) -> Candidate:
    diagnostics = []
    variants = candidate_variants(group, backend)
    if not variants:
        raise SelectionError(
            "{} is not available via {}".format(group.name, backend)
        )
    for candidate in variants:
        try:
            candidate.backend.inspect(candidate.scanner)
            return candidate
        except (BackendError, ValueError) as error:
            diagnostics.append("{}: {}".format(candidate.backend.name, error))
            release = getattr(candidate.backend, "release", None)
            if release is not None:
                release(60)
    raise SelectionError(
        "no backend could inspect {} ({})".format(group.name, "; ".join(diagnostics))
    )


def configured(candidate: Candidate) -> config.ConfiguredScanner:
    scanner = candidate.scanner
    scanner_id = None if scanner.id.startswith("bonjour:") else scanner.id
    return config.ConfiguredScanner(
        id=scanner_id,
        name=scanner.name,
        host=scanner.host,
        address=scanner.address,
        backend=scanner.backend,
    )


def matches_saved(group: PhysicalCandidate,
                  saved: config.ConfiguredScanner) -> bool:
    saved_identity = stable_identity(saved.id)
    if saved_identity and group.identity == saved_identity:
        return True
    locators = {
        value.rstrip(".").casefold()
        for value in (saved.host, saved.address) if value
    }
    for candidate in group.candidates:
        candidate_locators = {
            value.rstrip(".").casefold()
            for value in (candidate.scanner.host, candidate.scanner.address) if value
        }
        if locators.intersection(candidate_locators):
            return True
    return False
