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
source_name=""
output=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --source) source_name="$2"; shift 2 ;;
    -o) output="$2"; shift 2 ;;
    *) shift ;;
  esac
done
echo "$source_name" >> "$FAKE_SCAN_LOG"
echo -ne 'Progress: 100.0%\\r' >&2
if [ "$source_name" = "ADF" ]; then
  if [ "${FAKE_SCAN_RESULT:-page-then-empty}" = "io-error" ]; then
    echo 'scanimage: sane_read: Error during device I/O' >&2
    exit 9
  fi
  printf 'complete image' > "$output"
  echo 'scanimage: sane_read: Document feeder out of documents' >&2
  exit 7
fi
printf 'complete image' > "$output"
"""


class GuestWSDScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = tempfile.mkdtemp(prefix="scanbox-wsd-tools-")
        self.output = tempfile.mkdtemp(prefix="scanbox-wsd-test-", dir="/tmp")
        self.log = os.path.join(self.tools, "calls")
        fake = os.path.join(self.tools, "scanimage")
        with open(fake, "w") as stream:
            stream.write(textwrap.dedent(FAKE_SCANIMAGE))
        os.chmod(fake, 0o755)
        self.env = dict(os.environ)
        self.env["PATH"] = self.tools + os.pathsep + self.env.get("PATH", "")
        self.env["FAKE_SCAN_LOG"] = self.log

    def tearDown(self) -> None:
        shutil.rmtree(self.tools, ignore_errors=True)
        shutil.rmtree(self.output, ignore_errors=True)

    def run_guest(self, source: str, auto_feeder: str = ""):
        return subprocess.run(
            [
                "bash", paths.GUEST_WSD_SCAN_SH,
                "SANE_AIRSCAN_DEVICE=wsd:scanbox-wsd:http://192.0.2.25/ws/",
                "airscan:w0:scanbox-wsd", source, auto_feeder,
                "Gray", "200", "auto", "123-456", self.output,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            env=self.env,
        )

    def test_feeder_keeps_page_when_xerox_returns_no_docs_from_final_read(self) -> None:
        result = self.run_guest("ADF")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SOURCE ADF\n", result.stdout)
        self.assertIn("PAGES 1\n", result.stdout)
        self.assertIn("PAGE {}/p0001.png\n".format(self.output), result.stdout)
        self.assertNotIn("TRUNCATED", result.stdout)

    def test_auto_does_not_fall_back_after_ambiguous_feeder_failure(self) -> None:
        self.env["FAKE_SCAN_RESULT"] = "io-error"
        result = self.run_guest("auto", "ADF")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to fall back", result.stderr)
        with open(self.log) as stream:
            self.assertEqual(stream.read().splitlines(), ["ADF"])


if __name__ == "__main__":
    unittest.main()
