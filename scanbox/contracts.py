"""Backend-neutral scanner and acquisition contracts.

The CLI, output assembler, and backend router should be able to reason about a
scan without knowing whether ImageCaptureCore, sane-airscan, or HPLIP performs
the acquisition.  This module is deliberately standard-library-only and has no
imports from the current HP implementation.

Lifecycle invariants matter because scanning moves physical paper:

* ``discover()``, ``inspect()``, and ``prepare()`` must not move paper.
* ``ScanJob.scan()`` is the only boundary allowed to start acquisition.
* ``ScanJob.cancel()`` is idempotent.
* ``ScanJob.result`` is side-effect-free and remains ``None`` until a complete
  result is available.

These contracts are not wired into the production CLI yet.  Issue #7 defines
the vocabulary that the native, WSD, and legacy implementations will share.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
import os
from typing import Optional, Sequence, Tuple, Union

__all__ = [
    "Backend",
    "BackendError",
    "BackendErrorCode",
    "Capabilities",
    "PageSize",
    "ScanJob",
    "ScanMode",
    "ScanPage",
    "ScanRequest",
    "ScanResult",
    "Scanner",
    "ScanSource",
    "SourceCapabilities",
    "UnsupportedRequest",
]


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ScanSource(_StringEnum):
    """Normalized input sources used across all backends."""

    AUTO = "auto"
    FLATBED = "flatbed"
    FEEDER = "feeder"
    FEEDER_DUPLEX = "feeder-duplex"
    POSITIVE_TRANSPARENCY = "positive-transparency"
    NEGATIVE_TRANSPARENCY = "negative-transparency"

    @classmethod
    def parse(cls, value: Union["ScanSource", str]) -> "ScanSource":
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise ValueError("source must be a string or ScanSource")
        key = value.strip().lower().replace("_", "-")
        aliases = {
            "auto": cls.AUTO,
            "bed": cls.FLATBED,
            "flatbed": cls.FLATBED,
            "adf": cls.FEEDER,
            "feeder": cls.FEEDER,
            "adf duplex": cls.FEEDER_DUPLEX,
            "adf-duplex": cls.FEEDER_DUPLEX,
            "feeder duplex": cls.FEEDER_DUPLEX,
            "feeder-duplex": cls.FEEDER_DUPLEX,
            "positive transparency": cls.POSITIVE_TRANSPARENCY,
            "positive-transparency": cls.POSITIVE_TRANSPARENCY,
            "negative transparency": cls.NEGATIVE_TRANSPARENCY,
            "negative-transparency": cls.NEGATIVE_TRANSPARENCY,
        }
        try:
            return aliases[key]
        except KeyError:
            raise ValueError("unknown scan source: {!r}".format(value))


class ScanMode(_StringEnum):
    """Normalized raster modes; backend spellings are parsed at the edge."""

    COLOR = "color"
    GRAYSCALE = "grayscale"
    LINEART = "lineart"

    @classmethod
    def parse(cls, value: Union["ScanMode", str]) -> "ScanMode":
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise ValueError("mode must be a string or ScanMode")
        key = value.strip().lower().replace("_", "-")
        aliases = {
            "color": cls.COLOR,
            "colour": cls.COLOR,
            "color-rgb": cls.COLOR,
            "palette": cls.COLOR,
            "color-cmy": cls.COLOR,
            "color-cmyk": cls.COLOR,
            "color-yuv": cls.COLOR,
            "color-yuvk": cls.COLOR,
            "color-ciexyz": cls.COLOR,
            "gray": cls.GRAYSCALE,
            "grey": cls.GRAYSCALE,
            "grayscale": cls.GRAYSCALE,
            "greyscale": cls.GRAYSCALE,
            "lineart": cls.LINEART,
            "line-art": cls.LINEART,
            "bw": cls.LINEART,
            "black-and-white": cls.LINEART,
        }
        try:
            return aliases[key]
        except KeyError:
            raise ValueError("unknown scan mode: {!r}".format(value))


class PageSize(_StringEnum):
    """Page choices currently exposed by the CLI."""

    AUTO = "auto"
    LETTER = "letter"
    LEGAL = "legal"
    A4 = "a4"
    MAX = "max"

    @classmethod
    def parse(cls, value: Union["PageSize", str]) -> "PageSize":
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise ValueError("page size must be a string or PageSize")
        key = value.strip().lower()
        try:
            return cls(key)
        except ValueError:
            raise ValueError("unknown page size: {!r}".format(value))


class BackendErrorCode(_StringEnum):
    """Stable error categories that user-facing code can handle uniformly."""

    UNAVAILABLE = "unavailable"
    BUSY = "busy"
    UNSUPPORTED = "unsupported"
    EMPTY_FEEDER = "empty-feeder"
    JAMMED = "jammed"
    CANCELLED = "cancelled"
    IO = "io"
    PROTOCOL = "protocol"


class BackendError(RuntimeError):
    """A backend failure translated into backend-independent vocabulary."""

    def __init__(self, code: BackendErrorCode, message: str, *,
                 backend: Optional[str] = None, retryable: bool = False) -> None:
        if not isinstance(code, BackendErrorCode):
            try:
                code = BackendErrorCode(code)
            except (TypeError, ValueError):
                raise ValueError("unknown backend error code: {!r}".format(code))
        message = _required_text(message, "message")
        if backend is not None:
            backend = _required_text(backend, "backend")
        if not isinstance(retryable, bool):
            raise ValueError("retryable must be a bool")
        super().__init__(message)
        self.code = code
        self.backend = backend
        self.retryable = retryable


class UnsupportedRequest(ValueError):
    """The selected scanner cannot satisfy a normalized scan request."""


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{} must be a non-empty string".format(field))
    return value.strip()


def _optional_text(value: Optional[str], field: str) -> Optional[str]:
    if value is None:
        return None
    return _required_text(value, field)


def _positive_int(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("{} must be a positive integer".format(field))
    return value


@dataclass(frozen=True)
class Scanner:
    """One backend candidate for a physical scanner.

    ``id`` is the stable physical identity used to group advertisements from
    different backends. ``backend`` names the implementation that produced
    this candidate, while ``endpoint`` is an opaque locator understood only by
    that backend. It may be an ImageCapture persistent ID, a WSD URL, or an
    HPLIP URI; shared code must not parse it.
    """

    id: str
    name: str
    backend: str
    endpoint: str
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    transport: Optional[str] = None
    serial_number: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required_text(self.id, "id"))
        object.__setattr__(self, "name", _required_text(self.name, "name"))
        object.__setattr__(self, "backend", _required_text(self.backend, "backend"))
        object.__setattr__(self, "endpoint", _required_text(self.endpoint, "endpoint"))
        for field in ("manufacturer", "model", "transport", "serial_number"):
            object.__setattr__(self, field, _optional_text(getattr(self, field), field))


_MODE_ORDER = {
    ScanMode.COLOR: 0,
    ScanMode.GRAYSCALE: 1,
    ScanMode.LINEART: 2,
}
_SOURCE_ORDER = {
    ScanSource.FLATBED: 0,
    ScanSource.FEEDER: 1,
    ScanSource.FEEDER_DUPLEX: 2,
    ScanSource.POSITIVE_TRANSPARENCY: 3,
    ScanSource.NEGATIVE_TRANSPARENCY: 4,
}


@dataclass(frozen=True)
class SourceCapabilities:
    """Capabilities that can differ between a scanner's physical sources."""

    source: ScanSource
    modes: Tuple[ScanMode, ...]
    resolutions: Tuple[int, ...]
    supports_lossless: Optional[bool] = None
    modes_complete: bool = True
    bit_depths: Tuple[int, ...] = ()
    native_resolution: Optional[Tuple[int, int]] = None
    max_size_mm: Optional[Tuple[float, float]] = None

    def __post_init__(self) -> None:
        source = ScanSource.parse(self.source)
        if source is ScanSource.AUTO:
            raise ValueError("auto is a request policy, not a scanner source")

        modes = tuple(ScanMode.parse(mode) for mode in self.modes)
        if not modes and self.modes_complete:
            raise ValueError("modes must not be empty when modes_complete is true")
        if len(set(modes)) != len(modes):
            raise ValueError("modes must not contain duplicates")
        modes = tuple(sorted(modes, key=_MODE_ORDER.__getitem__))

        resolutions = tuple(
            _positive_int(value, "resolution") for value in self.resolutions
        )
        if not resolutions:
            raise ValueError("resolutions must not be empty")
        if len(set(resolutions)) != len(resolutions):
            raise ValueError("resolutions must not contain duplicates")
        resolutions = tuple(sorted(resolutions))

        bit_depths = tuple(_positive_int(value, "bit depth") for value in self.bit_depths)
        if len(set(bit_depths)) != len(bit_depths):
            raise ValueError("bit_depths must not contain duplicates")
        bit_depths = tuple(sorted(bit_depths))

        if self.supports_lossless is not None and not isinstance(
            self.supports_lossless, bool
        ):
            raise ValueError("supports_lossless must be a bool or None")
        if not isinstance(self.modes_complete, bool):
            raise ValueError("modes_complete must be a bool")

        native_resolution = _positive_pair(self.native_resolution, "native_resolution")
        max_size_mm = _positive_pair(self.max_size_mm, "max_size_mm")

        object.__setattr__(self, "source", source)
        object.__setattr__(self, "modes", modes)
        object.__setattr__(self, "resolutions", resolutions)
        object.__setattr__(self, "bit_depths", bit_depths)
        object.__setattr__(self, "native_resolution", native_resolution)
        object.__setattr__(self, "max_size_mm", max_size_mm)


def _positive_pair(value, field: str):
    if value is None:
        return None
    try:
        pair = tuple(value)
    except TypeError:
        raise ValueError("{} must be a pair".format(field))
    if len(pair) != 2:
        raise ValueError("{} must be a pair".format(field))
    for item in pair:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or item <= 0:
            raise ValueError("{} values must be positive numbers".format(field))
    return pair


@dataclass(frozen=True)
class ScanRequest:
    """Acquisition settings, independent of final PDF/image assembly."""

    scanner_id: str
    source: ScanSource = ScanSource.AUTO
    mode: ScanMode = ScanMode.COLOR
    resolution: int = 300
    page_size: PageSize = PageSize.AUTO
    lossless: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "scanner_id", _required_text(self.scanner_id, "scanner_id"))
        object.__setattr__(self, "source", ScanSource.parse(self.source))
        object.__setattr__(self, "mode", ScanMode.parse(self.mode))
        object.__setattr__(self, "resolution", _positive_int(self.resolution, "resolution"))
        object.__setattr__(self, "page_size", PageSize.parse(self.page_size))
        if not isinstance(self.lossless, bool):
            raise ValueError("lossless must be a bool")


@dataclass(frozen=True)
class Capabilities:
    """Source-specific capabilities reported by one scanner candidate."""

    scanner_id: str
    sources: Tuple[SourceCapabilities, ...]

    def __post_init__(self) -> None:
        scanner_id = _required_text(self.scanner_id, "scanner_id")
        sources = tuple(self.sources)
        if not sources:
            raise ValueError("sources must not be empty")
        if not all(isinstance(source, SourceCapabilities) for source in sources):
            raise ValueError("sources must contain SourceCapabilities values")
        names = [source.source for source in sources]
        if len(set(names)) != len(names):
            raise ValueError("sources must not contain duplicate source types")
        sources = tuple(sorted(sources, key=lambda item: _SOURCE_ORDER[item.source]))
        object.__setattr__(self, "scanner_id", scanner_id)
        object.__setattr__(self, "sources", sources)

    def source(self, value: Union[ScanSource, str]) -> Optional[SourceCapabilities]:
        """Return one concrete source, or ``None`` when it is unavailable."""
        normalized = ScanSource.parse(value)
        if normalized is ScanSource.AUTO:
            raise ValueError("auto does not identify one concrete source")
        for candidate in self.sources:
            if candidate.source is normalized:
                return candidate
        return None

    def compatible_sources(self, request: ScanRequest) -> Tuple[SourceCapabilities, ...]:
        """Validate a request and return sources that can satisfy it.

        For ``auto``, the backend still decides which compatible source is
        physically active. This method only constrains that decision; it does
        not probe the feeder or move paper.
        """
        if not isinstance(request, ScanRequest):
            raise ValueError("request must be a ScanRequest")
        if request.scanner_id != self.scanner_id:
            raise UnsupportedRequest("request targets a different scanner")

        candidates = self.sources
        if request.source is not ScanSource.AUTO:
            candidates = tuple(
                source for source in candidates if source.source is request.source
            )
            if not candidates:
                raise UnsupportedRequest(
                    "source {} is not available".format(request.source.value)
                )

        matches = tuple(
            source for source in candidates
            if (request.mode in source.modes or not source.modes_complete)
            and request.resolution in source.resolutions
            and (not request.lossless or source.supports_lossless is not False)
        )
        if matches:
            return matches

        requirements = "{} at {} dpi".format(request.mode.value, request.resolution)
        if request.lossless:
            requirements += " with lossless transfer"
        raise UnsupportedRequest("no selected source supports {}".format(requirements))


@dataclass(frozen=True)
class ScanPage:
    """One acquired page staged at a host-readable absolute path."""

    index: int
    path: str
    media_type: str
    width_px: Optional[int] = None
    height_px: Optional[int] = None
    resolution: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "index", _positive_int(self.index, "index"))
        path = _required_text(self.path, "path")
        if not os.path.isabs(path):
            raise ValueError("path must be an absolute host path")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "media_type", _required_text(self.media_type, "media_type"))

        dimensions = (self.width_px, self.height_px)
        if (dimensions[0] is None) != (dimensions[1] is None):
            raise ValueError("width_px and height_px must be provided together")
        if dimensions[0] is not None:
            object.__setattr__(self, "width_px", _positive_int(dimensions[0], "width_px"))
            object.__setattr__(self, "height_px", _positive_int(dimensions[1], "height_px"))
        if self.resolution is not None:
            object.__setattr__(
                self, "resolution", _positive_int(self.resolution, "resolution")
            )


@dataclass(frozen=True)
class ScanResult:
    """Completed acquisition, before backend-neutral output assembly."""

    scanner_id: str
    backend: str
    source: ScanSource
    pages: Tuple[ScanPage, ...]
    truncated: bool = False
    warnings: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "scanner_id", _required_text(self.scanner_id, "scanner_id"))
        object.__setattr__(self, "backend", _required_text(self.backend, "backend"))
        source = ScanSource.parse(self.source)
        if source is ScanSource.AUTO:
            raise ValueError("a completed result must report the source actually used")

        pages = tuple(self.pages)
        if not pages or not all(isinstance(page, ScanPage) for page in pages):
            raise ValueError("pages must contain at least one ScanPage")
        indexes = tuple(page.index for page in pages)
        if indexes != tuple(range(1, len(pages) + 1)):
            raise ValueError("page indexes must be consecutive and start at 1")
        if not isinstance(self.truncated, bool):
            raise ValueError("truncated must be a bool")
        warnings = tuple(_required_text(warning, "warning") for warning in self.warnings)

        object.__setattr__(self, "source", source)
        object.__setattr__(self, "pages", pages)
        object.__setattr__(self, "warnings", warnings)


class ScanJob(ABC):
    """A prepared acquisition owned by exactly one backend."""

    @abstractmethod
    def scan(self) -> ScanResult:
        """Move paper, acquire all pages, and return the completed result."""
        raise NotImplementedError

    @abstractmethod
    def cancel(self) -> None:
        """Stop acquisition if active; safe to call repeatedly."""
        raise NotImplementedError

    @property
    @abstractmethod
    def result(self) -> Optional[ScanResult]:
        """Return the completed result without performing any work."""
        raise NotImplementedError


class Backend(ABC):
    """Discovery, inspection, and preparation implemented by one protocol."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable backend identifier used in ``Scanner.backend``."""
        raise NotImplementedError

    @abstractmethod
    def discover(self) -> Sequence[Scanner]:
        """Return current candidates without moving paper."""
        raise NotImplementedError

    @abstractmethod
    def inspect(self, scanner: Scanner) -> Capabilities:
        """Read capabilities without moving paper."""
        raise NotImplementedError

    @abstractmethod
    def prepare(self, scanner: Scanner, request: ScanRequest) -> ScanJob:
        """Validate and reserve resources without starting acquisition."""
        raise NotImplementedError
