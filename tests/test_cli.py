import contextlib
import io
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

    def test_protocol_override_is_passed_to_scan_options(self):
        with mock.patch.object(cli.scan, "run", return_value=[]) as run:
            self.assertEqual(cli.main(["scan", "--protocol", "wsd"]), 0)
        self.assertEqual(run.call_args.args[0].protocol, "wsd")

    def test_printer_rejects_a_nonlegacy_protocol(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main([
                "scan", "--printer", "old-hp.local", "--protocol", "wsd"
            ]), 1)


class SetupIdentityTests(unittest.TestCase):
    def test_discovered_stable_identity_and_hostname_are_saved(self):
        found = cli.discover.Instance(
            "Xerox instance", "xerox.local", {
                "ty": "Xerox WorkCentre 6605DN",
                "UUID": "5DE90400-1DD2-11B2-84BC-9C934E010299",
            },
        )
        with contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(cli.config, "exists", return_value=False), \
                mock.patch.object(cli.discover, "instances", return_value=[found.name]), \
                mock.patch.object(cli.discover, "resolve_instance", return_value=found), \
                mock.patch.object(cli.discover, "resolve_ipv4", return_value="192.0.2.52"), \
                mock.patch.object(cli.ui, "tty_readable", return_value=True), \
                mock.patch.object(cli.ui, "ask", return_value=""), \
                mock.patch.object(cli.config, "save") as save:
            self.assertEqual(cli.main(["setup"]), 0)

        configured = save.call_args.args[0]
        self.assertEqual(
            configured.id, "uuid:5de90400-1dd2-11b2-84bc-9c934e010299"
        )
        self.assertEqual(configured.name, "Xerox WorkCentre 6605DN")
        self.assertEqual(configured.host, "xerox.local")
        self.assertEqual(configured.protocol, "auto")


if __name__ == "__main__":
    unittest.main()
