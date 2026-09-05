import unittest
from dataclasses import FrozenInstanceError
from typing import Optional, Sequence

from scanbox.contracts import (
    Backend,
    BackendError,
    BackendErrorCode,
    Capabilities,
    PageSize,
    ScanJob,
    ScanMode,
    ScanPage,
    ScanRequest,
    ScanResult,
    Scanner,
    ScanSource,
    SourceCapabilities,
    UnsupportedRequest,
)


class ContractValueTests(unittest.TestCase):
    def test_backend_spellings_are_normalized(self) -> None:
        source = SourceCapabilities(
            source="ADF Duplex",
            modes=("Lineart", "Gray", "Color"),
            resolutions=(600, 200, 300),
            bit_depths=(8, 1),
        )

        self.assertEqual(source.source, ScanSource.FEEDER_DUPLEX)
        self.assertEqual(
            source.modes,
            (ScanMode.COLOR, ScanMode.GRAYSCALE, ScanMode.LINEART),
        )
        self.assertEqual(source.resolutions, (200, 300, 600))
        self.assertEqual(source.bit_depths, (1, 8))

        request = ScanRequest(
            scanner_id="scanner-1",
            source="bed",
            mode="grey",
            page_size="A4",
        )
        self.assertEqual(request.source, ScanSource.FLATBED)
        self.assertEqual(request.mode, ScanMode.GRAYSCALE)
        self.assertEqual(request.page_size, PageSize.A4)
        self.assertEqual(ScanMode.parse("color-rgb"), ScanMode.COLOR)
        self.assertEqual(
            ScanSource.parse("positive-transparency"),
            ScanSource.POSITIVE_TRANSPARENCY,
        )

    def test_auto_is_not_a_physical_capability(self) -> None:
        with self.assertRaisesRegex(ValueError, "not a scanner source"):
            SourceCapabilities(
                source="auto",
                modes=("Color",),
                resolutions=(300,),
            )

    def test_invalid_or_duplicate_capabilities_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicates"):
            SourceCapabilities(
                source="Flatbed",
                modes=("Gray", "grayscale"),
                resolutions=(300,),
            )
        with self.assertRaisesRegex(ValueError, "positive integer"):
            SourceCapabilities(
                source="Flatbed",
                modes=("Color",),
                resolutions=(0,),
            )

    def test_values_are_immutable(self) -> None:
        scanner = Scanner(
            id="physical-1",
            name="Office scanner",
            backend="imagecapture",
            endpoint="persistent-id",
        )
        with self.assertRaises(FrozenInstanceError):
            scanner.name = "changed"  # type: ignore[misc]


class RepresentationTests(unittest.TestCase):
    def test_existing_hp_scanner_has_no_special_case_in_shared_model(self) -> None:
        scanner = Scanner(
            id="serial:hp-example",
            name="HP LaserJet Pro MFP",
            backend="hpaio",
            endpoint="hpaio:/net/example?ip=192.0.2.10",
            manufacturer="HP",
            model="LaserJet Pro MFP",
            transport="network",
            serial_number="hp-example",
        )
        capabilities = Capabilities(
            scanner_id=scanner.id,
            sources=(
                SourceCapabilities(
                    source="ADF",
                    modes=("Color", "Gray", "Lineart"),
                    resolutions=(75, 150, 300, 600, 1200),
                    supports_lossless=True,
                ),
                SourceCapabilities(
                    source="Flatbed",
                    modes=("Color", "Gray", "Lineart"),
                    resolutions=(75, 150, 300, 600, 1200),
                    supports_lossless=True,
                ),
            ),
        )

        request = ScanRequest(
            scanner_id=scanner.id,
            source="auto",
            mode="Color",
            resolution=600,
            lossless=True,
        )
        self.assertEqual(
            tuple(source.source for source in capabilities.compatible_sources(request)),
            (ScanSource.FLATBED, ScanSource.FEEDER),
        )

        # The current HP path's measured pages and short-batch warning also fit
        # without placing HPLIP fields in shared result handling.
        result = ScanResult(
            scanner_id=scanner.id,
            backend=scanner.backend,
            source="ADF",
            pages=(
                ScanPage(
                    1,
                    "/tmp/scanbox-hp/page-1.png",
                    "image/png",
                    width_px=2550,
                    height_px=3300,
                    resolution=300,
                ),
                ScanPage(
                    2,
                    "/tmp/scanbox-hp/page-2.png",
                    "image/png",
                    width_px=2550,
                    height_px=4200,
                    resolution=300,
                ),
            ),
            truncated=True,
            warnings=("feeder stopped early",),
        )
        self.assertEqual(result.source, ScanSource.FEEDER)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.pages), 2)

    def test_native_scanner_can_omit_vendor_specific_fields(self) -> None:
        scanner = Scanner(
            id="imagecapture:persistent-id",
            name="Network Scanner",
            backend="imagecapture",
            endpoint="persistent-id",
            transport="Bonjour",
        )
        capabilities = Capabilities(
            scanner_id=scanner.id,
            sources=(
                SourceCapabilities(
                    source="flatbed",
                    modes=("color-rgb",),
                    resolutions=(300, 600),
                    modes_complete=False,
                    bit_depths=(8,),
                    native_resolution=(600, 600),
                ),
            ),
        )

        self.assertIsNone(scanner.manufacturer)
        self.assertEqual(capabilities.sources[0].native_resolution, (600, 600))
        # ImageCaptureCore reports only the current mode. An absent mode is
        # therefore unknown, not unsupported, until native preparation probes it.
        request = ScanRequest(scanner.id, mode="Gray", resolution=300)
        self.assertEqual(capabilities.compatible_sources(request), capabilities.sources)


class RequestValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capabilities = Capabilities(
            scanner_id="scanner-1",
            sources=(
                SourceCapabilities(
                    source="flatbed",
                    modes=("Color", "Gray"),
                    resolutions=(200, 300, 600),
                    supports_lossless=True,
                ),
                SourceCapabilities(
                    source="ADF",
                    modes=("Color", "Gray"),
                    resolutions=(200, 300),
                ),
            ),
        )

    def test_auto_returns_only_compatible_sources(self) -> None:
        request = ScanRequest(
            scanner_id="scanner-1",
            source="auto",
            mode="gray",
            resolution=600,
            lossless=True,
        )
        matches = self.capabilities.compatible_sources(request)
        self.assertEqual(tuple(item.source for item in matches), (ScanSource.FLATBED,))

    def test_unknown_source_and_unsupported_mode_are_rejected(self) -> None:
        with self.assertRaisesRegex(UnsupportedRequest, "not available"):
            self.capabilities.compatible_sources(ScanRequest(
                scanner_id="scanner-1",
                source="feeder-duplex",
            ))
        with self.assertRaisesRegex(UnsupportedRequest, "no selected source"):
            self.capabilities.compatible_sources(ScanRequest(
                scanner_id="scanner-1",
                mode="lineart",
            ))

    def test_request_for_another_scanner_is_rejected(self) -> None:
        with self.assertRaisesRegex(UnsupportedRequest, "different scanner"):
            self.capabilities.compatible_sources(ScanRequest(scanner_id="scanner-2"))


class ResultTests(unittest.TestCase):
    def test_completed_result_reports_concrete_source_and_host_pages(self) -> None:
        page = ScanPage(
            index=1,
            path="/tmp/scanbox-job/page-1.png",
            media_type="image/png",
            width_px=1700,
            height_px=2339,
            resolution=200,
        )
        result = ScanResult(
            scanner_id="scanner-1",
            backend="sane-airscan-wsd",
            source="ADF",
            pages=(page,),
            warnings=("feeder stopped early",),
            truncated=True,
        )

        self.assertEqual(result.source, ScanSource.FEEDER)
        self.assertEqual(result.pages[0].path, page.path)

    def test_result_rejects_auto_nonsequential_pages_and_relative_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute host path"):
            ScanPage(index=1, path="page.png", media_type="image/png")

        page = ScanPage(index=2, path="/tmp/page.png", media_type="image/png")
        with self.assertRaisesRegex(ValueError, "consecutive"):
            ScanResult(
                scanner_id="scanner-1",
                backend="imagecapture",
                source="flatbed",
                pages=(page,),
            )

        page = ScanPage(index=1, path="/tmp/page.png", media_type="image/png")
        with self.assertRaisesRegex(ValueError, "source actually used"):
            ScanResult(
                scanner_id="scanner-1",
                backend="imagecapture",
                source="auto",
                pages=(page,),
            )


class ErrorTests(unittest.TestCase):
    def test_backend_errors_have_stable_machine_readable_fields(self) -> None:
        error = BackendError(
            "busy", "scanner is in use", backend="imagecapture", retryable=True
        )
        self.assertEqual(error.code, BackendErrorCode.BUSY)
        self.assertEqual(error.backend, "imagecapture")
        self.assertTrue(error.retryable)
        self.assertEqual(str(error), "scanner is in use")


class _FakeJob(ScanJob):
    def __init__(self, result: ScanResult) -> None:
        self._result: Optional[ScanResult] = None
        self._next_result = result
        self.cancel_count = 0
        self._cancelled = False

    def scan(self) -> ScanResult:
        self._result = self._next_result
        return self._result

    def cancel(self) -> None:
        if not self._cancelled:
            self.cancel_count += 1
            self._cancelled = True

    @property
    def result(self) -> Optional[ScanResult]:
        return self._result


class _FakeBackend(Backend):
    def __init__(self, scanner: Scanner, capabilities: Capabilities,
                 job: ScanJob) -> None:
        self._scanner = scanner
        self._capabilities = capabilities
        self._job = job

    @property
    def name(self) -> str:
        return self._scanner.backend

    def discover(self) -> Sequence[Scanner]:
        return (self._scanner,)

    def inspect(self, scanner: Scanner) -> Capabilities:
        return self._capabilities

    def prepare(self, scanner: Scanner, request: ScanRequest) -> ScanJob:
        self._capabilities.compatible_sources(request)
        return self._job


class LifecycleContractTests(unittest.TestCase):
    def test_backend_and_job_boundaries_are_implementable(self) -> None:
        scanner = Scanner("scanner-1", "Scanner", "fake", "opaque-endpoint")
        capabilities = Capabilities(
            scanner.id,
            (SourceCapabilities("flatbed", ("Color",), (300,)),),
        )
        result = ScanResult(
            scanner.id,
            scanner.backend,
            "flatbed",
            (ScanPage(1, "/tmp/page.png", "image/png"),),
        )
        job = _FakeJob(result)
        backend = _FakeBackend(scanner, capabilities, job)

        request = ScanRequest(scanner.id)
        prepared = backend.prepare(scanner, request)
        self.assertIsNone(prepared.result)
        self.assertEqual(prepared.scan(), result)
        self.assertEqual(prepared.result, result)
        prepared.cancel()
        prepared.cancel()
        self.assertEqual(job.cancel_count, 1)

    def test_abstract_contracts_cannot_be_instantiated(self) -> None:
        with self.assertRaises(TypeError):
            Backend()  # type: ignore[abstract]
        with self.assertRaises(TypeError):
            ScanJob()  # type: ignore[abstract]


if __name__ == "__main__":
    unittest.main()
