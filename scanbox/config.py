"""User configuration: which scanner to talk to.

Never committed -- the address differs per machine and network.
"""
import os
import tempfile
from typing import Dict, Optional

CONFIG_FILE = os.environ.get(
    "SCANBOX_CONFIG", os.path.expanduser("~/.config/scanbox/config")
)

TEMPLATE = """\
# Written by `scanbox setup`.
#
# The Bonjour name is derived from the printer's MAC address, so it survives
# DHCP moving the IP. Prefer it over a fixed address.
PRINTER_HOST={host}
"""


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


def save(host: str) -> None:
    """Write atomically: an interrupted write must not leave a half-config."""
    directory = os.path.dirname(CONFIG_FILE)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(TEMPLATE.format(host=host))
        os.replace(tmp, CONFIG_FILE)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def printer_label() -> Optional[str]:
    values = load()
    return values.get("PRINTER_HOST") or values.get("PRINTER_IP")
