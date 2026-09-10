# scanbox

`scanbox` is a macOS command-line scanner for network MFPs that the Mac cannot
scan from on its own. It discovers the scanner on the Mac, acquires pages through
a small on-demand Debian VM, and saves ordinary PDFs or images back on macOS.

It currently has two acquisition paths:

- **WSD**, through `sane-airscan`. This is vendor-neutral and is tested with a
  Xerox WorkCentre 6605DN.
- **Legacy HP**, through HPLIP's `hpaio` backend and HP's scan plugin. This path
  was built for an HP LaserJet Pro 200 color MFP M276nw.

Both paths use the VM. Native Image Capture/AirScan acquisition is future work,
not a current feature.

## Is this for my scanner?

Try the scanner's simple path first. If it offers eSCL or AirScan in its web
interface, enable that and check Image Capture. You do not need scanbox when
macOS already sees the scanner.

scanbox is useful today if one of these is true:

- the scanner advertises a WSD scan service but macOS does not expose it; or
- it is an older networked HP MFP supported by HPLIP, but no longer by macOS.

Current limitations:

- network scanners only; USB is not supported;
- WSD and the legacy HP protocol only—eSCL is not yet an acquisition backend;
- the current VM configuration targets Apple Silicon macOS;
- WSD discovery requires the Mac and scanner to be on the same multicast-capable
  LAN;
- arbitrary proprietary legacy protocols need their own backend adapter.

The quickest check after installation is read-only:

```sh
scanbox scanners
```

This lists WSD scanners and HP scanners advertised for HPLIP on the current
LAN. It does not start the VM or move paper.

## Install

With Homebrew:

```sh
brew tap vincentcr/scanbox
brew trust vincentcr/scanbox
brew install scanbox
```

Or from a checkout:

```sh
brew install lima
git clone https://github.com/vincentcr/scanbox
cd scanbox
./bin/install
```

scanbox needs Python 3.9 or newer and Lima. It has no Python package
dependencies. The first scan creates the VM and downloads its Debian image;
allow several minutes. Later runs start it on demand and stop it after the idle
timeout.

## Choose a scanner

Discover the current network and save a scanner you use regularly:

```sh
scanbox scanners
scanbox scanners --save
```

Discovery runs WSD and Bonjour/HPLIP searches through the same catalog used by
temporary scans. `--save` asks which physical scanner to remember, checks its
available backends without acquiring a page, and stores the backend that
worked. Repeated saves add or update entries in
`~/.config/scanbox/scanners.json`; they do not replace scanners from another
network.

Select by exact displayed name or stable ID, or make the saved scanner the
preferred tie-breaker:

```sh
scanbox scanners --save "Xerox WorkCentre 6605DN"
scanbox scanners --save "HP LaserJet" --preferred
```

For a device that does not advertise itself, save an explicit locator and
backend:

```sh
scanbox scanners --save "Basement HP" --host hp.local --backend hplip
```

Inspect and manage the registry without touching the network:

```sh
scanbox scanners --saved
scanbox scanners --prefer "Xerox WorkCentre 6605DN"
scanbox scanners --forget "Basement HP"
```

To use a scanner on the LAN temporarily, without reading or changing the saved
configuration:

```sh
scanbox scanners
scanbox scan --scanner auto
```

With one physical match, `auto` selects it. With several, an interactive
terminal asks; scripts fail with the candidate list instead of guessing. Plain
`scanbox scan` tries the preferred saved scanner first, then the remaining
saved scanners until one can prepare a scan. This lets home and office scanners
coexist without tying configuration to an SSID.

## Scan

```sh
scanbox scan                   # feeder when loaded, otherwise flatbed
scanbox scan feeder            # require the document feeder
scanbox scan bed               # require the flatbed
scanbox scan bed --dpi 600 --mode Gray
```

Scans go to `~/Pictures/Scans` and are named
`scan-YYYYMMDDHHMMSS.<extension>` by default. Common controls:

| Option | Meaning |
|---|---|
| `--out DIR` | Change the output directory |
| `--name NAME` | Change the base filename |
| `--dpi N` | Set resolution; defaults to 300, or 600 with `--image` |
| `--mode M` | Select `Color`, `Gray`, or `Lineart`; defaults to `Color` |
| `--page P` | Select `auto`, `letter`, `legal`, `a4`, or `max` |
| `--image` | Use the smart image-format rules below instead of PDF |
| `--split` | Save one output file per page |
| `--format F` | Force `pdf`, `png`, `tiff`, or `jpeg` |
| `--lossless` | Request uncompressed scanner transport when supported |
| `--keep-alive MIN` | Set VM idle time after the scan; defaults to 60 minutes |

`--image` and `--format` are mutually exclusive. `--dpi` and `--mode` always
override their defaults.

### Output defaults

Without an output option, scanbox creates PDF. Feeder pages are joined into one
file unless `--split` is present.

`--image` chooses a format after scanbox knows whether `auto` used the feeder or
the bed:

| Result | Format |
|---|---|
| Feeder, pages kept together | Multi-page TIFF |
| Flatbed or split pages, lossless or line art | PNG |
| Other flatbed or split pages | JPEG |

Examples:

```sh
scanbox scan feeder --image                 # one multi-page TIFF
scanbox scan feeder --image --split         # one JPEG per page
scanbox scan feeder --image --split --mode Lineart  # one PNG per page
scanbox scan bed --image                    # JPEG
scanbox scan bed --image --lossless         # PNG, when supported
scanbox scan feeder --split                 # one PDF per page
scanbox scan bed --format tiff              # explicitly choose TIFF
```

`--lossless` controls transfer from the scanner, not merely the final filename.
It can be dramatically slower and only works when the selected backend exposes
an uncompressed mode. The legacy HP path does; the current WSD path does not.
Choosing PNG by itself cannot recover detail already lost during transport.

The HP M276 feeder cannot report sheet length. On that legacy path, `--page auto`
measures and crops each fed sheet independently. This is an HP-specific backend
accommodation, not a promise made for every WSD scanner.

## Backend selection

Discovery groups WSD and Bonjour advertisements only when they carry the same
strong UUID or serial identity. During `scanners --save`, scanbox checks WSD
first and falls back to HPLIP while acquisition is still read-only. The
successful concrete backend is stored with that scanner, avoiding repeated
probing on later scans.

It never switches protocols after acquisition begins: once paper might have
moved, an error is reported rather than risking a duplicate or incomplete scan.

You can override backend selection for one scan:

```sh
scanbox scan --backend wsd
scanbox scan --backend hplip
```

`--backend imagecapture` is reserved and currently reports that ImageCapture
acquisition is unavailable.

Provisioning is also selected lazily. WSD installs only core SANE tools and
`sane-airscan`; it does not install HPLIP or HP's proprietary plugin. The HPLIP
path installs its own dependencies only when selected.

## Status and troubleshooting

```sh
scanbox status
scanbox stop
```

If discovery finds nothing:

- confirm the scanner and Mac are on the same LAN, without client isolation;
- wake the scanner and check that WSD scanning is enabled;
- for an HP scanner that does not advertise itself, try
  `scanbox scanners --save --host HOST --backend hplip`;
- use `scanbox scanners --saved` to inspect configuration without discovery;
- remember that eSCL-only devices are not yet supported.

If VM creation or provisioning fails, the detailed Lima log is
`~/.local/state/scanbox/lima.log`. `scanbox stop` is safe and the next scan starts
the VM again. To discard only scanbox's managed VM and rebuild it on the next
scan:

```sh
limactl delete scanbox
```

Ctrl-C cancels the remote acquisition as well as the local command. Some legacy
HP devices retain their single scan session briefly after cancellation; scanbox
recognizes that busy response and retries it.

## Development and verification

```sh
python3 -m unittest discover -v
```

The automated suite covers both backend contracts, routing, discovery,
selection, saved-scanner configuration, output assembly, guest protocols, and lazy
provisioning. WSD discovery, flatbed scanning, and a complete three-page feeder
batch have been exercised on the Xerox. The refactored HPLIP path has automated
regression coverage, but still awaits a repeat physical run against the HP on
its home LAN.

Implementation details and the hardware verification record live in
[`scanbox/backends/README.md`](scanbox/backends/README.md). The unfinished native
ImageCapture experiment is documented in [`native/README.md`](native/README.md).

## License

MIT
