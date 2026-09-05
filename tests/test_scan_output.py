import contextlib
import os
import shutil
import tempfile
import unittest
from unittest import mock

from scanbox import proc, scan
from scanbox.contracts import ScanSource


class LegacyScanOutputIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="scanbox-scan-output-")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_legacy_scan_copies_rasters_into_normalized_output_assembler(self):
        streaming_commands = []
        assembled = []

        def run_streaming(command, on_line, **_kwargs):
            streaming_commands.append(command)
            for line in (
                "TRUNCATED 2", "SOURCE ADF", "PAGES 2",
                "RASTER /tmp/scanbox-out/p0001.png",
                "RASTER /tmp/scanbox-out/p0002.png",
            ):
                on_line(line)
            return proc.Result(0, "", "")

        def run(command, timeout=None):
            self.assertEqual(command[0:2], ["limactl", "copy"])
            with open(command[-1], "wb") as page:
                page.write(b"png raster")
            return proc.Result(0, "", "")

        def assemble(result, options, on_event=None):
            assembled.append((result, options, on_event))
            return (os.path.join(options.out_dir, options.name + ".tiff"),)

        options = scan.Options(
            source="ADF", mode="Lineart", dpi=600, image=True,
            out_dir=self.root, name="documents",
        )
        with mock.patch.object(scan, "resolve_printer", return_value="192.0.2.20"), \
                mock.patch.object(scan.vm, "ensure"), \
                mock.patch.object(scan.lock, "Lock", side_effect=contextlib.nullcontext), \
                mock.patch.object(scan, "clear_stale_scan"), \
                mock.patch.object(scan, "device_uri", return_value="hpaio:/net/test"), \
                mock.patch.object(scan.proc, "run_streaming", side_effect=run_streaming), \
                mock.patch.object(scan.proc, "run", side_effect=run), \
                mock.patch.object(scan.output, "assemble", side_effect=assemble), \
                mock.patch.object(scan.vm, "idle_timer_arm"):
            outputs = scan.run(options)

        self.assertEqual(outputs, [os.path.join(self.root, "documents.tiff")])
        self.assertEqual(streaming_commands[0][-1], "1")
        result, output_options, event = assembled[0]
        self.assertEqual(result.source, ScanSource.FEEDER)
        self.assertEqual(len(result.pages), 2)
        self.assertTrue(result.truncated)
        self.assertEqual(result.backend, "hplip-legacy")
        self.assertIsNone(output_options.fmt)
        self.assertTrue(output_options.image)
        self.assertEqual(output_options.mode.value, "lineart")
        self.assertIsNotNone(event)


if __name__ == "__main__":
    unittest.main()
