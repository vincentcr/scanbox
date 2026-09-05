import os
import shutil
import sys
import tempfile
import unittest

from scanbox import proc
from scanbox.contracts import ScanPage, ScanResult, ScanSource
from scanbox.output import PDF_JOIN, SIPS, TIFFUTIL, OutputOptions, assemble


@unittest.skipUnless(
    sys.platform == "darwin"
    and all(os.path.isfile(path) for path in (SIPS, TIFFUTIL, PDF_JOIN)),
    "requires the macOS system image and PDF utilities",
)
class MacOSOutputIntegrationTests(unittest.TestCase):
    """Exercise the dependency-light production commands, not fake converters."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="scanbox-output-macos-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def result(self):
        acquired = os.path.join(self.root, "acquired")
        os.makedirs(acquired)
        pages = []
        for index, color in enumerate(((255, 0, 0), (0, 0, 255)), 1):
            path = os.path.join(acquired, "page-{}.ppm".format(index))
            with open(path, "w") as output:
                output.write("P3\n8 8\n255\n")
                output.write(("{} {} {}\n".format(*color)) * 64)
            pages.append(ScanPage(index, path, "image/x-portable-pixmap"))
        return ScanResult("scanner", "fake", ScanSource.FEEDER, tuple(pages))

    def test_system_tools_build_joined_pdf_and_tiff(self):
        for fmt in ("pdf", "tiff"):
            with self.subTest(fmt=fmt):
                out_dir = os.path.join(self.root, fmt)
                outputs = assemble(
                    self.result(), OutputOptions(out_dir, "two-pages", fmt=fmt)
                )
                self.assertEqual(len(outputs), 1)
                self.assertGreater(os.path.getsize(outputs[0]), 100)
                if fmt == "pdf":
                    with open(outputs[0], "rb") as stream:
                        pdf = stream.read()
                    self.assertIn(b"/FlateDecode", pdf)
                    self.assertNotIn(b"/DCTDecode", pdf)
                else:
                    info = proc.run([TIFFUTIL, "-info", outputs[0]])
                    self.assertTrue(info.ok)
                    self.assertGreaterEqual(info.out.count("Directory"), 2)


if __name__ == "__main__":
    unittest.main()
