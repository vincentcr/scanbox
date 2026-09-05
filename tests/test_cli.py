import unittest
from unittest import mock

from scanbox import cli, ui


class ScanTargetArgumentTests(unittest.TestCase):
    def test_scanner_auto_is_passed_to_scan_options(self):
        with mock.patch.object(cli.scan, "run", return_value=[]) as run:
            self.assertEqual(cli.main(["scan", "--scanner", "auto"]), 0)
        self.assertEqual(run.call_args.args[0].scanner, "auto")
        self.assertIsNone(run.call_args.args[0].printer)

    def test_legacy_printer_override_remains_available(self):
        with mock.patch.object(cli.scan, "run", return_value=[]) as run:
            self.assertEqual(
                cli.main(["scan", "--printer", "old-hp.local"]), 0
            )
        self.assertEqual(run.call_args.args[0].printer, "old-hp.local")
        self.assertIsNone(run.call_args.args[0].scanner)

    def test_scanner_and_printer_are_mutually_exclusive(self):
        with self.assertRaises(ui.ScanboxError):
            cli.build_parser().parse_args([
                "scan", "--scanner", "auto", "--printer", "old-hp.local"
            ])

    def test_scanners_command_is_registered(self):
        self.assertEqual(cli.build_parser().parse_args(["scanners"]).cmd, "scanners")


if __name__ == "__main__":
    unittest.main()
