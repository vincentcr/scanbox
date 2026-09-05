import unittest

from scanbox import selection
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
        self.assertIn(selection.describe(self.first), message)
        self.assertIn(selection.describe(self.second), message)

    def test_multiple_interactive_prompts_until_valid_choice(self):
        answers = iter(("", "3", "2"))
        output = []
        chosen = selection.select(
            (self.first, self.second), "auto", interactive=True,
            ask=lambda _prompt: next(answers), say=output.append,
        )
        self.assertIs(chosen, self.second)
        self.assertTrue(any("please enter" in line for line in output))


if __name__ == "__main__":
    unittest.main()
