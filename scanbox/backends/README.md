# Scanner backends

Backends implement the normalized contracts in `scanbox.contracts`.
`scanbox.output` owns backend-neutral smart format selection, naming,
host-side assembly, and staged-page cleanup. `scanbox.selection` builds the
current-network inventory by retaining only advertisements paired with a
usable backend, and implements exact name/stable-ID and interactive selection.

`scanbox.routing` owns configured-device routing. It normalizes UUID and serial
spellings so advertisements from different protocols can be grouped without
using weak names or IP addresses as identity. In `auto` mode it prefers the WSD
backend, matches the saved stable identity before consulting a hostname/address,
and only considers HPLIP for an eligible HP device while preparation is still
read-only. The returned job is the boundary: errors from `ScanJob.scan()` are
never routed to another protocol. Route diagnostics name the accepted or
rejected backend directly.

## Legacy HP through HPLIP

`HPLIPBackend` encapsulates the existing configured-HP path. It owns
`hp-makeuri`, `hpaio` device construction, HPLIP capability inspection, Lima
lifecycle hooks, the guest scan protocol, stale-session handling, remote
cancellation, and copying acquired PNG pages back to the host. It is
constructed only when routing selects `hplip`; it does not advertise
candidates during host discovery.

The guest retains the HP M276-specific feeder trailing-edge measurement and
HPLIP compression and busy-session behavior. Those accommodations therefore
cannot leak into native ImageCapture or WSD acquisition.

## WSD through sane-airscan

`WSDBackend` performs WS-Discovery on the macOS host. A discovered scanner has
two deliberately different identifiers:

- `Scanner.id` is the device's stable WS-Addressing UUID and is safe to persist;
- `Scanner.endpoint` is the current HTTP WSD endpoint and is valid only for the
  current discovery result.

The guest is started lazily only after a WSD scanner is selected. Provisioning
tracks installed capabilities independently: this path requests only the core
SANE tools and `sane-airscan`, and never installs HPLIP or HP's proprietary
plugin. Every guest command receives
`SANE_AIRSCAN_DEVICE=wsd:scanbox-wsd:<host-discovered-endpoint>`. This both
disables guest discovery and forces WSD, even though sane-airscan also supports
eSCL.

`inspect()` maps SANE's Flatbed, ADF, ADF Duplex, Color, Gray, and resolution
options into shared capabilities. `prepare()` validates the normalized request
without moving paper. `ScanJob.scan()` is the sole acquisition boundary and
returns PNG pages staged in a host temporary directory. The output assembler
owns those staged pages and removes them after saving the requested PDF or image
output.

Automatic source selection tries a compatible feeder first. It falls back to
the flatbed only after an explicit empty-feeder response. An ambiguous error,
jam, or partially acquired batch stops in the WSD backend; protocol routing
must never retry through another backend after this boundary.

`WSDBackend.discover()` is currently the production source for the temporary
current-network catalog. It runs entirely on the host, without inspecting the
device or touching the VM. Only after the CLI selects a candidate does
`prepare()` ensure the WSD guest and inspect capabilities.

This path is vendor-neutral: it depends on a scanner advertising the WSD scan
service, not on its manufacturer. The Xerox used during development is one
known-good device, not a vendor restriction. Legacy protocols are different;
support for a non-HP legacy protocol requires its own backend adapter rather
than a shared-code vendor special case.

## Guest provisioning boundaries

`scanbox.vm` treats the guest runtime and installed software as separate
concerns. It probes four capabilities from actual guest state rather than
trusting one global marker: core SANE tools, sane-airscan, HPLIP/hpaio, and the
HP plugin. Dependencies are additive and idempotent, so a VM created by an
older scanbox remains usable and receives only a missing component.

WSD requests `core -> wsd`; HPLIP requests
`core -> hplip -> hp-plugin` and synchronizes its measurement helper. Each
provisioning failure names the component that failed. Host-side discovery has
no dependency on any of these capabilities and therefore cannot create or
start the VM.

## Verification

Run the full automated matrix from the repository root with:

```sh
python3 -m unittest discover -v
```

The matrix covers WS-Discovery parsing and identity grouping, ambiguous dynamic
selection, normalized capabilities, backend preference and safe pre-scan
fallback, configuration migration, output-format selection, both guest line
protocols, and capability-specific provisioning. The HPLIP guest test also
asserts that every auto-sized feeder page passes through its HP-specific
trailing-edge measurement.

Hardware verified during this refactor:

- Xerox WorkCentre 6605DN discovery and WSD flatbed acquisition;
- a complete three-sheet Xerox WSD feeder batch, including preservation of all
  three pages;
- lazy WSD preparation using only the core and sane-airscan capability plan.

A post-refactor 600 dpi color flatbed scan completed through HPLIP on the
configured HP M276, including guest-to-host copy and PDF assembly. HP feeder
auto-sizing is covered by its per-page guest regression test.

Native macOS ImageCapture acquisition is deferred backlog work. `imagecapture`
is a reserved backend value that reports unavailable before discovery or guest
startup; it is not a current backend.
