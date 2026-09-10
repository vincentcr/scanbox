import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from scanbox import config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="scanbox-config-")
        self.path = os.path.join(self.root, "scanners.json")
        self.patch = mock.patch.object(config, "CONFIG_FILE", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_multiple_scanners_and_preference_round_trip(self):
        hp = config.ConfiguredScanner(
            id="serial:hp-home", name="HP home", host="hp.local", backend="hplip"
        )
        xerox = config.ConfiguredScanner(
            id="wsd:urn:uuid:5de90400-1dd2-11b2-84bc-9c934e010299",
            name="Xerox office", address="192.0.2.52", backend="wsd",
        )
        expected = config.ScannerRegistry((hp, xerox), xerox.key)

        config.save_registry(expected)

        self.assertEqual(config.load_registry(), expected)
        with open(self.path) as stream:
            stored = json.load(stream)
        self.assertEqual(stored["version"], 1)
        self.assertEqual(len(stored["scanners"]), 2)
        self.assertEqual(stored["preferred"], xerox.key)

    def test_remember_adds_and_updates_without_replacing_other_scanners(self):
        hp = config.ConfiguredScanner(
            id="serial:hp-home", name="HP", host="old.local", backend="hplip"
        )
        xerox = config.ConfiguredScanner(
            id="uuid:5de90400-1dd2-11b2-84bc-9c934e010299",
            name="Xerox", host="xerox.local", backend="wsd",
        )
        config.remember(hp)
        config.remember(xerox)
        updated_hp = config.ConfiguredScanner(
            id="SERIAL:HP-HOME", name="HP home", host="new.local", backend="hplip"
        )

        registry = config.remember(updated_hp)

        self.assertEqual(len(registry.scanners), 2)
        self.assertEqual(registry.preferred, updated_hp.key)
        self.assertEqual(registry.scanners[0].host, "new.local")

    def test_discovered_identity_upgrades_same_host_without_duplicate(self):
        manual = config.ConfiguredScanner(
            name="Office", host="office.local", backend="hplip"
        )
        config.remember(manual)
        discovered = config.ConfiguredScanner(
            id="serial:office-123", name="Office", host="office.local",
            backend="hplip",
        )

        registry = config.remember(discovered)

        self.assertEqual(registry.scanners, (discovered,))
        self.assertEqual(registry.preferred, discovered.key)

    def test_forget_and_set_preferred_use_exact_saved_selectors(self):
        hp = config.ConfiguredScanner(
            id="serial:hp-home", name="HP home", host="hp.local", backend="hplip"
        )
        xerox = config.ConfiguredScanner(
            id="serial:xerox-office", name="Xerox office",
            host="xerox.local", backend="wsd",
        )
        config.save_registry(config.ScannerRegistry((hp, xerox), hp.key))

        preferred = config.set_preferred("Xerox office")
        remaining = config.forget("Xerox office")

        self.assertEqual(preferred.preferred, xerox.key)
        self.assertEqual(remaining.scanners, (hp,))
        self.assertEqual(remaining.preferred, hp.key)

    def test_previous_environment_format_is_not_accepted(self):
        with open(self.path, "w") as stream:
            stream.write("SCANNER_HOST=old.local\nSCANNER_BACKEND=hplip\n")

        with self.assertRaisesRegex(ValueError, "invalid scanner registry"):
            config.load_registry()

    def test_failed_atomic_write_preserves_complete_registry(self):
        original = config.ScannerRegistry((
            config.ConfiguredScanner(host="home.local", backend="hplip"),
        ))
        config.save_registry(original)
        contents = config.read_raw()

        with mock.patch.object(config.os, "replace", side_effect=OSError("stop")):
            with self.assertRaisesRegex(OSError, "stop"):
                config.save_registry(config.ScannerRegistry((
                    config.ConfiguredScanner(host="other.local", backend="wsd"),
                )))

        self.assertEqual(config.read_raw(), contents)
        self.assertEqual(
            [name for name in os.listdir(self.root) if name.startswith(".scanners.")],
            [],
        )

    def test_backend_and_duplicate_identity_are_validated(self):
        with self.assertRaisesRegex(ValueError, "unknown scanner backend"):
            config.ConfiguredScanner(host="scanner.local", backend="cups")
        first = config.ConfiguredScanner(
            id="wsd:urn:uuid:5de90400-1dd2-11b2-84bc-9c934e010299",
            backend="wsd",
        )
        second = config.ConfiguredScanner(
            id="uuid:5DE90400-1DD2-11B2-84BC-9C934E010299",
            backend="hplip",
        )
        with self.assertRaisesRegex(ValueError, "duplicate scanner identities"):
            config.ScannerRegistry((first, second))


if __name__ == "__main__":
    unittest.main()
