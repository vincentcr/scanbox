import unittest
from unittest import mock

from scanbox import config, selection
from scanbox.contracts import (
    Backend,
    BackendError,
    BackendErrorCode,
    Capabilities,
    Scanner,
)


class FakeBackend(Backend):
    def __init__(self, name, scanners=(), error=None):
        self._name = name
        self.scanners = tuple(scanners)
        self.error = error
        self.discoveries = 0

    @property
    def name(self):
        return self._name

    def discover(self):
        self.discoveries += 1
        if self.error:
            raise self.error
        return self.scanners

    def inspect(self, scanner):
        raise AssertionError("catalog discovery must not inspect or start a VM")

    def prepare(self, scanner, request):
        raise AssertionError("catalog discovery must not prepare a scan")


def scanner(identifier, name, backend="test", endpoint=None):
    return Scanner(identifier, name, backend, endpoint or "https://example.test/scan")


class CatalogTests(unittest.TestCase):
    def test_production_catalog_contains_wsd_and_hplip_discovery(self):
        catalog = selection.current_network_catalog(discovery_seconds=0.1)
        self.assertEqual(tuple(backend.name for backend in catalog.backends), (
            "wsd", "hplip",
        ))

    def test_zero_one_and_duplicate_discovery_cases(self):
        empty = FakeBackend("empty")
        item = scanner("scanner-1", "Office scanner")
        backend = FakeBackend("test", (item, item))

        inventory = selection.Catalog((empty, backend)).discover()

        self.assertEqual(tuple(c.scanner for c in inventory.candidates), (item,))
        self.assertEqual((empty.discoveries, backend.discoveries), (1, 1))
        self.assertEqual(inventory.failures, ())

    def test_failed_and_unusable_backend_results_are_filtered(self):
        failed = FakeBackend(
            "failed", error=BackendError(
                BackendErrorCode.UNAVAILABLE, "network unavailable", backend="failed"
            )
        )
        mismatched = FakeBackend(
            "registered", (scanner("scanner-2", "Wrong backend", "unregistered"),)
        )

        inventory = selection.Catalog((failed, mismatched)).discover()

        self.assertEqual(inventory.candidates, ())
        self.assertEqual(len(inventory.failures), 2)
        self.assertEqual(inventory.failures[0].backend, "failed")
        self.assertIn("network unavailable", inventory.failures[0].message)
        self.assertIn("do not match", inventory.failures[1].message)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend("test")
        self.first = selection.Candidate(
            scanner("stable:first", "Alpha scanner"), self.backend
        )
        self.second = selection.Candidate(
            scanner("stable:second", "Beta scanner"), self.backend
        )

    def test_zero_and_one_auto_cases(self):
        with self.assertRaisesRegex(selection.SelectionError, "no usable scanners"):
            selection.select((), "auto")
        self.assertIs(selection.select((self.first,), "auto"), self.first)

    def test_exact_name_and_stable_id_select_case_insensitively(self):
        candidates = (self.first, self.second)
        self.assertIs(selection.select(candidates, "BETA SCANNER"), self.second)
        self.assertIs(selection.select(candidates, "STABLE:FIRST"), self.first)
        with self.assertRaisesRegex(selection.SelectionError, "no scanner found"):
            selection.select(candidates, "scanner")

    def test_multiple_noninteractive_lists_candidates_and_fails(self):
        with self.assertRaises(selection.SelectionError) as raised:
            selection.select((self.first, self.second), "auto")
        message = str(raised.exception)
        self.assertIn("Alpha scanner", message)
        self.assertIn("Beta scanner", message)

    def test_multiple_interactive_prompts_until_valid_choice(self):
        answers = iter(("", "3", "2"))
        output = []
        chosen = selection.select(
            (self.first, self.second), "auto", interactive=True,
            ask=lambda _prompt: next(answers), say=output.append,
        )
        self.assertIs(chosen, self.second)
        self.assertTrue(any("please enter" in line for line in output))

    def test_cross_backend_uuid_advertisements_form_one_physical_scanner(self):
        wsd_backend = FakeBackend("wsd")
        hplip_backend = FakeBackend("hplip")
        candidates = (
            selection.Candidate(scanner(
                "wsd:urn:uuid:5de90400-1dd2-11b2-84bc-9c934e010299",
                "Office scanner", "wsd", "http://192.0.2.20/ws/",
            ), wsd_backend),
            selection.Candidate(scanner(
                "uuid:5DE90400-1DD2-11B2-84BC-9C934E010299",
                "Office scanner", "hplip", "office.local",
            ), hplip_backend),
        )

        groups = selection.group_candidates(candidates)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].backend_names, ("wsd", "hplip"))

    def test_saved_scanner_matches_identity_before_locator(self):
        candidate = selection.Candidate(scanner(
            "wsd:urn:uuid:5de90400-1dd2-11b2-84bc-9c934e010299",
            "Office scanner", "wsd", "http://192.0.2.99/ws/",
        ), FakeBackend("wsd"))
        group = selection.group_candidates((candidate,))[0]
        saved = config.ConfiguredScanner(
            id="uuid:5DE90400-1DD2-11B2-84BC-9C934E010299",
            host="old.local", backend="wsd",
        )

        self.assertTrue(selection.matches_saved(group, saved))

    def test_validation_falls_back_before_any_scan_is_prepared(self):
        broken = FakeBackend("wsd", error=None)
        working = FakeBackend("hplip", error=None)
        broken.inspect = mock.Mock(side_effect=BackendError(
            BackendErrorCode.UNAVAILABLE, "not usable", backend="wsd"
        ))
        working.inspect = mock.Mock(return_value=object())
        group = selection.group_candidates((
            selection.Candidate(scanner("serial:test", "Test", "wsd"), broken),
            selection.Candidate(scanner("serial:test", "Test", "hplip"), working),
        ))[0]

        chosen = selection.validate(group)

        self.assertIs(chosen.backend, working)
        broken.inspect.assert_called_once()
        working.inspect.assert_called_once()


if __name__ == "__main__":
    unittest.main()
