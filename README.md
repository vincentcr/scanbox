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

This lists usable WSD scanners on the current LAN. It does not start the VM or
move paper.

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

For a scanner you use regularly, save it once:

```sh
scanbox setup
```

Setup discovers Bonjour-advertised scanners and writes
`~/.config/scanbox/config`. If discovery does not list a known WSD scanner,
configure its hostname or fixed address directly:

```sh
scanbox setup --host office-scanner.local --backend hplip
scanbox setup --host 192.168.1.40 --backend wsd
```

Setup asks before replacing an existing configuration. Use `--overwrite` to
skip that confirmation. Existing configurations containing `PRINTER_HOST` or
`PRINTER_IP` are accepted and migrated automatically.

To use a scanner on the LAN temporarily, without reading or changing the saved
configuration:

```sh
scanbox scanners
scanbox scan --scanner auto
```

With one match, `auto` selects it. With several, an interactive terminal asks;
scripts fail with the candidate list instead of guessing. You can also pass the
exact displayed name or stable ID to `--scanner`.

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

Setup saves Bonjour `_scanner._tcp` discoveries as `hplip`, because that is the
HP discovery path backed by this project. A manual `--host` setup accepts
`--backend auto|wsd|hplip|imagecapture`; without it, the backend remains `auto`.

With `auto`, scanbox first looks for the same physical scanner over WSD,
matching its stable identity before its current hostname or address. For an
eligible HP device, it can fall back to HPLIP if WSD fails during discovery or
capability inspection.

It never switches protocols after acquisition begins: once paper might have
moved, an error is reported rather than risking a duplicate or incomplete scan.

You can override backend selection for one configured scan:

```sh
scanbox scan --backend wsd
scanbox scan --backend hplip
```

`--backend imagecapture` is reserved and currently reports that ImageCapture
acquisition is unavailable. `--printer HOST` is a shortcut for an explicit
HPLIP scan at that hostname or address.

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
- try `scanbox setup --host HOST_OR_IP` for a scanner you know;
- remember that `scanbox scanners` currently lists WSD, not eSCL-only, devices.

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
selection, configuration migration, output assembly, guest protocols, and lazy
provisioning. WSD discovery, flatbed scanning, and a complete three-page feeder
batch have been exercised on the Xerox. The refactored HPLIP path has automated
regression coverage, but still awaits a repeat physical run against the HP on
its home LAN.

Implementation details and the hardware verification record live in
[`scanbox/backends/README.md`](scanbox/backends/README.md). The unfinished native
ImageCapture experiment is documented in [`native/README.md`](native/README.md).

## License

MIT
