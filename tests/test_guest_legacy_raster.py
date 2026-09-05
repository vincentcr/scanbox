import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from scanbox import paths


FAKE_SCANIMAGE = """\
#!/bin/bash
set -eu
output=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) output="$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf 'complete png raster' > "$output"
echo -ne 'Progress: 100.0%\r' >&2
"""


class GuestLegacyRasterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="scanbox-legacy-guest-")
        self.tools = os.path.join(self.root, "tools")
        self.output = os.path.join(self.root, "output")
        os.makedirs(self.tools)
        fake = os.path.join(self.tools, "scanimage")
        with open(fake, "w") as stream:
            stream.write(textwrap.dedent(FAKE_SCANIMAGE))
        os.chmod(fake, 0o755)
        self.env = dict(os.environ)
        self.env["PATH"] = self.tools + os.pathsep + self.env.get("PATH", "")
        self.env["SCANBOX_GUEST_OUTDIR"] = self.output

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_raster_mode_skips_guest_assembly_and_keeps_acquired_page(self) -> None:
        result = subprocess.run(
            [
                "bash", paths.GUEST_SCAN_SH, "hpaio:/net/test", "Flatbed",
                "Color", "300", "letter", "0", "name", "", "pdf",
                "0", "0", "1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            env=self.env,
        )

        raster = os.path.join(self.output, "p0001.png")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SOURCE Flatbed\n", result.stdout)
        self.assertIn("PAGES 1\n", result.stdout)
        self.assertIn("RASTER {}\n".format(raster), result.stdout)
        self.assertNotIn("OUT ", result.stdout)
        self.assertTrue(os.path.isfile(raster))


if __name__ == "__main__":
    unittest.main()
