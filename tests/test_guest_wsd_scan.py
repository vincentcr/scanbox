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
batch=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --source) source_name="$2"; shift 2 ;;
    -o) output="$2"; shift 2 ;;
    --batch=*) batch="${1#--batch=}"; shift ;;
    *) shift ;;
  esac
done
echo "$source_name" >> "$FAKE_SCAN_LOG"
if [ "$source_name" = "ADF" ]; then
  if [ "${FAKE_SCAN_RESULT:-complete}" = "io-error" ]; then
    echo 'scanimage: sane_read: Error during device I/O' >&2
    exit 9
  fi
  pages="${FAKE_SCAN_PAGES:-3}"
  index=1
  while [ "$index" -le "$pages" ]; do
    echo "Scanning page $index" >&2
    echo -ne 'Progress: 100.0%\\r' >&2
    if [ "${FAKE_SCAN_RESULT:-complete}" != "discard-last" ] || \
        [ "$index" -lt "$pages" ]; then
      target=$(printf "$batch" "$index")
      printf 'complete image %s' "$index" > "$target"
    fi
    index=$((index + 1))
  done
  if [ "${FAKE_SCAN_RESULT:-complete}" = "partial-error" ]; then
    echo 'scanimage: sane_read: Error during device I/O' >&2
    exit 9
  fi
  echo "Batch terminated, $pages pages scanned" >&2
  exit 0
fi
echo -ne 'Progress: 100.0%\\r' >&2
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

    def test_feeder_captures_every_page_in_one_batch_session(self) -> None:
        result = self.run_guest("ADF")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SOURCE ADF\n", result.stdout)
        self.assertIn("PAGES 3\n", result.stdout)
        self.assertIn("PAGE {}/p0001.png\n".format(self.output), result.stdout)
        self.assertIn("PAGE {}/p0002.png\n".format(self.output), result.stdout)
        self.assertIn("PAGE {}/p0003.png\n".format(self.output), result.stdout)
        self.assertNotIn("TRUNCATED", result.stdout)
        with open(self.log) as stream:
            self.assertEqual(stream.read().splitlines(), ["ADF"])

    def test_partial_batch_is_reported_as_truncated(self) -> None:
        self.env["FAKE_SCAN_RESULT"] = "partial-error"
        self.env["FAKE_SCAN_PAGES"] = "1"

        result = self.run_guest("ADF")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PAGES 1\n", result.stdout)
        self.assertIn("TRUNCATED 1\n", result.stdout)
        self.assertIn("WARNING feeder stopped", result.stdout)

    def test_reported_page_without_file_is_never_silent(self) -> None:
        self.env["FAKE_SCAN_RESULT"] = "discard-last"

        result = self.run_guest("ADF")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PAGES 2\n", result.stdout)
        self.assertIn("TRUNCATED 2\n", result.stdout)
        self.assertIn("WARNING feeder stopped", result.stdout)

    def test_auto_does_not_fall_back_after_ambiguous_feeder_failure(self) -> None:
        self.env["FAKE_SCAN_RESULT"] = "io-error"
        result = self.run_guest("auto", "ADF")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to fall back", result.stderr)
        with open(self.log) as stream:
            self.assertEqual(stream.read().splitlines(), ["ADF"])


if __name__ == "__main__":
    unittest.main()
