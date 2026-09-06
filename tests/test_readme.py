import os
import shlex
import unittest

from scanbox import cli, paths


class ReadmeExamplesTests(unittest.TestCase):
    def test_every_standalone_scanbox_example_parses(self) -> None:
        readme = os.path.join(paths.ROOT, "README.md")
        with open(readme) as stream:
            commands = []
            shell_block = False
            for line in stream:
                stripped = line.strip()
                if stripped == "```sh":
                    shell_block = True
                    continue
                if stripped == "```":
                    shell_block = False
                    continue
                if shell_block and (
                        stripped == "scanbox"
                        or stripped.startswith("scanbox ")):
                    commands.append(shlex.split(stripped, comments=True))

        self.assertGreater(len(commands), 10)
        for command in commands:
            with self.subTest(command=command):
                cli.build_parser().parse_args(command[1:])


if __name__ == "__main__":
    unittest.main()
