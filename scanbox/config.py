"""Backend-neutral scanner configuration and legacy migration.

The configured scanner is identified independently of its current address or
acquisition implementation. ``PRINTER_HOST`` and ``PRINTER_IP`` remain valid
inputs and are migrated to the new schema atomically when the configured path
is used.
"""
from dataclasses import dataclass
import os
import tempfile
from typing import Dict, Optional

CONFIG_FILE = os.environ.get(
    "SCANBOX_CONFIG", os.path.expanduser("~/.config/scanbox/config")
)

PROTOCOLS = ("auto", "native", "wsd", "legacy")


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
    protocol: str = "auto"

    def __post_init__(self) -> None:
        for field in ("id", "name", "host", "address"):
            object.__setattr__(self, field, _optional(getattr(self, field), field))
        protocol = str(self.protocol).strip().lower()
        if protocol not in PROTOCOLS:
            raise ValueError("unknown scanner protocol: {!r}".format(self.protocol))
        object.__setattr__(self, "protocol", protocol)
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
    modern = any(key.startswith("SCANNER_") for key in values)
    if modern:
        identity = values.get("SCANNER_ID") or None
        name = values.get("SCANNER_NAME") or None
        host = values.get("SCANNER_HOST") or None
        address = values.get("SCANNER_ADDRESS") or None
        protocol = values.get("SCANNER_PROTOCOL") or "auto"
    else:
        identity = None
        name = None
        host = values.get("PRINTER_HOST") or None
        address = values.get("PRINTER_IP") or None
        protocol = "auto"
    if not any((identity, host, address)):
        return None
    return ConfiguredScanner(identity, name, host, address, protocol)


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
        ("SCANNER_PROTOCOL", scanner.protocol),
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


def load_scanner(*, migrate: bool = False) -> Optional[ConfiguredScanner]:
    """Load either schema and optionally replace a legacy file atomically."""
    values = load()
    scanner = _from_values(values)
    if scanner is not None and migrate and not any(
            key.startswith("SCANNER_") for key in values):
        save(scanner)
    return scanner


def scanner_label() -> Optional[str]:
    scanner = load_scanner()
    return scanner.label if scanner is not None else None


def printer_label() -> Optional[str]:
    """Compatibility alias for callers predating backend-neutral config."""
    return scanner_label()
