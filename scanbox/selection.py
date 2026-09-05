"""Current-network scanner inventory and temporary selection.

Discovery returns a backend together with every scanner candidate.  That keeps
the current-LAN path honest: a Bonjour or WS-Discovery advertisement is not
offered to the user unless this process also has an implementation capable of
using it.  Protocol preference and grouping multiple advertisements for one
physical device deliberately belong to the router, not this module.
"""
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence, Tuple

from .backends.wsd import WSDBackend
from .contracts import Backend, BackendError, Scanner


class SelectionError(ValueError):
    """A requested scanner cannot be selected unambiguously."""


@dataclass(frozen=True)
class Candidate:
    """A scanner advertisement paired with the backend that can use it."""

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
class DiscoveryFailure:
    backend: str
    message: str


@dataclass(frozen=True)
class Inventory:
    candidates: Tuple[Candidate, ...]
    failures: Tuple[DiscoveryFailure, ...] = ()


class Catalog:
    """Discover only candidates backed by implementations available here."""

    def __init__(self, backends: Iterable[Backend]) -> None:
        self.backends = tuple(backends)

    def discover(self) -> Inventory:
        candidates = []
        failures = []
        seen = set()
        for backend in self.backends:
            try:
                scanners = backend.discover()
            except BackendError as error:
                failures.append(DiscoveryFailure(backend.name, str(error)))
                continue
            except (OSError, ValueError) as error:
                failures.append(DiscoveryFailure(backend.name, str(error)))
                continue

            for scanner in scanners:
                try:
                    candidate = Candidate(scanner, backend)
                except ValueError as error:
                    failures.append(DiscoveryFailure(backend.name, str(error)))
                    continue
                key = (scanner.backend, scanner.id, scanner.endpoint)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(candidate)

        candidates.sort(key=lambda item: (
            item.scanner.name.casefold(), item.scanner.id.casefold(),
            item.scanner.backend, item.scanner.endpoint,
        ))
        return Inventory(tuple(candidates), tuple(failures))


def current_network_catalog(*, discovery_seconds: float = 3.0) -> Catalog:
    """Build the production catalog without starting or inspecting a guest."""
    return Catalog((WSDBackend(discovery_seconds=discovery_seconds),))


def describe(candidate: Candidate) -> str:
    scanner = candidate.scanner
    return "{} [{}] via {}".format(scanner.name, scanner.id, scanner.backend)


def _matching(candidates: Sequence[Candidate], selector: str) -> Tuple[Candidate, ...]:
    selector_key = selector.casefold()
    if selector_key == "auto":
        return tuple(candidates)
    return tuple(
        candidate for candidate in candidates
        if candidate.scanner.id.casefold() == selector_key
        or candidate.scanner.name.casefold() == selector_key
    )


def _ambiguous_message(candidates: Sequence[Candidate], selector: str) -> str:
    heading = (
        "more than one usable scanner was found on this network"
        if selector.casefold() == "auto"
        else "more than one scanner matches {!r}".format(selector)
    )
    choices = "\n".join("  - " + describe(item) for item in candidates)
    return "{}:\n{}\nRe-run with --scanner NAME or a stable ID.".format(
        heading, choices
    )


def select(candidates: Sequence[Candidate], selector: str, *,
           interactive: bool = False,
           ask: Optional[Callable[[str], str]] = None,
           say: Optional[Callable[[str], None]] = None) -> Candidate:
    """Select by exact name/stable ID, or apply the interactive auto policy."""
    selector = (selector or "").strip()
    if not selector:
        raise SelectionError("scanner selector must not be empty")
    matches = _matching(tuple(candidates), selector)
    if not matches:
        if selector.casefold() == "auto":
            raise SelectionError("no usable scanners found on this network")
        raise SelectionError("no scanner found matching {!r}".format(selector))
    if len(matches) == 1:
        return matches[0]
    if not interactive or ask is None:
        raise SelectionError(_ambiguous_message(matches, selector))

    emit = say or (lambda _message: None)
    emit("Found {} matching scanners:".format(len(matches)))
    emit("")
    for index, candidate in enumerate(matches, 1):
        emit("  {}) {}".format(index, describe(candidate)))
    emit("")
    while True:
        choice = ask("Which one? [1-{}] ".format(len(matches)))
        if choice.isdigit() and 1 <= int(choice) <= len(matches):
            return matches[int(choice) - 1]
        emit("  please enter a number between 1 and {}".format(len(matches)))
