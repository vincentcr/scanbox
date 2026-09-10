import contextlib
import io
import unittest
from unittest import mock

from scanbox import cli, config, selection, ui
from scanbox.contracts import Backend, Scanner


class DiscoveryBackend(Backend):
    def __init__(self, name="wsd"):
        self._name = name
        self.released = []

    @property
    def name(self):
        return self._name

    def discover(self):
        return ()

    def inspect(self, scanner):
        raise AssertionError("validation is mocked")

    def prepare(self, scanner, request):
        raise AssertionError("scanners must not prepare acquisition")

    def release(self, minutes):
        self.released.append(minutes)


class FakeCatalog:
    def __init__(self, *candidates):
        self.candidates = candidates

    def discover(self):
        return selection.Inventory(self.candidates)


class ScanTargetArgumentTests(unittest.TestCase):
    def test_scanner_auto_is_passed_to_scan_options(self):
        with mock.patch.object(cli.scan, "run", return_value=[]) as run:
            self.assertEqual(cli.main(["scan", "--scanner", "auto"]), 0)
        self.assertEqual(run.call_args.args[0].scanner, "auto")

    def test_scanners_command_is_registered(self):
        self.assertEqual(cli.build_parser().parse_args(["scanners"]).cmd, "scanners")

    def test_backend_override_is_passed_to_scan_options(self):
        with mock.patch.object(cli.scan, "run", return_value=[]) as run:
            self.assertEqual(cli.main(["scan", "--backend", "wsd"]), 0)
        self.assertEqual(run.call_args.args[0].backend, "wsd")

    def test_removed_protocol_option_is_rejected(self):
        with self.assertRaises(ui.ScanboxError):
            cli.build_parser().parse_args(["scan", "--protocol", "legacy"])


class ScannerRegistryCommandTests(unittest.TestCase):
    def test_discovered_scanner_is_validated_and_saved(self):
        backend = DiscoveryBackend()
        candidate = selection.Candidate(
            Scanner(
                "wsd:urn:uuid:5de90400-1dd2-11b2-84bc-9c934e010299",
                "Xerox WorkCentre", "wsd", "http://192.0.2.52/ws/",
                address="192.0.2.52",
            ),
            backend,
        )
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(cli.selection, "current_network_catalog",
                                  return_value=FakeCatalog(candidate)), \
                mock.patch.object(cli.selection, "validate", return_value=candidate) as validate, \
                mock.patch.object(cli.config, "load_registry",
                                  return_value=config.ScannerRegistry()), \
                mock.patch.object(cli.config, "remember") as remember:
            self.assertEqual(cli.main(["scanners", "--save"]), 0)

        validate.assert_called_once()
        saved = remember.call_args.args[0]
        self.assertEqual(saved.name, "Xerox WorkCentre")
        self.assertEqual(saved.address, "192.0.2.52")
        self.assertEqual(saved.backend, "wsd")
        self.assertEqual(backend.released, [60])

    def test_manual_host_requires_and_saves_a_concrete_backend(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(cli.config, "remember") as remember:
            self.assertEqual(cli.main([
                "scanners", "--save", "Xerox", "--host", "xerox.local",
                "--backend", "wsd", "--preferred",
            ]), 0)

        saved = remember.call_args.args[0]
        self.assertEqual(saved.host, "xerox.local")
        self.assertEqual(saved.backend, "wsd")
        self.assertTrue(remember.call_args.kwargs["preferred"])

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main([
                "scanners", "--save", "--host", "xerox.local"
            ]), 1)

    def test_saved_display_is_offline_and_setup_is_removed(self):
        registry = config.ScannerRegistry((config.ConfiguredScanner(
            name="HP home", host="hp.local", backend="hplip"
        ),))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
                mock.patch.object(cli.config, "load_registry", return_value=registry), \
                mock.patch.object(cli.selection, "current_network_catalog") as catalog:
            self.assertEqual(cli.main(["scanners", "--saved"]), 0)
        catalog.assert_not_called()
        self.assertIn("HP home", output.getvalue())
        self.assertIn("hplip", output.getvalue())

        with self.assertRaises(ui.ScanboxError):
            cli.build_parser().parse_args(["setup"])

    def test_save_prompts_for_ambiguous_discovery_on_a_terminal(self):
        backend = DiscoveryBackend()
        first = selection.Candidate(Scanner(
            "serial:first", "First scanner", "wsd", "http://192.0.2.60/ws/"
        ), backend)
        second = selection.Candidate(Scanner(
            "serial:second", "Second scanner", "wsd", "http://192.0.2.61/ws/"
        ), backend)
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(cli.selection, "current_network_catalog",
                                  return_value=FakeCatalog(first, second)), \
                mock.patch.object(cli.ui, "tty_readable", return_value=True), \
                mock.patch.object(cli.ui, "ask", return_value="2") as ask, \
                mock.patch.object(cli.selection, "validate", return_value=second), \
                mock.patch.object(cli.config, "remember") as remember:
            self.assertEqual(cli.main(["scanners", "--save"]), 0)

        ask.assert_called_once_with("Which one? [1-2] ")
        self.assertEqual(remember.call_args.args[0].name, "Second scanner")

    def test_noninteractive_save_prints_commands_that_disambiguate(self):
        hplip = DiscoveryBackend("hplip")
        wsd = DiscoveryBackend()
        first = selection.Candidate(Scanner(
            "bonjour:hp", "HP LaserJet", "hplip", "hp.local", host="hp.local"
        ), hplip)
        second = selection.Candidate(Scanner(
            "wsd:urn:uuid:5de90400-1dd2-11b2-84bc-9c934e010299",
            "WSD scanner at 192.0.2.62", "wsd", "http://192.0.2.62/ws/",
            address="192.0.2.62",
        ), wsd)
        error = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(error), \
                mock.patch.object(cli.selection, "current_network_catalog",
                                  return_value=FakeCatalog(first, second)), \
                mock.patch.object(cli.ui, "tty_readable", return_value=False):
            self.assertEqual(cli.main(["scanners", "--save"]), 1)

        message = error.getvalue()
        self.assertIn(
            "scanbox scanners --save 'HP LaserJet' --backend hplip", message
        )
        self.assertIn(
            "scanbox scanners --save uuid:5de90400-1dd2-11b2-84bc-9c934e010299 "
            "--backend wsd", message
        )


if __name__ == "__main__":
    unittest.main()
