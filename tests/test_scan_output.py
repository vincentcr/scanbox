import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from scanbox import scan
from scanbox.contracts import ScanPage, ScanResult, ScanSource, Scanner


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


class LegacyScanOutputIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="scanbox-scan-output-")
        self.pages = tempfile.mkdtemp(prefix="scanbox-scan-pages-")
        FakeBackend.instances = []
        FakeBackend.page_root = self.pages

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


if __name__ == "__main__":
    unittest.main()
