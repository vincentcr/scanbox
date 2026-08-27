# scanbox

Gives the **HP LaserJet Pro 200 color MFP M276nw** (2013) its scanner back on modern
macOS — as a native scanner in Preview / Image Capture, with no client software.

## Why this exists

The M276nw predates **eSCL / AirScan**, Apple's driverless scan protocol. It speaks
only HP's proprietary SOAP scan protocol (TCP 8289) and WSD. Its AirPrint support
covers *printing only* — which is why printing still works and scanning doesn't.
macOS scanning relied on HP's bundled ICA driver, and Apple no longer ships vendor
scanner drivers. HP stopped updating the firmware, so there is no fix printer-side.

HPLIP's `hpaio` SANE backend still implements that protocol. So: run HPLIP in a
Debian VM, and republish the scanner over eSCL with AirSane.

```
 macOS  ──eSCL/AirScan──▶ AirSane ──SANE──▶ hpaio ──HP SOAP:8289──▶  M276nw
  Preview,                └────── Debian VM (lima) ──────┘           192.168.86.22
  Image Capture              192.168.64.2:8090
```

Bonjour discovery works across Lima's vzNAT, so the Mac finds the scanner on its
own — no bridged networking or `socket_vmnet` required.

## Layout

| path | what |
|---|---|
| `scanbox.yaml` | Lima VM definition (Debian 13 arm64, vzNAT, project mounted) |
| `printer.env` | printer IP / model — the only file you should need to edit |
| `provision/` | idempotent setup steps, run inside the VM, in numeric order |
| `bin/setup` | applies all provisioning; safe to re-run |
| `bin/scan` | scan a page onto the Mac from the command line |
| `scans/` | output lands here (shared with the VM via virtiofs) |

## Setup

```sh
brew install lima
limactl start --name=scanbox ./scanbox.yaml
./bin/setup
```

## Scanning

From macOS, the scanner shows up by itself in **Preview → File → Import from
Scanner**, or **Image Capture**. Nothing to install.

From the command line:

```sh
./bin/scan                                    # one flatbed page -> 300dpi PNG
./bin/scan invoice --resolution 600
./bin/scan contract --source ADF              # feeder -> one multi-page PDF
./bin/scan deed --source ADF --page legal     # pin the size instead of detecting
./bin/scan deed --source ADF --lossless       # turn off the scanner's own JPEG
```

`--source ADF` keeps pulling sheets until the feeder reports empty, and collates the
run into a single PDF by default (`--format png` gives numbered pages instead).

Page size is **detected per sheet**, so a mixed stack works — feed two letter pages
and a legal one and you get a PDF with two letter pages and a legal page:

```
  p0001: letter (measured 10.94in)
  p0002: letter (measured 10.91in)
  p0003: legal  (measured 14.11in)
```

## The three non-obvious things

These each cost real debugging time; they are why the provision scripts look the
way they do.

**1. Scanning needs HP's closed-source plugin, and the obvious signal lies.**
`models.dat` reports `plugin=0` for this model, which suggests no plugin is needed —
that flag only covers *printing*. Scanning uses `scan-type=5` (SOAPHT), implemented
in `bb_soapht.so`, which ships only in HP's proprietary plugin. Without it
`scanimage` fails with a bare `Error during device I/O`; the real reason
(`unable to load library .../bb_soapht.so`) appears only in the journal:

```sh
limactl shell scanbox -- sudo journalctl -u airsaned -n50
limactl shell scanbox -- sudo journalctl | grep scanimage
```

HP's own CDN 403s on the plugin download; `provision/20-hplip.sh` pulls it from the
OpenPrinting mirror. `bb_soapht-arm64.so` does exist, so an Apple Silicon VM is fine —
no emulated x86 needed.

**2. The CUPS queue is load-bearing for scanning.**
`provision/25-cups.sh` registers a print queue not because we want to print, but
because HPLIP discovers network devices by SLP/mDNS broadcast, which cannot cross
the VM's NAT boundary — so `scanimage -L` finds nothing. HPLIP *also* derives devices
from configured `hp:` CUPS queues, over unicast. AirSane publishes whatever SANE
enumerates, so without the queue it publishes nothing. Direct `scanimage -d <uri>`
works either way; only enumeration needs this.

It uses `lpadmin` with the checked-in PPD rather than `hp-setup`, which insists on
printing a physical test page.

**3. The ADF cannot report page length — but the sheet edge is measurable.**
The feeder always runs its full 381mm (15") travel whatever you feed it, and exposes
no page-length sensor through SANE, so every scan arrives 15" tall.

Inferring size from where the *ink* stops is wrong: a letter sheet with content only
in its top half would come out A5. The fix is to measure the sheet itself. Past the
trailing edge the scanner images its own backing, which comes back as a **perfectly
constant 254** (65278 at 16-bit), while paper reads 255 or textured. So the physical
edge is exactly where that constant run begins — independent of content, and correct
for a blank legal page.

`lib/autofit.sh` walks up from the bottom of the bed to find it, then snaps to a
standard size when the measurement lands within ~4mm of one, and reports the measured
length otherwise rather than inventing a size.

Two thresholds matter, and both were found the hard way:
- Paper white (65535) sits only **257 above** the backing (65278) — one 8-bit level.
  A tolerance of 400 swallows the sheet entirely and reads it as backing.
- A single noisy row must not end the walk, so an edge requires a run of 5
  consecutive non-backing rows.

This is ADF-only. The flatbed has no backing to measure against (the lid is white
like paper), so `--page auto` leaves flatbed scans at full size.

**4. Don't let Lima mount `~`.**
`scanbox.yaml` deliberately omits `template:_default/mounts`. That base mounts the
home directory read-only, which shadows the writable project mount, and scans fail
with `could not open output file`.

Also worth knowing: `/lib/aarch64-linux-gnu/libm.so` is a GNU *ld script*, not an ELF
object, so HPLIP logs `unable to load library libm.so: invalid ELF header` on every
run. It is a red herring — harmless, and unrelated to scanning.

## On file formats

Not worth switching to TIFF. Same page, same source scan:

| format | size | notes |
|---|---|---|
| PNG (scan output) | 3.2M | lossless |
| TIFF LZW | 2.9M | lossless |
| TIFF Zip | 2.7M | lossless |
| **PDF (what we emit)** | **2.6M** | lossless `/FlateDecode`, multi-page |
| TIFF G4 | 44K | bilevel only — kills colour and shading |

The PDF is already lossless Flate *and* the smallest of the lossless options, and it
is multi-page, so TIFF buys nothing here. TIFF G4 is dramatic (70x smaller) but it is
1-bit black and white — fine for pure text, wrong for anything with a logo, photo, or
grey panel.

The real quality lever is not the file format at all: **the scanner JPEG-compresses
before the data reaches us** (`--compression [JPEG]` is its default). No output format
recovers that. Use `--lossless` when it matters; it costs scan time and file size.

## Operating notes

- **The printer IP must be stable.** DHCP moving it breaks the device URI. Reserve it
  on the router, or point `printer.env` at `PRINTER_HOST` instead.
- The VM must be running to scan: `limactl start scanbox`. To have it come up at
  login, `limactl start` it from a LaunchAgent.
- Feeder scans run about 7s/page at 300dpi colour; the flatbed is ~7s. `--page auto`
  always scans the full 15" bed, so it costs the same regardless of sheet size.
- Sheets fed upside-down scan upside-down; there is no auto-rotation.
- AirSane's web UI is at <http://192.168.64.2:8090/> — useful for testing without macOS.

```sh
limactl stop scanbox
limactl shell scanbox          # poke around inside
limactl delete scanbox         # start over; ./bin/setup rebuilds it
```
