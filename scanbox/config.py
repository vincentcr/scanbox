"""Persistent registry of scanners and their known-good backends."""
from dataclasses import dataclass
import json
import os
import tempfile
from typing import Optional, Tuple

from .identity import stable_identity


CONFIG_FILE = os.environ.get(
    "SCANBOX_CONFIG", os.path.expanduser("~/.config/scanbox/scanners.json")
)

BACKENDS = ("auto", "wsd", "hplip", "imagecapture")


def _optional(value: Optional[str], field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{} must be a non-empty string".format(field))
    value = value.strip()
    if "\n" in value or "\r" in value:
        raise ValueError("{} must fit on one line".format(field))
    return value


@dataclass(frozen=True)
class ConfiguredScanner:
    """A remembered physical scanner and the backend known to operate it."""

    id: Optional[str] = None
    name: Optional[str] = None
    host: Optional[str] = None
    address: Optional[str] = None
    backend: str = "auto"

    def __post_init__(self) -> None:
        for field in ("id", "name", "host", "address"):
            object.__setattr__(self, field, _optional(getattr(self, field), field))
        backend = str(self.backend).strip().lower()
        if backend not in BACKENDS:
            raise ValueError("unknown scanner backend: {!r}".format(self.backend))
        object.__setattr__(self, "backend", backend)
        if not any((self.id, self.host, self.address)):
            raise ValueError("configured scanner needs an identity or locator")

    @property
    def label(self) -> str:
        return self.name or self.id or self.host or self.address or "unset"

    @property
    def locator(self) -> Optional[str]:
        return self.host or self.address

    @property
    def key(self) -> str:
        identity = stable_identity(self.id)
        if identity:
            return identity
        if self.id:
            return "id:" + self.id.casefold()
        if self.host:
            return "host:" + self.host.rstrip(".").casefold()
        return "address:" + (self.address or "").casefold()


@dataclass(frozen=True)
class ScannerRegistry:
    scanners: Tuple[ConfiguredScanner, ...] = ()
    preferred: Optional[str] = None

    def __post_init__(self) -> None:
        scanners = tuple(self.scanners)
        if not all(isinstance(scanner, ConfiguredScanner) for scanner in scanners):
            raise ValueError("registry entries must be ConfiguredScanner values")
        keys = tuple(scanner.key for scanner in scanners)
        if len(set(keys)) != len(keys):
            raise ValueError("registry contains duplicate scanner identities")
        preferred = self.preferred
        if scanners and preferred is None:
            preferred = scanners[0].key
        if preferred is not None and preferred not in keys:
            raise ValueError("preferred scanner is not in the registry")
        object.__setattr__(self, "scanners", scanners)
        object.__setattr__(self, "preferred", preferred)

    @property
    def preferred_scanner(self) -> Optional[ConfiguredScanner]:
        return next(
            (scanner for scanner in self.scanners if scanner.key == self.preferred),
            None,
        )

    def ordered(self) -> Tuple[ConfiguredScanner, ...]:
        preferred = self.preferred_scanner
        if preferred is None:
            return self.scanners
        return (preferred,) + tuple(
            scanner for scanner in self.scanners if scanner.key != preferred.key
        )


def path() -> str:
    return CONFIG_FILE


def display_path() -> str:
    home = os.path.expanduser("~")
    return CONFIG_FILE.replace(home, "~", 1) if CONFIG_FILE.startswith(home) else CONFIG_FILE


def exists() -> bool:
    return os.path.isfile(CONFIG_FILE)


def read_raw() -> str:
    with open(CONFIG_FILE) as stream:
        return stream.read()


def _scanner_data(scanner: ConfiguredScanner) -> dict:
    return {
        key: value for key, value in (
            ("id", scanner.id),
            ("name", scanner.name),
            ("host", scanner.host),
            ("address", scanner.address),
            ("backend", scanner.backend),
        ) if value is not None
    }


def _serialize(registry: ScannerRegistry) -> str:
    data = {
        "version": 1,
        "preferred": registry.preferred,
        "scanners": [_scanner_data(scanner) for scanner in registry.scanners],
    }
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _write_atomic(contents: str) -> None:
    directory = os.path.dirname(CONFIG_FILE)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".scanners.")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, CONFIG_FILE)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def save_registry(registry: ScannerRegistry) -> None:
    if not isinstance(registry, ScannerRegistry):
        raise ValueError("registry must be a ScannerRegistry")
    _write_atomic(_serialize(registry))


def load_registry() -> ScannerRegistry:
    if not exists():
        return ScannerRegistry()
    try:
        data = json.loads(read_raw())
    except (OSError, ValueError) as error:
        raise ValueError("invalid scanner registry: {}".format(error))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("unsupported scanner registry format")
    raw_scanners = data.get("scanners")
    if not isinstance(raw_scanners, list):
        raise ValueError("scanner registry needs a scanners list")
    try:
        scanners = tuple(ConfiguredScanner(**item) for item in raw_scanners)
        return ScannerRegistry(scanners, data.get("preferred"))
    except (TypeError, ValueError) as error:
        raise ValueError("invalid scanner registry: {}".format(error))


def remember(scanner: ConfiguredScanner, *, preferred: bool = False) -> ScannerRegistry:
    if not isinstance(scanner, ConfiguredScanner):
        raise ValueError("scanner must be a ConfiguredScanner")
    current = load_registry()
    scanners = list(current.scanners)
    replaced_key = None
    for index, saved in enumerate(scanners):
        same_host = (
            saved.host is not None and scanner.host is not None
            and saved.host.rstrip(".").casefold()
            == scanner.host.rstrip(".").casefold()
        )
        if saved.key == scanner.key or same_host:
            replaced_key = saved.key
            scanners[index] = scanner
            break
    else:
        scanners.append(scanner)
    preferred_key = scanner.key if preferred or not current.scanners else current.preferred
    if replaced_key == current.preferred:
        preferred_key = scanner.key
    updated = ScannerRegistry(tuple(scanners), preferred_key)
    save_registry(updated)
    return updated


def _matches(scanner: ConfiguredScanner, selector: str) -> bool:
    selector = selector.casefold()
    return selector in {
        scanner.key.casefold(),
        (scanner.id or "").casefold(),
        scanner.label.casefold(),
        (scanner.locator or "").casefold(),
    }


def forget(selector: str) -> ScannerRegistry:
    selector = (selector or "").strip()
    if not selector:
        raise ValueError("scanner selector must not be empty")
    current = load_registry()
    matches = tuple(scanner for scanner in current.scanners if _matches(scanner, selector))
    if not matches:
        raise ValueError("no saved scanner matches {!r}".format(selector))
    if len(matches) > 1:
        raise ValueError("more than one saved scanner matches {!r}".format(selector))
    removed = matches[0]
    scanners = tuple(scanner for scanner in current.scanners if scanner.key != removed.key)
    preferred = current.preferred if current.preferred != removed.key else None
    updated = ScannerRegistry(scanners, preferred)
    save_registry(updated)
    return updated


def set_preferred(selector: str) -> ScannerRegistry:
    current = load_registry()
    selector = (selector or "").strip()
    matches = tuple(scanner for scanner in current.scanners if _matches(scanner, selector))
    if len(matches) != 1:
        raise ValueError("saved scanner selector must match exactly one scanner")
    updated = ScannerRegistry(current.scanners, matches[0].key)
    save_registry(updated)
    return updated
