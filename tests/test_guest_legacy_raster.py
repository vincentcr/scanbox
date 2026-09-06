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

BUSY_THEN_SUCCESS_SCANIMAGE = """\
#!/bin/bash
set -eu
count=0
[ ! -f "$SCANBOX_TEST_COUNT" ] || count=$(cat "$SCANBOX_TEST_COUNT")
count=$((count + 1))
printf '%s' "$count" > "$SCANBOX_TEST_COUNT"
if [ "$count" -lt 3 ]; then
  echo 'scanimage: sane_start: Error during device I/O' >&2
  exit 1
fi
output=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) output="$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf 'complete png raster' > "$output"
"""

FEEDER_SCANIMAGE = """\
#!/bin/bash
set -eu
batch=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --batch=*) batch="${1#--batch=}" ;;
  esac
  shift
done
printf 'first page' > "${batch%p%04d.png}p0001.png"
printf 'second page' > "${batch%p%04d.png}p0002.png"
echo 'scanimage: sane_start: Document feeder out of documents' >&2
exit 1
"""

FAKE_AUTOFIT = """\
#!/bin/bash
set -eu
printf '%s %s\\n' "$1" "$(basename "$2")" >> "$SCANBOX_AUTOFIT_LOG"
if [ "$1" = measure ]; then
  echo 'letter 3300 11.00'
fi
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

    def test_flatbed_retries_hplip_busy_session_before_succeeding(self) -> None:
        fake = os.path.join(self.tools, "scanimage")
        with open(fake, "w") as stream:
            stream.write(textwrap.dedent(BUSY_THEN_SUCCESS_SCANIMAGE))
        os.chmod(fake, 0o755)
        count = os.path.join(self.root, "attempts")
        self.env["SCANBOX_TEST_COUNT"] = count
        self.env["SCANBOX_BUSY_TRIES"] = "3"
        self.env["SCANBOX_BUSY_WAIT"] = "0"

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

        self.assertEqual(result.returncode, 0, result.stderr)
        with open(count) as stream:
            self.assertEqual(stream.read(), "3")
        self.assertEqual(result.stdout.count("NOTE the scanner is still busy"), 2)
        self.assertIn("PAGES 1\n", result.stdout)

    def test_auto_sized_feeder_measures_and_crops_every_page(self) -> None:
        scanimage = os.path.join(self.tools, "scanimage")
        with open(scanimage, "w") as stream:
            stream.write(textwrap.dedent(FEEDER_SCANIMAGE))
        os.chmod(scanimage, 0o755)
        autofit = os.path.join(self.tools, "autofit")
        with open(autofit, "w") as stream:
            stream.write(textwrap.dedent(FAKE_AUTOFIT))
        os.chmod(autofit, 0o755)
        log = os.path.join(self.root, "autofit.log")
        self.env["SCANBOX_AUTOFIT"] = autofit
        self.env["SCANBOX_AUTOFIT_LOG"] = log

        result = subprocess.run(
            [
                "bash", paths.GUEST_SCAN_SH, "hpaio:/net/test", "ADF",
                "Color", "300", "auto", "0", "name", "", "pdf",
                "0", "0", "1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            env=self.env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SOURCE ADF\n", result.stdout)
        self.assertIn("PAGES 2\n", result.stdout)
        self.assertEqual(result.stdout.count("RASTER "), 2)
        with open(log) as stream:
            self.assertEqual(stream.read().splitlines(), [
                "measure p0001.png", "crop p0001.png",
                "measure p0002.png", "crop p0002.png",
            ])


if __name__ == "__main__":
    unittest.main()
