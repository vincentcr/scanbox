import os
import shutil
import struct
import unittest
from unittest import mock

from scanbox import proc, vm
from scanbox.backends.wsd import (
    BACKEND_NAME,
    WSDBackend,
    _airscan_environment,
    parse_probe_response,
)
from scanbox.contracts import (
    BackendError,
    BackendErrorCode,
    ScanRequest,
    ScanSource,
    Scanner,
    UnsupportedRequest,
)


PROBE_RESPONSE = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:scan="http://schemas.microsoft.com/windows/2006/08/wdp/scan">
 <s:Body><d:ProbeMatches><d:ProbeMatch>
  <a:EndpointReference>
   <a:Address>urn:uuid:01234567-89ab-cdef-0123-456789abcdef</a:Address>
  </a:EndpointReference>
  <d:Types>scan:ScanDeviceType</d:Types>
  <d:Scopes>http://example.test/device/name/Xerox%20WorkCentre%206605DN</d:Scopes>
  <d:XAddrs>http://192.0.2.25:5358/WSDScanner</d:XAddrs>
 </d:ProbeMatch></d:ProbeMatches></s:Body>
</s:Envelope>
"""

SANE_OPTIONS = """\
Options specific to device `airscan:w0:scanbox-wsd':
  Standard:
    --source Flatbed|ADF|ADF Duplex [Flatbed]
    --mode Color|Gray [Color]
    --resolution 200|300|400|600dpi [300]
  Geometry:
    -l 0..215.9mm [0]
    -t 0..355.6mm [0]
    -x 0..215.9mm [215.9]
    -y 0..355.6mm [355.6]
"""


def scanner() -> Scanner:
    return Scanner(
        id="wsd:urn:uuid:01234567-89ab-cdef-0123-456789abcdef",
        name="Xerox WorkCentre 6605DN",
        backend=BACKEND_NAME,
        endpoint="http://192.0.2.25:5358/WSDScanner",
        transport="network-wsd",
    )


class ProbeResponseTests(unittest.TestCase):
    def test_scan_device_has_stable_identity_and_explicit_endpoint(self) -> None:
        found = parse_probe_response(PROBE_RESPONSE)

        self.assertEqual(len(found), 1)
        self.assertEqual(
            found[0].id,
            "wsd:urn:uuid:01234567-89ab-cdef-0123-456789abcdef",
        )
        self.assertEqual(found[0].name, "Xerox WorkCentre 6605DN")
        self.assertEqual(found[0].endpoint, "http://192.0.2.25:5358/WSDScanner")
        self.assertEqual(found[0].backend, BACKEND_NAME)

    def test_ip_address_is_never_used_as_persistent_identity(self) -> None:
        response = PROBE_RESPONSE.replace(
            b"urn:uuid:01234567-89ab-cdef-0123-456789abcdef",
            b"http://192.0.2.25:5358/WSDScanner",
        )
        self.assertEqual(parse_probe_response(response), ())

    def test_non_scan_and_malformed_responses_are_ignored(self) -> None:
        self.assertEqual(
            parse_probe_response(PROBE_RESPONSE.replace(
                b"scan:ScanDeviceType", b"dn:PrintDeviceType"
            )),
            (),
        )
        self.assertEqual(parse_probe_response(b"not XML"), ())


class FakeRunner:
    def __init__(self) -> None:
        self.commands = []

    def __call__(self, command, **kwargs):
        command = list(command)
        self.commands.append(command)
        if command[-1] == "-L":
            return proc.Result(
                0,
                "device `airscan:w0:scanbox-wsd' is a WSD Xerox scanner\n",
                "",
            )
        if "--all-options" in command:
            options = SANE_OPTIONS
            if "--source" in command:
                source = command[command.index("--source") + 1]
                if source == "Flatbed":
                    options = options.replace("355.6mm [355.6]", "297.053mm [297.053]")
            return proc.Result(0, options, "")
        if len(command) >= 3 and command[:2] == ["limactl", "copy"]:
            width, height = 1700, 2800
            header = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            with open(command[-1], "wb") as stream:
                stream.write(header + struct.pack(">II", width, height))
            return proc.Result(0, "", "")
        return proc.Result(0, "", "")


class WSDBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = FakeRunner()
        self.ensure_count = 0

        def ensure() -> None:
            self.ensure_count += 1

        self.backend = WSDBackend(ensure_guest=ensure, runner=self.runner)

    def test_discovery_does_not_ensure_or_inspect_the_guest(self) -> None:
        with mock.patch(
            "scanbox.backends.wsd.discover_wsd", return_value=(scanner(),)
        ) as discover:
            self.assertEqual(self.backend.discover(), (scanner(),))
        discover.assert_called_once_with(3.0)
        self.assertEqual(self.ensure_count, 0)
        self.assertEqual(self.runner.commands, [])

    def test_inspection_maps_xerox_sane_capabilities(self) -> None:
        capabilities = self.backend.inspect(scanner())

        self.assertEqual(self.ensure_count, 1)
        self.assertEqual(
            tuple(item.source for item in capabilities.sources),
            (ScanSource.FLATBED, ScanSource.FEEDER, ScanSource.FEEDER_DUPLEX),
        )
        for source in capabilities.sources:
            self.assertEqual(tuple(str(mode) for mode in source.modes),
                             ("color", "grayscale"))
            self.assertEqual(source.resolutions, (200, 300, 400, 600))
            self.assertFalse(source.supports_lossless)
        self.assertEqual(capabilities.sources[0].max_size_mm, (215.9, 297.053))
        self.assertEqual(capabilities.sources[1].max_size_mm, (215.9, 355.6))
        self.assertEqual(capabilities.sources[2].max_size_mm, (215.9, 355.6))

        commands = [" ".join(command) for command in self.runner.commands]
        self.assertTrue(all("SANE_AIRSCAN_DEVICE=wsd:" in command
                            for command in commands))
        self.assertTrue(all("hpaio" not in command and "hp-plugin" not in command
                            for command in commands))

    def test_inspection_is_cached_for_same_current_endpoint(self) -> None:
        first = self.backend.inspect(scanner())
        command_count = len(self.runner.commands)
        second = self.backend.inspect(scanner())

        self.assertIs(first, second)
        self.assertEqual(len(self.runner.commands), command_count)
        self.assertEqual(self.ensure_count, 1)

    def test_lossless_request_is_rejected_before_scanning(self) -> None:
        with self.assertRaises(UnsupportedRequest):
            self.backend.prepare(
                scanner(),
                ScanRequest(scanner().id, source="flatbed", resolution=200,
                            lossless=True),
            )

    def test_page_size_is_checked_per_source(self) -> None:
        with self.assertRaisesRegex(UnsupportedRequest, "legal paper"):
            self.backend.prepare(
                scanner(),
                ScanRequest(scanner().id, source="flatbed", resolution=200,
                            page_size="legal"),
            )

        job = self.backend.prepare(
            scanner(),
            ScanRequest(scanner().id, source="auto", resolution=200,
                        page_size="legal"),
        )
        self.assertEqual(
            tuple(source.source for source in job.compatible),
            (ScanSource.FEEDER, ScanSource.FEEDER_DUPLEX),
        )

    def test_scanner_from_another_backend_is_rejected(self) -> None:
        wrong = Scanner("id", "scanner", "hpaio", "hpaio:/net/example")
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.backend.inspect(wrong)

    def test_guest_is_forced_to_wsd_at_the_host_discovered_endpoint(self) -> None:
        value = _airscan_environment(scanner())
        self.assertEqual(
            value,
            "SANE_AIRSCAN_DEVICE=wsd:scanbox-wsd:"
            "http://192.0.2.25:5358/WSDScanner",
        )

    def test_release_arms_the_shared_vm_idle_timer(self) -> None:
        with mock.patch("scanbox.backends.wsd.vm.idle_timer_arm") as arm:
            self.backend.release(17)
        arm.assert_called_once_with(17)


class WSDScanJobTests(unittest.TestCase):
    def test_scan_returns_host_pages_progress_and_short_batch_warning(self) -> None:
        runner = FakeRunner()
        events = []

        def stream(command, on_line, **kwargs):
            self.assertIn("SANE_AIRSCAN_DEVICE=wsd:", " ".join(command))
            on_line("PROGRESS 50.0%")
            on_line("TRUNCATED 1")
            on_line("WARNING feeder stopped before reporting it was empty")
            on_line("SOURCE ADF")
            on_line("PAGES 1")
            on_line("PAGE /tmp/scanbox-wsd-run/p0001.png")
            return proc.Result(0, "", "")

        backend = WSDBackend(
            ensure_guest=lambda: None,
            runner=runner,
            streaming_runner=stream,
            on_event=lambda kind, value: events.append((kind, value)),
        )
        request = ScanRequest(scanner().id, source="feeder", mode="Gray",
                              resolution=200)
        result = backend.prepare(scanner(), request).scan()
        try:
            self.assertEqual(result.backend, BACKEND_NAME)
            self.assertEqual(result.source, ScanSource.FEEDER)
            self.assertTrue(result.truncated)
            self.assertEqual(len(result.pages), 1)
            self.assertTrue(os.path.isabs(result.pages[0].path))
            self.assertEqual((result.pages[0].width_px, result.pages[0].height_px),
                             (1700, 2800))
            self.assertEqual(events, [("progress", "50.0%")])
            self.assertEqual(
                result.warnings,
                ("feeder stopped before reporting it was empty",),
            )
        finally:
            shutil.rmtree(os.path.dirname(result.pages[0].path), ignore_errors=True)

    def test_cancel_before_scan_is_idempotent_and_moves_no_paper(self) -> None:
        streamed = []

        def stream(*args, **kwargs):
            streamed.append(True)
            return proc.Result(0, "", "")

        backend = WSDBackend(
            ensure_guest=lambda: None,
            runner=FakeRunner(),
            streaming_runner=stream,
        )
        request = ScanRequest(scanner().id, source="flatbed", resolution=200)
        job = backend.prepare(scanner(), request)
        job.cancel()
        job.cancel()

        with self.assertRaises(BackendError) as raised:
            job.scan()
        self.assertEqual(raised.exception.code, BackendErrorCode.CANCELLED)
        self.assertEqual(streamed, [])


class WSDProvisioningTests(unittest.TestCase):
    @mock.patch("scanbox.vm.provision_wsd")
    @mock.patch("scanbox.vm.is_wsd_provisioned", return_value=False)
    @mock.patch("scanbox.vm.ensure_runtime")
    def test_wsd_ensure_uses_only_wsd_provisioning(
        self, ensure_runtime, is_provisioned, provision_wsd
    ) -> None:
        with mock.patch("scanbox.vm.provision") as provision_hplip:
            vm.ensure_wsd()

        ensure_runtime.assert_called_once_with()
        is_provisioned.assert_called_once_with()
        provision_wsd.assert_called_once_with()
        provision_hplip.assert_not_called()


if __name__ == "__main__":
    unittest.main()
