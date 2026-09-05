import os
import shutil
import tempfile
import unittest

from scanbox import proc
from scanbox.contracts import ScanMode, ScanPage, ScanResult, ScanSource
from scanbox.output import (
    OutputError,
    OutputOptions,
    assemble,
    choose_format,
    output_paths,
)


class FakeRunner:
    def __init__(self, fail_at=None):
        self.commands = []
        self.fail_at = fail_at

    def __call__(self, command, timeout=None):
        self.commands.append((list(command), timeout))
        if self.fail_at == len(self.commands):
            return proc.Result(1, "", "simulated conversion failure")
        if "--output" in command:
            target = command[command.index("--output") + 1]
        elif "--out" in command:
            target = command[command.index("--out") + 1]
        else:
            target = command[command.index("-out") + 1]
        with open(target, "wb") as output:
            output.write(("made by " + os.path.basename(command[0])).encode("ascii"))
        return proc.Result(0, "", "")


class OutputPolicyTests(unittest.TestCase):
    def options(self, **values):
        defaults = {"out_dir": "/tmp/out", "name": "scan"}
        defaults.update(values)
        return OutputOptions(**defaults)

    def test_pdf_is_the_default(self):
        self.assertEqual(choose_format(ScanSource.FLATBED, self.options()), "pdf")

    def test_joined_feeder_image_is_tiff_even_for_lineart(self):
        options = self.options(image=True, mode=ScanMode.LINEART)
        self.assertEqual(choose_format(ScanSource.FEEDER, options), "tiff")

    def test_lossless_or_lineart_image_is_png_when_not_joined_feeder(self):
        self.assertEqual(choose_format(
            ScanSource.FLATBED, self.options(image=True, lossless=True)), "png")
        self.assertEqual(choose_format(
            ScanSource.FEEDER, self.options(image=True, split=True,
                                            mode=ScanMode.LINEART)), "png")

    def test_other_images_are_jpeg(self):
        self.assertEqual(choose_format(
            ScanSource.FLATBED, self.options(image=True)), "jpeg")
        self.assertEqual(choose_format(
            ScanSource.FEEDER, self.options(image=True, split=True)), "jpeg")

    def test_exact_format_overrides_smart_choice(self):
        options = self.options(image=True, lossless=True, fmt="jpeg")
        self.assertEqual(choose_format(ScanSource.FLATBED, options), "jpeg")


class OutputAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="scanbox-output-test-")
        self.out = os.path.join(self.root, "out")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def result(self, count=2, source=ScanSource.FEEDER):
        acquired = os.path.join(self.root, "acquired")
        os.makedirs(acquired, exist_ok=True)
        pages = []
        for index in range(1, count + 1):
            path = os.path.join(acquired, "page-{}.png".format(index))
            with open(path, "wb") as output:
                output.write(b"not-a-real-png")
            pages.append(ScanPage(index, path, "image/png"))
        return ScanResult("scanner", "fake", source, tuple(pages))

    def options(self, **values):
        defaults = {"out_dir": self.out, "name": "receipt"}
        defaults.update(values)
        return OutputOptions(**defaults)

    def test_names_are_identical_for_equivalent_backend_results(self):
        first = self.result()
        second_pages = tuple(
            ScanPage(page.index, page.path.replace("acquired", "other"), "image/png")
            for page in first.pages
        )
        second = ScanResult("other", "other-backend", first.source, second_pages)
        options = self.options(fmt="pdf")
        self.assertEqual(output_paths(first, options), output_paths(second, options))

    def test_joined_pdf_has_one_name_and_reports_page_progress(self):
        result = self.result()
        events = []
        outputs = assemble(result, self.options(fmt="pdf"),
                           on_event=lambda kind, value: events.append((kind, value)),
                           runner=FakeRunner())
        self.assertEqual(outputs, (os.path.join(self.out, "receipt.pdf"),))
        self.assertTrue(os.path.isfile(outputs[0]))
        self.assertEqual([value for kind, value in events if kind == "progress"], [
            "assembling page 1 of 2", "assembling page 2 of 2",
        ])
        self.assertFalse(os.path.exists(os.path.dirname(result.pages[0].path)))

    def test_split_pdf_uses_page_suffixes(self):
        result = self.result()
        outputs = assemble(result, self.options(fmt="pdf", split=True),
                           runner=FakeRunner())
        self.assertEqual(tuple(os.path.basename(path) for path in outputs),
                         ("receipt-p001.pdf", "receipt-p002.pdf"))

    def test_joined_tiff_has_one_file(self):
        outputs = assemble(self.result(), self.options(fmt="tiff"),
                           runner=FakeRunner())
        self.assertEqual(tuple(os.path.basename(path) for path in outputs),
                         ("receipt.tiff",))

    def test_png_and_jpeg_are_always_per_page(self):
        png = assemble(self.result(), self.options(fmt="png"), runner=FakeRunner())
        jpeg = assemble(self.result(), self.options(fmt="jpeg"), runner=FakeRunner())
        self.assertEqual(tuple(os.path.basename(path) for path in png),
                         ("receipt-p001.png", "receipt-p002.png"))
        self.assertEqual(tuple(os.path.basename(path) for path in jpeg),
                         ("receipt-p001.jpg", "receipt-p002.jpg"))

    def test_one_page_never_gets_a_page_suffix(self):
        result = self.result(count=1, source=ScanSource.FLATBED)
        outputs = assemble(result, self.options(fmt="png", split=True),
                           runner=FakeRunner())
        self.assertEqual(os.path.basename(outputs[0]), "receipt.png")

    def test_conversion_failure_leaves_no_final_output_and_cleans_pages(self):
        result = self.result()
        with self.assertRaisesRegex(OutputError, "simulated conversion failure"):
            assemble(result, self.options(fmt="pdf"), runner=FakeRunner(fail_at=2))
        self.assertFalse(os.path.exists(self.out) and os.listdir(self.out))
        self.assertFalse(os.path.exists(os.path.dirname(result.pages[0].path)))

    def test_missing_page_still_cleans_other_staged_pages(self):
        result = self.result()
        os.unlink(result.pages[1].path)
        with self.assertRaisesRegex(OutputError, "acquired page is missing"):
            assemble(result, self.options(fmt="pdf"), runner=FakeRunner())
        self.assertFalse(os.path.exists(os.path.dirname(result.pages[0].path)))

    def test_existing_output_is_replaced_only_after_success(self):
        os.makedirs(self.out)
        old = os.path.join(self.out, "receipt.pdf")
        with open(old, "wb") as output:
            output.write(b"old")
        result = self.result()
        with self.assertRaises(OutputError):
            assemble(result, self.options(fmt="pdf"), runner=FakeRunner(fail_at=2))
        with open(old, "rb") as output:
            self.assertEqual(output.read(), b"old")


if __name__ == "__main__":
    unittest.main()
