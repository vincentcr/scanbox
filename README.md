# scanbox

**Scanning again from an older HP MFP that macOS dropped support for** — HP LaserJet /
OfficeJet all-in-ones that still print fine over AirPrint but whose scanner vanished
somewhere around macOS Ventura. Runs HPLIP in an on-demand Debian VM and gives you a
`scanbox` command.

Developed against an **HP LaserJet Pro 200 color MFP M276nw** (2013) on Apple Silicon.

## Does this apply to you?

Lots of people hit "my HP scanner stopped working on macOS", and **most of them have an
easier fix than this**: newer printers support **eSCL** (AirScan) and merely ship it
disabled. Browse to your printer's IP, look for eSCL or AirScan under Network or Scan,
turn it on, and macOS will find it in Image Capture. Try that first.

This repo is for the harder case: printers that have **no eSCL at all**. They predate
the standard and speak only HP's proprietary SOAP scan protocol (TCP 8289) and WSD.
There is no setting to enable, HP has stopped updating the firmware, and macOS no
longer ships vendor scanner drivers. Printing keeps working because AirPrint covers
printing only.

You are probably in this case if:

- the printer is roughly 2009–2015 vintage,
- its embedded web server has no eSCL/AirScan option anywhere,
- it prints fine but no longer appears as a scanner,
- and `dns-sd -B _uscan._tcp` shows nothing for it, while `dns-sd -B _scanner._tcp` does.

HPLIP's `hpaio` backend still implements the old protocol. Linux can therefore still
talk to these printers perfectly well — so this runs a small Debian VM and drives it
from a CLI.

```
 macOS ── scanner ──▶ Debian VM ── HPLIP/hpaio ── HP SOAP:8289 ──▶ printer
          (CLI)       created and started on demand,
                      stopped again when idle
```

The VM is a **runtime component, not an installation**: created on first use, started
when needed, stopped after 60 idle minutes. Scanning here happens a few times a year
and nothing should be running in between. Cold start to a finished scan is ~21s.

## Scope, honestly

Developed and tested against **one printer, on one Mac, on Apple Silicon**. It is built
from generic pieces — HPLIP, SANE, Bonjour discovery, no model hardcoded in the scan
path — so other pre-eSCL HP MFPs have a good chance of working. But "good chance" is
the honest claim; it has not been tried on a second device. Reports welcome.

Intel Macs should work (Lima supports both) but are likewise untested.

## Install

```sh
brew tap vincentcr/scanbox
brew trust vincentcr/scanbox   # Homebrew 6+ gates third-party taps
brew install scanbox           # pulls in lima
scanbox setup                  # find your scanner and save it
```

Or without Homebrew:

```sh
brew install lima          # the one prerequisite; scanbox reports it, never installs it
git clone https://github.com/vincentcr/scanbox && cd scanbox
./bin/install              # symlinks scanbox into ~/.local/bin
scanbox setup
```

`scanbox setup` lists the scanners it finds and writes your choice to
`~/.config/scanbox/config`. It confirms before replacing an existing config, and
nothing is written until the whole flow completes. `--host=NAME` skips discovery;
`--overwrite` skips the confirmation.

## Use

```sh
scanbox scan                   # feeder if loaded, else the bed
scanbox scan feeder            # force the document feeder
scanbox scan bed               # force the flatbed
scanbox scanners               # list usable scanners on this LAN
scanbox scan --scanner auto    # use this LAN, not the configured default
scanbox setup                  # find a scanner and save it as your config
scanbox status                 # VM state, config, resolved address
scanbox stop                   # stop the VM now
```

Running `scanbox` with no arguments prints help. Scanning moves paper, so it needs
the explicit `scan` verb rather than being the default action.

Scans land in `~/Pictures/Scans`, named `scan-YYYYMMDDHHMMSS` followed by the
chosen extension (and a page number when a scan is split).

| option | |
|---|---|
| `--out DIR` | where scans land (default `~/Pictures/Scans`) |
| `--name NAME` | base filename |
| `--dpi N` | 75–1200, default 300 for PDF or 600 with `--image` |
| `--mode M` | `Color`\|`Gray`\|`Lineart` |
| `--page P` | `auto`\|`letter`\|`legal`\|`a4`\|`max` |
| `--image` | save image output instead of PDF; the format is chosen automatically |
| `--split` | save one output file per page instead of joining the pages |
| `--format F` | explicitly choose `pdf`\|`png`\|`tiff`\|`jpeg`; cannot be combined with `--image` |
| `--lossless` | disable the scanner's in-transit JPEG compression (slow — see below) |
| `--keep-alive MIN` | idle minutes before the VM stops (default 60) |
| `--scanner NAME` | temporarily use a current-LAN scanner by exact name or stable ID; `auto` uses the only match or prompts |
| `--printer HOST` | legacy compatibility override for the configured HP path |

`--scanner` is deliberately temporary: discovery and selection never read,
replace, or otherwise edit the configured default. If several usable scanners
are present, an interactive command prompts; a noninteractive command lists the
candidates and fails rather than guessing. `--scanner` and the legacy
`--printer` override are mutually exclusive.

A feeder run normally collates into one PDF. `--split` writes one PDF per page
instead. In either case, **each sheet is sized from its own trailing edge** — feed
two letter pages and a legal one and you get pages with the corresponding sizes.
Every page is kept, blanks included.

Config lives at `~/.config/scanbox/config` (see `config.example`) and is not committed.

## The non-obvious parts

Each of these cost real debugging. They are the reason this repo exists at all, more
than the code is.

**1. Scanning needs HP's closed-source plugin, and HPLIP's own metadata misleads.**
`models.dat` reports `plugin=0` for this model, which reads as "no plugin needed" —
that flag covers *printing* only. Scanning is `scan-type=5` (SOAPHT), implemented in
`bb_soapht.so`, which ships only in HP's proprietary plugin. Without it `scanimage`
fails with a bare `Error during device I/O`, and the real cause appears only in the
journal:

```sh
limactl shell scanbox -- sudo journalctl | grep scanimage
```

HP's own CDN 403s on the plugin download; `provision/20-plugin.sh` uses the
OpenPrinting mirror. `bb_soapht-arm64.so` exists, so Apple Silicon needs no emulated
x86 VM.

`hp-plugin`'s exit status is also unusable: `yes` takes SIGPIPE when it exits, so the
pipeline reports 141 under `set -o pipefail` *even on success*. The installed `.so` is
the only trustworthy signal.

**2. Discovery happens on the host, never in the guest.**
Multicast does not cross Lima's vzNAT boundary, so `scanimage -L` inside the VM finds
nothing. Legacy HP setup does its Bonjour work in macOS (`scanbox/discover.py`) and
passes only a plain IPv4 address into HPLIP. Dynamic current-LAN discovery sends
WS-Discovery probes from macOS and passes the chosen device's explicit endpoint into
the WSD guest backend; this is why the WSD-only Xerox works even though multicast
cannot reach the VM.

The current-network catalog only offers advertisements for which scanbox has a usable
backend. `scanbox scanners` and `scanbox scan --scanner auto` do not start the VM;
the selected WSD backend starts it later, while inspecting capabilities before the
actual scan. Native ImageCapture/eSCL candidates will join the same catalog when that
acquisition backend is available.

This is also why the printer is configured by **mDNS name** rather than IP: the name
derives from the printer's MAC, so it survives DHCP moving the address.

**3. The ADF cannot report page length — but the sheet edge is measurable.**
The feeder always runs its full 381mm (15") travel whatever you feed it, so raw scans
are 15" tall. Inferring size from where the *ink* stops is wrong: a letter sheet with
content only in its top half would come out A5.

So `lib/autofit.sh` measures the sheet instead. Past the trailing edge the scanner
images its own backing, which returns a **perfectly constant 254** (65278 at 16-bit)
while paper reads 255 or textured. The physical edge is where that constant run
begins — independent of content, and correct for a blank legal page.

Two thresholds matter. Paper white sits only **257 above** the backing at 16-bit — one
8-bit level — so a loose tolerance reads the whole sheet as backing. And the snap
tolerance must be *proportional*, not fixed: the feeder grips differently on each pass,
and one legal sheet measured anywhere from 13.77" to 14.12" across runs.

ADF only. The flatbed has no backing to measure against, since the lid is the same
white as paper.

**4. A short feeder batch must never look like a clean scan.**
A batch has exactly one healthy ending — the feeder reporting it is out of documents.
A jam or mis-feed partway leaves a batch that stopped early, which is otherwise
indistinguishable from success and quietly yields a short PDF. That is how a page goes
missing without anyone noticing, so anything else is flagged loudly.

(A *double-feed*, where several sheets are pulled through as one, stays undetectable in
software — the page count and sheet count simply disagree. Compare against what you
loaded.)

**5. File format is not where quality is lost.** The default PDF output is lossless
(`/FlateDecode`) and smaller than TIFF. The scanner JPEG-compresses *in transit*
before the data ever reaches us — the only lossy step, which no output format
recovers. Use `--lossless` when it matters.

The default output is a PDF, joined into one file unless `--split` asks for one PDF
per page. `--image` instead chooses an image format from the job itself:

- a joined feeder scan becomes a multi-page TIFF;
- a flatbed or split lossless scan becomes PNG;
- a flatbed or split Lineart scan also becomes PNG, because JPEG is a poor fit
  for hard one-bit edges;
- every other flatbed or split scan becomes JPEG.

With the default `auto` source, this is based on the source actually used: the same
command produces TIFF when paper is in the feeder and JPEG or PNG when it falls back
to the bed. `--image` defaults to 600 dpi and Color; `--dpi` and `--mode` always
override those defaults.

`--format` remains available when the exact format matters. PDF and TIFF join pages
unless `--split` is present. PNG and JPEG are inherently one file per page. PNG
skips assembly and saves the scanned pages as-is, so it is the only path with no
output re-encoding. Combining explicit `--format jpeg` with `--lossless` is
self-defeating (a warning says so): it pays for an uncompressed transfer only to
re-compress the result on disk.

It is not free. Without the in-transit JPEG the whole raster crosses the network
uncompressed, and this printer's SOAP transfer measures about **550 KB/s**:

| | bytes on the wire | time per page |
|---|---|---|
| `--lossless --dpi 300` | 27 MB | ~50 s |
| `--lossless --dpi 600` | 107 MB | ~3 min |
| `--lossless --dpi 1200` | 430 MB | ~13 min |

A 1200 dpi lossless page really does take a quarter of an hour, so `scan` prints the
estimate up front and then reports a live percentage and ETA. Cancelling is safe —
Ctrl-C stops the scan inside the VM as well as on the host. Note that the printer
holds its one scan session for roughly **45 seconds** after an aborted scan; `scan`
retries through that window rather than failing.

## Host portability notes

The host side is Python — standard library only, no virtualenv, nothing to install
beyond an interpreter. It targets **3.9**, which is what the macOS Command Line Tools
ship, and Homebrew requires those anyway, so a Mac that can install this can already
run it. Output assembly uses macOS's built-in `sips`, `tiffutil`, and Combine PDF
Pages action; it does not add a Python imaging dependency.

Everything that runs *inside* the VM stays shell (`lib/guest-scan.sh`, `lib/autofit.sh`,
`provision/`). Those are piped in over stdin, so the VM never learns where this repo
lives — and there is no reason to give a Debian guest a Python dependency.

The lock is the one piece that did not change: `mkdir` is still the mutex, because
macOS has no `flock` and this has to work *between processes*. Two `scanbox scan`
invocations are two processes, so an in-process lock would not be in the conversation.

The host was bash until v0.5.0, which meant working around macOS's bash 3.2 and its
missing `flock`/`setsid`/`timeout` — `nohup` for detaching, `perl` for timeouts, and no
`mapfile` or associative arrays. None of that survives; `git log` has it if you are
curious.

## VM lifecycle

`scanbox` owns it entirely. There is no launchd agent — a permanently registered
background job is the thing this design avoids. After each scan it arms one detached
timer, guarded by a pidfile, which stops the VM once idle.

```sh
limactl shell scanbox        # poke around inside
limactl delete scanbox       # start over; the next scan rebuilds it
```

First ever run takes several minutes: it downloads the Debian image and the HP plugin.

## Prior art

- [AirSane](https://github.com/SimulPiscator/AirSane) — publishes SANE scanners over
  eSCL. Excellent, but assumes you already have a machine running SANE.
- [node-hp-scan-to](https://github.com/manuc66/node-hp-scan-to) — reimplements HP's
  "scan to computer" for a different subset of printers.
- [sane-airscan](https://github.com/alexpevzner/sane-airscan) — eSCL and WSD client for
  Linux. Its WSD support is a plausible alternative path for these printers.

An earlier version of this repo bridged the scanner to Preview and Image Capture via
AirSane, so it appeared as a native macOS scanner. That was dropped in favour of a
CLI-only design, since keeping it meant keeping a VM and a Bonjour advertiser running
permanently for a few minutes of use a year. To get it back, branch from `ea3d852`.

## License

MIT
