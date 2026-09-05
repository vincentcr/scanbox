import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from scanbox import config, scan, selection
from scanbox.contracts import (
    Backend,
    ScanPage,
    ScanResult,
    ScanSource,
    Scanner,
)


class FakeJob:
    diagnostics = ("legacy detail",)
    measurements = ("p0001: letter (measured 11.0in)",)

    def __init__(self, root):
        self.root = root

    def scan(self):
        pages = []
        for index in (1, 2):
            path = os.path.join(self.root, "page-{:04d}.png".format(index))
            with open(path, "wb") as stream:
                stream.write(b"png raster")
            pages.append(ScanPage(index, path, "image/png", resolution=600))
        return ScanResult(
            "hpaio:/net/test", "hplip-legacy", ScanSource.FEEDER,
            tuple(pages), truncated=True,
        )


class FakeBackend:
    instances = []

    def __init__(self, address, on_event=None):
        self.address = address
        self.on_event = on_event or (lambda _kind, _value: None)
        self.released = []
        self.request = None
        self.scanner = Scanner(
            "hpaio:/net/test", "HP test", "hplip-legacy", "hpaio:/net/test"
        )
        self.__class__.instances.append(self)

    def discover(self):
        return (self.scanner,)

    def prepare(self, scanner, request):
        self.request = request
        return FakeJob(FakeBackend.page_root)

    def release(self, keep_alive):
        self.released.append(keep_alive)


class DynamicBackend(Backend):
    def __init__(self):
        self.scanner = Scanner(
            "wsd:stable-xerox", "Xerox test", "dynamic-test",
            "http://192.0.2.25/ws/",
        )
        self.on_event = lambda _kind, _value: None
        self.request = None
        self.released = []

    @property
    def name(self):
        return "dynamic-test"

    def discover(self):
        raise AssertionError("the supplied catalog already performed discovery")

    def inspect(self, scanner):
        raise AssertionError("fake backend does not need inspection")

    def prepare(self, scanner, request):
        self.request = request
        return DynamicJob(DynamicBackend.page_root, scanner)

    def release(self, keep_alive):
        self.released.append(keep_alive)


class DynamicJob:
    def __init__(self, root, scanner):
        self.root = root
        self.scanner = scanner

    @property
    def result(self):
        return None

    def cancel(self):
        pass

    def scan(self):
        path = os.path.join(self.root, "page-0001.png")
        with open(path, "wb") as stream:
            stream.write(b"png raster")
        return ScanResult(
            self.scanner.id, self.scanner.backend, ScanSource.FLATBED,
            (ScanPage(1, path, "image/png", resolution=300),),
        )


class FakeCatalog:
    def __init__(self, backend=None):
        self.backend = backend

    def discover(self):
        if self.backend is None:
            return selection.Inventory(())
        return selection.Inventory((
            selection.Candidate(self.backend.scanner, self.backend),
        ))


class LegacyScanOutputIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="scanbox-scan-output-")
        self.pages = tempfile.mkdtemp(prefix="scanbox-scan-pages-")
        FakeBackend.instances = []
        FakeBackend.page_root = self.pages
        DynamicBackend.page_root = self.pages

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.pages, ignore_errors=True)

    def test_legacy_scan_uses_backend_contract_then_normalized_output(self):
        assembled = []

        def assemble(result, options, on_event=None):
            assembled.append((result, options, on_event))
            return (os.path.join(options.out_dir, options.name + ".tiff"),)

        options = scan.Options(
            source="ADF", mode="Lineart", dpi=600, image=True,
            out_dir=self.root, name="documents", keep_alive=17,
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
                mock.patch.object(scan, "resolve_printer", return_value="192.0.2.20"), \
                mock.patch.object(scan, "HPLIPBackend", FakeBackend), \
                mock.patch.object(scan.output, "assemble", side_effect=assemble):
            outputs = scan.run(options)

        self.assertEqual(outputs, [os.path.join(self.root, "documents.tiff")])
        backend = FakeBackend.instances[0]
        self.assertEqual(backend.address, "192.0.2.20")
        self.assertEqual(backend.request.source, ScanSource.FEEDER)
        self.assertEqual(backend.request.mode.value, "lineart")
        self.assertEqual(backend.request.resolution, 600)
        self.assertEqual(backend.released, [17])
        result, output_options, event = assembled[0]
        self.assertEqual(result.source, ScanSource.FEEDER)
        self.assertEqual(len(result.pages), 2)
        self.assertTrue(result.truncated)
        self.assertEqual(result.backend, "hplip-legacy")
        self.assertIsNone(output_options.fmt)
        self.assertTrue(output_options.image)
        self.assertEqual(output_options.mode.value, "lineart")
        self.assertIsNotNone(event)
        self.assertIn("legacy detail", stderr.getvalue())
        self.assertIn("p0001: letter (measured 11.0in)", stderr.getvalue())
        self.assertIn("feeder, 2 page(s)", stderr.getvalue())
        self.assertIn("WARNING: the feeder stopped early", stderr.getvalue())

    def test_dynamic_selection_bypasses_and_preserves_config(self):
        config_path = os.path.join(self.root, "config")
        original = b"# keep comments byte-for-byte\nPRINTER_HOST=home-scanner.local\n"
        with open(config_path, "wb") as stream:
            stream.write(original)
        backend = DynamicBackend()

        def assemble(result, options, on_event=None):
            return (os.path.join(options.out_dir, options.name + ".pdf"),)

        options = scan.Options(
            scanner="Xerox test", out_dir=self.root, name="away-from-home"
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
                mock.patch.object(config, "CONFIG_FILE", config_path), \
                mock.patch.object(scan, "resolve_printer") as legacy_resolver, \
                mock.patch.object(scan.ui, "tty_readable", return_value=False), \
                mock.patch.object(scan.output, "assemble", side_effect=assemble):
            outputs = scan.run(options, catalog=FakeCatalog(backend))

        legacy_resolver.assert_not_called()
        with open(config_path, "rb") as stream:
            self.assertEqual(stream.read(), original)
        self.assertEqual(outputs, [os.path.join(self.root, "away-from-home.pdf")])
        self.assertEqual(backend.request.scanner_id, "wsd:stable-xerox")
        self.assertEqual(backend.released, [60])
        self.assertIn("using Xerox test via dynamic-test", stderr.getvalue())

    def test_dynamic_selection_reports_no_current_network_scanner_cleanly(self):
        options = scan.Options(scanner="auto", out_dir=self.root)
        with contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(scan.ui, "tty_readable", return_value=False):
            with self.assertRaisesRegex(
                scan.ui.ScanboxError, "no usable scanners found on this network"
            ):
                scan.run(options, catalog=FakeCatalog())


if __name__ == "__main__":
    unittest.main()
