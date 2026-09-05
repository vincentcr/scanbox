import os
import shutil
import tempfile
import unittest
from unittest import mock

from scanbox import config, scan


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="scanbox-config-")
        self.path = os.path.join(self.root, "config")
        self.patch = mock.patch.object(config, "CONFIG_FILE", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, contents):
        with open(self.path, "w") as stream:
            stream.write(contents)

    def test_stable_identity_and_fallback_locator_round_trip(self):
        expected = config.ConfiguredScanner(
            id="uuid:5de90400-1dd2-11b2-84bc-9c934e010299",
            name="Xerox WorkCentre 6605DN",
            host="xerox.local",
            address="192.0.2.52",
        )

        config.save(expected)

        self.assertEqual(config.load_scanner(), expected)
        values = config.load()
        self.assertEqual(values["SCANNER_ID"], expected.id)
        self.assertEqual(values["SCANNER_PROTOCOL"], "auto")
        self.assertNotIn("PRINTER_HOST", values)

    def test_legacy_host_and_ip_are_accepted_and_migrated_atomically(self):
        self.write(
            "# old install\nPRINTER_HOST=home-scanner.local\n"
            "PRINTER_IP=192.0.2.20\n"
        )

        scanner = config.load_scanner(migrate=True)

        self.assertEqual(scanner.host, "home-scanner.local")
        self.assertEqual(scanner.address, "192.0.2.20")
        self.assertEqual(scanner.protocol, "auto")
        values = config.load()
        self.assertEqual(values["SCANNER_HOST"], "home-scanner.local")
        self.assertEqual(values["SCANNER_ADDRESS"], "192.0.2.20")
        self.assertNotIn("PRINTER_HOST", values)
        self.assertEqual(
            [name for name in os.listdir(self.root) if name.startswith(".config.")],
            [],
        )

    def test_failed_migration_preserves_the_complete_legacy_file(self):
        original = "# old install\nPRINTER_HOST=home-scanner.local\n"
        self.write(original)

        with mock.patch.object(config.os, "replace", side_effect=OSError("stop")):
            with self.assertRaisesRegex(OSError, "stop"):
                config.load_scanner(migrate=True)

        with open(self.path) as stream:
            self.assertEqual(stream.read(), original)
        self.assertEqual(
            [name for name in os.listdir(self.root) if name.startswith(".config.")],
            [],
        )

    def test_hostname_is_resolved_fresh_and_preferred_over_saved_address(self):
        config.save(config.ConfiguredScanner(
            id="serial:home-scanner",
            host="home-scanner.local",
            address="192.0.2.10",
        ))
        with mock.patch.object(
                scan.discover, "resolve_ipv4",
                side_effect=("192.0.2.20", "192.0.2.21")) as resolve:
            self.assertEqual(scan.resolve_printer(), "192.0.2.20")
            self.assertEqual(scan.resolve_printer(), "192.0.2.21")

        self.assertEqual(resolve.call_count, 2)

    def test_protocol_is_validated(self):
        with self.assertRaisesRegex(ValueError, "unknown scanner protocol"):
            config.ConfiguredScanner(host="scanner.local", protocol="cups")


if __name__ == "__main__":
    unittest.main()
