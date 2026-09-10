import io
import unittest
from unittest import mock

from scanbox import ui


class TTYBuffer(io.StringIO):
    def isatty(self):
        return True


class PromptTests(unittest.TestCase):
    def test_inherited_tty_does_not_require_opening_dev_tty(self):
        stdin = TTYBuffer("2\n")
        stderr = TTYBuffer()
        with mock.patch.object(ui.sys, "stdin", stdin), \
                mock.patch.object(ui.sys, "stderr", stderr), \
                mock.patch("builtins.open", side_effect=AssertionError):
            self.assertTrue(ui.tty_readable())
            self.assertEqual(ui.ask("Which one? "), "2")

        self.assertEqual(stderr.getvalue(), "Which one? ")


if __name__ == "__main__":
    unittest.main()
