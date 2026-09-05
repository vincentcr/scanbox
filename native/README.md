# Native scanner spike

This directory contains the macOS `ImageCaptureCore` feasibility spike. It is
not wired into the production `scanbox` command yet.

Build it with the Command Line Tools already required by scanbox:

```sh
swiftc -framework ImageCaptureCore \
  native/scanbox-native.swift -o /tmp/scanbox-native
```

Discover scanners visible to macOS and emit machine-readable JSON:

```sh
/tmp/scanbox-native discover
/tmp/scanbox-native discover --timeout 10
```

Discovery runs for a fixed interval because `ICDeviceBrowser` reports network
devices asynchronously and exposes no callback for "network enumeration is
complete." For every discovered scanner, the helper opens a session and reports
each source's current pixel mode, supported bit depths, supported/preferred
resolutions, and native resolution. Session or source-selection failures appear
as `inspectionError` on that scanner instead of making discovery fail. No VM is
created or started by this helper.

ImageCaptureCore exposes the current `pixelDataType`, but it does not expose a
set of supported pixel data types. Consequently, `currentMode` is useful as a
default/capability hint but is not a complete list of color, grayscale, and
line-art modes. The supported bit-depth list is reported separately.

## Xerox WorkCentre 6605DN result

Tested on 2026-09-05 against a Xerox WorkCentre 6605DN on the local network:

- `ICDeviceBrowser` returned zero scanners after repeated 5- and 15-second
  discovery windows.
- The printer advertised `_ipp._tcp` with `Scan=T` but `air=none` and did not
  advertise `_uscan._tcp`, `_uscans._tcp`, or `_scanner._tcp`.
- While awake, its eSCL capability endpoint returned HTTP 404 over both its
  web and IPP services.
- WS-Discovery identified the device as both a printer and a scanner and
  returned a usable WSD endpoint.

The result is definitive for the native path: this Xerox is not presented to
ImageCaptureCore and therefore cannot validate or use the proposed native
backend as currently configured. A positive ImageCaptureCore capability test
still requires a scanner that macOS shows in Image Capture.

As a separate follow-up, `sane-airscan` 0.99.35 was installed manually in the
existing scanbox VM and configured with the Xerox's explicit WSD endpoint.
Explicit configuration is necessary because WS-Discovery multicast does not
cross Lima's `vzNAT` boundary. `scanimage -L` then found the device, and test
scans succeeded from both sources at 200 dpi grayscale:

- ADF: 1700 x 2800, 8-bit grayscale PNG
- Flatbed: 1700 x 2339, 8-bit grayscale PNG

This proves the Xerox is usable through a VM-hosted WSD backend despite not
supporting Apple's eSCL/AirScan protocol. The temporary scan files, package,
and endpoint configuration were removed from the guest after the test; project
provisioning does not yet reproduce them.
