"""Stable physical-scanner identity shared by discovery and configuration."""
import re
import uuid
from typing import Optional


_UUID_SEARCH_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def stable_identity(value: Optional[str]) -> Optional[str]:
    """Normalize cross-backend UUID and serial spellings."""
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
