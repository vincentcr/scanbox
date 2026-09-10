"""Backend-neutral scanner configuration."""
from dataclasses import dataclass
import os
import tempfile
from typing import Dict, Optional

CONFIG_FILE = os.environ.get(
    "SCANBOX_CONFIG", os.path.expanduser("~/.config/scanbox/config")
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
    """Persistent physical identity plus locators resolved at scan time."""

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


def path() -> str:
    return CONFIG_FILE


def display_path() -> str:
    home = os.path.expanduser("~")
    return CONFIG_FILE.replace(home, "~", 1) if CONFIG_FILE.startswith(home) else CONFIG_FILE


def exists() -> bool:
    return os.path.isfile(CONFIG_FILE)


def read_raw() -> str:
    with open(CONFIG_FILE) as f:
        return f.read()


def load() -> Dict[str, str]:
    """Read key/value data without migrating it."""
    values = {}
    if not exists():
        return values
    for line in read_raw().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip()
    return values


def _from_values(values: Dict[str, str]) -> Optional[ConfiguredScanner]:
    if not values:
        return None
    identity = values.get("SCANNER_ID") or None
    name = values.get("SCANNER_NAME") or None
    host = values.get("SCANNER_HOST") or None
    address = values.get("SCANNER_ADDRESS") or None
    backend = values.get("SCANNER_BACKEND") or "auto"
    if not any((identity, host, address)):
        return None
    return ConfiguredScanner(identity, name, host, address, backend)


def _serialize(scanner: ConfiguredScanner) -> str:
    lines = [
        "# Written by `scanbox setup`.",
        "# Stable identity is kept separate from locators that may change.",
    ]
    fields = (
        ("SCANNER_ID", scanner.id),
        ("SCANNER_NAME", scanner.name),
        ("SCANNER_HOST", scanner.host),
        ("SCANNER_ADDRESS", scanner.address),
        ("SCANNER_BACKEND", scanner.backend),
    )
    lines.extend("{}={}".format(key, value) for key, value in fields if value)
    return "\n".join(lines) + "\n"


def _write_atomic(contents: str) -> None:
    """Replace the config only after its complete contents reach disk."""
    directory = os.path.dirname(CONFIG_FILE)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config.")
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


def save(scanner: ConfiguredScanner) -> None:
    if not isinstance(scanner, ConfiguredScanner):
        raise ValueError("scanner must be a ConfiguredScanner")
    _write_atomic(_serialize(scanner))


def load_scanner() -> Optional[ConfiguredScanner]:
    return _from_values(load())


def scanner_label() -> Optional[str]:
    scanner = load_scanner()
    return scanner.label if scanner is not None else None
