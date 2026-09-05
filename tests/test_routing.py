import unittest

from scanbox import config, routing
from scanbox.backends import hplip
from scanbox.contracts import (
    Backend,
    BackendError,
    BackendErrorCode,
    Capabilities,
    ScanMode,
    ScanRequest,
    ScanSource,
    Scanner,
    SourceCapabilities,
)


CAPABILITIES_SOURCE = SourceCapabilities(
    ScanSource.FLATBED,
    (ScanMode.COLOR, ScanMode.GRAYSCALE, ScanMode.LINEART),
    (300, 600),
    supports_lossless=True,
)


class FakeJob:
    def __init__(self, error=None):
        self.error = error
        self.scan_calls = 0

    @property
    def result(self):
        return None

    def scan(self):
        self.scan_calls += 1
        if self.error:
            raise self.error
        raise AssertionError("test should not acquire paper")

    def cancel(self):
        pass


class FakeBackend(Backend):
    def __init__(self, name, scanners=(), *, prepare_error=None, job=None):
        self._name = name
        self.scanners = tuple(scanners)
        self.prepare_error = prepare_error
        self.job = job or FakeJob()
        self.discover_calls = 0
        self.inspect_calls = 0
        self.prepare_calls = 0
        self.prepared_request = None
        self.on_event = lambda _kind, _value: None

    @property
    def name(self):
        return self._name

    def discover(self):
        self.discover_calls += 1
        return self.scanners

    def inspect(self, scanner):
        self.inspect_calls += 1
        return Capabilities(scanner.id, (CAPABILITIES_SOURCE,))

    def prepare(self, scanner, request):
        self.prepare_calls += 1
        self.prepared_request = request
        if self.prepare_error:
            raise self.prepare_error
        return self.job

    def matches_locator(self, scanner, locators):
        return scanner.endpoint in locators


def wsd_scanner(identifier="wsd:urn:uuid:5de90400-1dd2-11b2-84bc-9c934e010299",
                name="Office scanner", endpoint="192.0.2.52"):
    return Scanner(identifier, name, "sane-airscan-wsd", endpoint)


def legacy_scanner():
    return Scanner(
        "hpaio:/net/HP_Test?ip=192.0.2.20", "HP Test", "hplip-legacy",
        "hpaio:/net/HP_Test?ip=192.0.2.20", manufacturer="HP",
    )


class IdentityTests(unittest.TestCase):
    def test_uuid_spellings_group_one_physical_scanner(self):
        first = wsd_scanner()
        second = Scanner(
            "uuid:5DE90400-1DD2-11B2-84BC-9C934E010299",
            "Office scanner", "imagecapture", "native-id",
        )

        groups = routing.group_scanners((second, first))

        self.assertEqual(len(groups), 1)
        self.assertEqual(
            groups[0].identity,
            "uuid:5de90400-1dd2-11b2-84bc-9c934e010299",
        )
        self.assertEqual({item.backend for item in groups[0].scanners}, {
            "imagecapture", "sane-airscan-wsd"
        })

    def test_names_and_addresses_do_not_merge_without_a_strong_identity(self):
        first = Scanner("opaque-a", "Same", "one", "192.0.2.20")
        second = Scanner("opaque-b", "Same", "two", "192.0.2.20")
        self.assertEqual(len(routing.group_scanners((first, second))), 2)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.request = ScanRequest(
            "configured", source=ScanSource.FLATBED,
            mode=ScanMode.COLOR, resolution=300,
        )
        self.legacy_instances = []

    def legacy_factory(self, address, on_event=None):
        backend = FakeBackend("hplip-legacy", (legacy_scanner(),))
        backend.address = address
        backend.on_event = on_event
        self.legacy_instances.append(backend)
        return backend

    def router(self, wsd, *, resolver=lambda _host: "192.0.2.20",
               legacy_support=lambda _configured: True):
        return routing.Router(
            wsd_backend=wsd,
            legacy_factory=self.legacy_factory,
            legacy_support=legacy_support,
            resolver=resolver,
        )

    def test_auto_prefers_wsd_by_stable_identity_without_resolving_locator(self):
        scanner = wsd_scanner(endpoint="not-a-locator")
        wsd = FakeBackend("sane-airscan-wsd", (scanner,))
        resolutions = []
        configured = config.ConfiguredScanner(
            id="uuid:5de90400-1dd2-11b2-84bc-9c934e010299",
            name="Xerox WorkCentre", host="xerox.local",
        )

        route = self.router(
            wsd, resolver=lambda host: resolutions.append(host)
        ).prepare(configured, self.request)

        self.assertEqual(route.protocol, "wsd")
        self.assertIs(route.scanner, scanner)
        self.assertEqual(resolutions, [])
        self.assertEqual(self.legacy_instances, [])
        self.assertEqual(wsd.prepared_request.scanner_id, scanner.id)
        self.assertIn("stable identity", route.diagnostics[-1])

    def test_locator_is_used_only_after_stable_identity_does_not_match(self):
        matching_locator = wsd_scanner(
            "wsd:urn:uuid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            endpoint="192.0.2.52",
        )
        wrong_locator = wsd_scanner(
            "wsd:urn:uuid:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            endpoint="192.0.2.99",
        )
        wsd = FakeBackend("sane-airscan-wsd", (wrong_locator, matching_locator))
        configured = config.ConfiguredScanner(
            id="uuid:cccccccc-cccc-cccc-cccc-cccccccccccc",
            name="HP Office scanner", host="office.local",
        )

        route = self.router(
            wsd, resolver=lambda _host: "192.0.2.52"
        ).prepare(configured, self.request)

        self.assertIs(route.scanner, matching_locator)
        self.assertIn("fallback locator", route.diagnostics[-1])

    def test_auto_falls_back_to_legacy_when_wsd_is_absent(self):
        wsd = FakeBackend("sane-airscan-wsd")
        configured = config.ConfiguredScanner(
            name="HP LaserJet", address="192.0.2.20"
        )

        route = self.router(wsd).prepare(configured, self.request)

        self.assertEqual(route.protocol, "legacy")
        self.assertEqual(len(self.legacy_instances), 1)
        legacy = self.legacy_instances[0]
        self.assertEqual(legacy.address, "192.0.2.20")
        self.assertEqual(legacy.inspect_calls, 1)
        self.assertEqual(legacy.prepare_calls, 1)
        self.assertTrue(any("rejected protocol wsd" in line
                            for line in route.diagnostics))

    def test_auto_falls_back_after_wsd_prepare_failure(self):
        error = BackendError(
            BackendErrorCode.UNAVAILABLE, "capability probe failed",
            backend="sane-airscan-wsd", retryable=True,
        )
        wsd = FakeBackend(
            "sane-airscan-wsd", (wsd_scanner(),), prepare_error=error
        )
        configured = config.ConfiguredScanner(
            id="uuid:5de90400-1dd2-11b2-84bc-9c934e010299",
            name="HP OfficeJet", address="192.0.2.20",
        )

        route = self.router(wsd).prepare(configured, self.request)

        self.assertEqual(route.protocol, "legacy")
        self.assertEqual(len(self.legacy_instances), 1)

    def test_explicit_wsd_failure_never_builds_legacy_backend(self):
        wsd = FakeBackend("sane-airscan-wsd")
        configured = config.ConfiguredScanner(
            name="HP LaserJet", address="192.0.2.20", protocol="wsd"
        )

        with self.assertRaisesRegex(routing.RoutingError, "WSD"):
            self.router(wsd).prepare(configured, self.request)
        self.assertEqual(self.legacy_instances, [])

    def test_explicit_legacy_does_not_discover_wsd(self):
        wsd = FakeBackend("sane-airscan-wsd")
        configured = config.ConfiguredScanner(
            name="HP LaserJet", address="192.0.2.20", protocol="legacy"
        )

        route = self.router(wsd).prepare(configured, self.request)

        self.assertEqual(route.protocol, "legacy")
        self.assertEqual(wsd.discover_calls, 0)

    def test_known_non_hp_wsd_device_does_not_try_hplip(self):
        wsd = FakeBackend("sane-airscan-wsd")
        configured = config.ConfiguredScanner(
            name="Xerox WorkCentre", address="192.0.2.52"
        )

        with self.assertRaisesRegex(routing.RoutingError, "not supported"):
            self.router(
                wsd, legacy_support=hplip.supports_configured
            ).prepare(configured, self.request)
        self.assertEqual(self.legacy_instances, [])

    def test_native_fails_before_discovery_or_guest_work(self):
        wsd = FakeBackend("sane-airscan-wsd")
        configured = config.ConfiguredScanner(
            name="Office scanner", address="192.0.2.20", protocol="native"
        )

        with self.assertRaisesRegex(routing.RoutingError, "not available yet"):
            self.router(wsd).prepare(configured, self.request)
        self.assertEqual(wsd.discover_calls, 0)
        self.assertEqual(self.legacy_instances, [])

    def test_scan_failure_cannot_trigger_cross_protocol_retry(self):
        acquisition_error = BackendError(
            BackendErrorCode.IO, "paper moved, then transport failed",
            backend="sane-airscan-wsd", retryable=True,
        )
        job = FakeJob(acquisition_error)
        wsd = FakeBackend("sane-airscan-wsd", (wsd_scanner(),), job=job)
        configured = config.ConfiguredScanner(
            id="uuid:5de90400-1dd2-11b2-84bc-9c934e010299",
            name="HP OfficeJet", address="192.0.2.20",
        )
        route = self.router(wsd).prepare(configured, self.request)

        with self.assertRaisesRegex(BackendError, "paper moved"):
            route.job.scan()

        self.assertEqual(job.scan_calls, 1)
        self.assertEqual(self.legacy_instances, [])


class HPLIPEligibilityTests(unittest.TestCase):
    def test_vendor_rule_stays_inside_hplip_backend(self):
        self.assertTrue(hplip.supports_configured(
            config.ConfiguredScanner(name="HP OfficeJet", host="hp.local")
        ))
        self.assertTrue(hplip.supports_configured(
            config.ConfiguredScanner(host="old-config.local")
        ))
        self.assertFalse(hplip.supports_configured(
            config.ConfiguredScanner(name="Xerox WorkCentre", host="xerox.local")
        ))


if __name__ == "__main__":
    unittest.main()
