import contextlib
import os
import shutil
import tempfile
import unittest
from unittest import mock

from scanbox import paths, proc
from scanbox.backends.hplip import (
    BACKEND_NAME,
    HPLIPBackend,
    HPLIPError,
    hpaio_uri,
    parse_summary,
)
from scanbox.contracts import (
    BackendErrorCode,
    ScanRequest,
    ScanSource,
    Scanner,
    UnsupportedRequest,
)


HPLIP_OPTIONS = """\
Options specific to device `hpaio:/net/test':
  Scan mode:
    --mode Lineart|Gray|Color [Color]
    --resolution 75|100|150|200|300|600|1200dpi [300]
    --source Flatbed|ADF [Flatbed]
"""


def legacy_scanner():
    uri = "hpaio:/net/HP_LaserJet_MFP?ip=192.0.2.20"
    return Scanner(uri, "HP LaserJet MFP", BACKEND_NAME, uri)


class HPLIPDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="scanbox-hplip-state-")
        self.state_patch = mock.patch.object(paths, "STATE_DIR", self.state)
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        shutil.rmtree(self.state, ignore_errors=True)

    def test_hp_makeuri_output_is_converted_to_hpaio(self):
        self.assertEqual(
            hpaio_uri("noise\nhp:/net/HP_LaserJet_MFP?ip=192.0.2.20\n"),
            "hpaio:/net/HP_LaserJet_MFP?ip=192.0.2.20",
        )
        self.assertIsNone(hpaio_uri("hp:/usb/not-a-network-scanner\n"))

    def test_discovery_is_explicit_cached_and_owns_guest_startup(self):
        commands = []
        ensures = []

        def runner(command, **_kwargs):
            commands.append(list(command))
            return proc.Result(
                0, "hp:/net/HP_LaserJet_MFP?ip=192.0.2.20\n", ""
            )

        backend = HPLIPBackend(
            "192.0.2.20", ensure_guest=lambda: ensures.append(True), runner=runner
        )
        first = backend.discover()
        second = backend.discover()

        self.assertEqual(first, second)
        self.assertEqual(ensures, [True])
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][-3:], ["hp-makeuri", "-c", "192.0.2.20"])
        self.assertEqual(first[0].backend, BACKEND_NAME)
        self.assertEqual(first[0].endpoint,
                         "hpaio:/net/HP_LaserJet_MFP?ip=192.0.2.20")
        self.assertEqual(first[0].manufacturer, "HP")
        with open(paths.uri_cache("192.0.2.20")) as stream:
            self.assertEqual(stream.read().strip(), first[0].endpoint)

    def test_inspection_maps_hplip_options_without_scanning(self):
        commands = []

        def runner(command, **_kwargs):
            commands.append(list(command))
            return proc.Result(0, HPLIP_OPTIONS, "")

        backend = HPLIPBackend(
            "192.0.2.20", ensure_guest=lambda: None, runner=runner
        )
        capabilities = backend.inspect(legacy_scanner())

        self.assertEqual(
            tuple(source.source for source in capabilities.sources),
            (ScanSource.FLATBED, ScanSource.FEEDER),
        )
        self.assertEqual(capabilities.sources[0].resolutions,
                         (75, 100, 150, 200, 300, 600, 1200))
        self.assertTrue(capabilities.sources[0].supports_lossless)
        self.assertIn("--all-options", commands[0])

    def test_prepare_rejects_nonlegacy_scanner_and_unsupported_source(self):
        backend = HPLIPBackend("192.0.2.20", ensure_guest=lambda: None)
        wrong = Scanner("id", "Xerox", "sane-airscan-wsd", "http://example.test")
        with self.assertRaisesRegex(ValueError, "does not belong"):
            backend.prepare(wrong, ScanRequest("id"))
        with self.assertRaises(UnsupportedRequest):
            backend.prepare(
                legacy_scanner(),
                ScanRequest(legacy_scanner().id, source="feeder-duplex"),
            )


class HPLIPScanJobTests(unittest.TestCase):
    def setUp(self):
        self.copied_dirs = []

    def tearDown(self):
        for directory in self.copied_dirs:
            shutil.rmtree(directory, ignore_errors=True)

    def test_guest_arguments_events_summary_and_page_copy(self):
        commands = []
        events = []

        def runner(command, **_kwargs):
            command = list(command)
            commands.append(command)
            if "pgrep" in command:
                return proc.Result(1, "", "")
            if command[:2] == ["limactl", "copy"]:
                os.makedirs(os.path.dirname(command[-1]), exist_ok=True)
                with open(command[-1], "wb") as stream:
                    stream.write(b"png raster")
                self.copied_dirs.append(os.path.dirname(command[-1]))
            return proc.Result(0, "", "")

        streamed = []

        def streaming(command, on_line, **kwargs):
            streamed.append((list(command), kwargs))
            for line in (
                "PROGRESS 42.0%",
                "NOTE the scanner is busy; retrying",
                "PHASE measuring page 1 of 2",
                "PAGE p0001 letter 11.0",
                "PAGE p0002 a4 11.7",
                "TRUNCATED 2",
                "SOURCE ADF",
                "PAGES 2",
                "RASTER /tmp/scanbox-out/p0001.png",
                "RASTER /tmp/scanbox-out/p0002.png",
            ):
                on_line(line)
            return proc.Result(0, "", "")

        backend = HPLIPBackend(
            "192.0.2.20", ensure_guest=lambda: None, runner=runner,
            streaming_runner=streaming,
            on_event=lambda kind, value: events.append((kind, value)),
        )
        request = ScanRequest(
            legacy_scanner().id, source="feeder", mode="Gray", resolution=600,
            page_size="legal", lossless=True,
        )
        with mock.patch("scanbox.backends.hplip.lock.Lock",
                        side_effect=contextlib.nullcontext):
            job = backend.prepare(legacy_scanner(), request)
            result = job.scan()

        guest_args = streamed[0][0]
        self.assertEqual(
            guest_args[-12:],
            [legacy_scanner().endpoint, "ADF", "Gray", "600", "legal", "1",
             "scan", job.run_id, "pdf", "0", "0", "1"],
        )
        self.assertEqual(streamed[0][1]["stdin_path"], paths.GUEST_SCAN_SH)
        self.assertEqual(events[:3], [
            ("progress", "42.0%"),
            ("note", "the scanner is busy; retrying"),
            ("phase", "measuring page 1 of 2"),
        ])
        self.assertEqual(events[-1], ("copy", "copying acquired pages"))
        self.assertEqual(result.source, ScanSource.FEEDER)
        self.assertEqual(len(result.pages), 2)
        self.assertTrue(result.truncated)
        self.assertEqual(job.measurements, (
            "p0001: letter (measured 11.0in)",
            "p0002: a4 (measured 11.7in)",
        ))
        self.assertTrue(all(os.path.isfile(page.path) for page in result.pages))
        self.assertIs(job.scan(), result)

    def test_incomplete_summary_is_a_protocol_error(self):
        def runner(command, **_kwargs):
            if "pgrep" in command:
                return proc.Result(1, "", "")
            return proc.Result(0, "", "")

        def streaming(_command, on_line, **_kwargs):
            on_line("SOURCE Flatbed")
            on_line("PAGES 1")
            return proc.Result(0, "", "")

        backend = HPLIPBackend(
            "192.0.2.20", ensure_guest=lambda: None,
            runner=runner, streaming_runner=streaming,
        )
        with mock.patch("scanbox.backends.hplip.lock.Lock",
                        side_effect=contextlib.nullcontext):
            with self.assertRaises(HPLIPError) as raised:
                backend.prepare(
                    legacy_scanner(), ScanRequest(legacy_scanner().id)
                ).scan()
        self.assertEqual(raised.exception.code, BackendErrorCode.PROTOCOL)
        self.assertEqual(str(raised.exception), "the VM produced no acquired pages")

    def test_cancel_before_scan_is_idempotent_and_moves_no_paper(self):
        streamed = []
        backend = HPLIPBackend(
            "192.0.2.20", ensure_guest=lambda: None,
            streaming_runner=lambda *_args, **_kwargs: streamed.append(True),
        )
        job = backend.prepare(legacy_scanner(), ScanRequest(legacy_scanner().id))
        job.cancel()
        job.cancel()
        with self.assertRaises(HPLIPError) as raised:
            job.scan()
        self.assertEqual(raised.exception.code, BackendErrorCode.CANCELLED)
        self.assertEqual(streamed, [])

    def test_interrupted_stream_aborts_the_remote_process_group(self):
        commands = []

        def runner(command, **_kwargs):
            commands.append(list(command))
            if "pgrep" in command:
                return proc.Result(1, "", "")
            return proc.Result(0, "", "")

        def streaming(*_args, **_kwargs):
            raise KeyboardInterrupt

        backend = HPLIPBackend(
            "192.0.2.20", ensure_guest=lambda: None,
            runner=runner, streaming_runner=streaming,
        )
        with mock.patch("scanbox.backends.hplip.lock.Lock",
                        side_effect=contextlib.nullcontext):
            with self.assertRaises(KeyboardInterrupt):
                backend.prepare(
                    legacy_scanner(), ScanRequest(legacy_scanner().id)
                ).scan()

        aborts = [command for command in commands
                  if "bash" in command and "kill -TERM" in command[-1]]
        self.assertEqual(len(aborts), 1)
        self.assertIn("/tmp/scanbox-run-", aborts[0][-1])

    def test_summary_parser_keeps_repeated_fields(self):
        fields = parse_summary([
            "SOURCE ADF", "PAGES 2", "RASTER one", "RASTER two", "TRUNCATED 2"
        ])
        self.assertEqual(fields["RASTER"], [["one"], ["two"]])
        self.assertIn("TRUNCATED", fields)


if __name__ == "__main__":
    unittest.main()
