"""Scanner acquisition backends."""

from .hplip import BonjourHPLIPBackend, HPLIPBackend
from .wsd import WSDBackend

__all__ = ["BonjourHPLIPBackend", "HPLIPBackend", "WSDBackend"]
