"""Scanner discovery over Bonjour, host-side.

The guest cannot do this: multicast does not cross Lima's vzNAT boundary, so
`scanimage -L` inside the VM finds nothing. All discovery and name resolution
happens on macOS and only a plain IPv4 address is handed to the VM.

HP MFPs that speak the proprietary SOAP scan protocol advertise `_scanner._tcp`,
a tighter filter than `_ipp._tcp` -- close to exactly the set hpaio can drive,
and its TXT record carries the model and feeder/flatbed capability.
"""
import re
from typing import Dict, List, Optional

from . import proc

MDNS_TYPE = "_scanner._tcp"

_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def is_ipv4(value: str) -> bool:
    return bool(_IPV4.match(value or ""))


def instances(seconds: float = 5.0) -> List[str]:
    """Service instance names currently advertising, de-duplicated."""
    out = proc.stream_until_timeout(["dns-sd", "-B", MDNS_TYPE, "local"], seconds)
    found, seen = [], set()
    for line in out.splitlines():
        parts = line.split()
        # timestamp Add flags if domain type instance-name...
        if len(parts) < 7 or parts[1] != "Add":
            continue
        name = " ".join(parts[6:])
        if name and name not in seen:
            seen.add(name)
            found.append(name)
    return found


def _resolve_raw(instance: str, seconds: float = 4.0) -> str:
    return proc.stream_until_timeout(
        ["dns-sd", "-L", instance, MDNS_TYPE, "local"], seconds
    )


class Instance:
    """One discovered scanner, with whatever its TXT record told us."""

    def __init__(self, name: str, host: Optional[str], txt: Dict[str, str]):
        self.name = name
        self.host = host
        self.txt = txt

    @property
    def model(self) -> str:
        return self.txt.get("ty") or self.name

    @property
    def has_feeder(self) -> bool:
        return self.txt.get("feeder") == "T"


def resolve_instance(name: str) -> Instance:
    raw = _resolve_raw(name)
    host = None
    m = re.search(r"can be reached at ([^:\s]+):(\d+)", raw)
    if m:
        host = m.group(1).rstrip(".")

    txt: Dict[str, str] = {}
    for line in raw.splitlines():
        if "txtvers=" not in line:
            continue
        # Values may contain backslash-escaped spaces (ty=HP\ LaserJet\ 200\ ...),
        # so protect those before splitting the record on whitespace.
        protected = line.strip().replace("\\ ", "\x00")
        for field in protected.split():
            key, sep, val = field.partition("=")
            if sep:
                txt[key] = val.replace("\x00", " ")
        break
    return Instance(name, host, txt)


def resolve_ipv4(host: str, seconds: float = 4.0) -> Optional[str]:
    """Resolve a .local name to IPv4.

    dns-sd is preferred over ping because it does not need the host to answer
    ICMP; ping is the fallback.
    """
    host = (host or "").rstrip(".")
    out = proc.stream_until_timeout(["dns-sd", "-G", "v4", host], seconds)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] != "Add":
            continue
        for token in parts:
            if is_ipv4(token):
                return token

    res = proc.run(["ping", "-c1", "-t2", host], timeout=5)
    m = re.search(r"\((\d{1,3}(?:\.\d{1,3}){3})\)", res.out)
    return m.group(1) if m else None
