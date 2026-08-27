# scanner

Scan from an **HP LaserJet Pro 200 color MFP M276nw** (2013) on modern macOS.

## Why this exists

The M276nw predates **eSCL/AirScan**, Apple's driverless scan protocol. It speaks
only HP's proprietary SOAP scan protocol (TCP 8289) and WSD. Its AirPrint support
covers *printing only* — which is why printing still works and scanning doesn't.
macOS no longer ships vendor scanner drivers, and HP stopped updating the firmware,
so there is no fix printer-side.

HPLIP's `hpaio` backend still implements that protocol. So HPLIP runs in a Debian VM,
and `scanner` drives it.

The VM is a **runtime component, not an installation**. It is created on first use,
started when needed, and stopped again once idle — scanning here happens a few times
a year, and nothing should be running in between.

## Install

```sh
brew install lima          # prerequisite; scanner reports it, never installs it
git clone <this repo> && cd scanbox
./bin/install              # symlinks scanner into ~/.local/bin
scanner find               # discover the scanner, then save it as shown
```

`scanner find` prints the exact command to write your config.

## Use

```sh
scanner                        # scan whatever is loaded: feeder if present, else bed
scanner feeder                 # force the document feeder
scanner bed                    # force the flatbed
scanner find                   # discover scanners on the network
scanner status                 # VM state, config, resolved address
scanner stop                   # stop the VM now
```

PDFs land in `~/Pictures/Scans/scan-YYYYMMDDHHMMSS.pdf`.

| option | |
|---|---|
| `--out DIR` | where PDFs land (default `~/Pictures/Scans`) |
| `--name NAME` | base filename |
| `--dpi N` | 75–1200, default 300 |
| `--mode M` | `Color`\|`Gray`\|`Lineart` |
| `--page P` | `auto`\|`letter`\|`legal`\|`a4`\|`max` |
| `--lossless` | disable the scanner's in-transit JPEG compression |
| `--keep-alive MIN` | idle minutes before the VM stops (default 60) |
| `--printer HOST` | override the configured scanner for one run |

A feeder run collates into one PDF, with **each sheet sized from its own trailing
edge** — feed two letter pages and a legal one and you get a PDF with two letter
pages and a legal page. Every page is kept, blanks included.

Config lives at `~/.config/scanbox/config` (see `config.example`) and is not
committed — the address differs per machine and network.

## The non-obvious parts

Each of these cost real debugging; they are why the code looks the way it does.

**1. Scanning needs HP's closed-source plugin, and HPLIP's own metadata misleads.**
`models.dat` reports `plugin=0` for this model, which reads as "no plugin needed" —
that flag covers *printing* only. Scanning is `scan-type=5` (SOAPHT), implemented in
`bb_soapht.so`, which ships only in HP's proprietary plugin. Without it `scanimage`
fails with a bare `Error during device I/O`, and the real cause appears only in the
journal:

```sh
limactl shell scanbox -- sudo journalctl | grep scanimage
```

HP's own CDN 403s on the download; `provision/20-plugin.sh` uses the OpenPrinting
mirror. `bb_soapht-arm64.so` exists, so Apple Silicon needs no emulated x86 VM.

**2. Discovery happens on the host, never in the guest.**
Multicast does not cross Lima's vzNAT boundary, so `scanimage -L` inside the VM finds
nothing. All Bonjour work is done by macOS (`lib/discover.sh`) and only a plain IPv4
address is passed in. The `_scanner._tcp` service type is the right filter — it is
close to exactly the set `hpaio` can drive, and its TXT record carries the model and
`feeder=T`/`flatbed=T`.

This is also why the printer is configured by **mDNS name**, not IP: the name derives
from the printer's MAC, so it survives DHCP moving the address.

**3. The ADF cannot report page length — but the sheet edge is measurable.**
The feeder always runs its full 381mm (15") travel whatever you feed it, so raw scans
are 15" tall. Inferring size from where the *ink* stops is wrong: a letter sheet with
content only in its top half would come out A5.

So `lib/autofit.sh` measures the sheet instead. Past the trailing edge the scanner
images its own backing, which returns a **perfectly constant 254** (65278 at 16-bit)
while paper reads 255 or textured. The physical edge is where that constant run
begins — independent of content, and correct for a blank legal page.

Two thresholds matter: paper white sits only **257 above** the backing at 16-bit — one
8-bit level — so a loose tolerance reads the whole sheet as backing; and an edge
requires a run of 5 consecutive non-backing rows so one noisy row cannot end the walk.

ADF only. The flatbed has no backing to measure against, since the lid is the same
white as paper.

**4. File format is not where quality is lost.** Output is lossless
(`/FlateDecode`) and smaller than TIFF. The scanner JPEG-compresses *in transit*
before the data ever reaches us — that is the only lossy step, and no output format
recovers it. Use `--lossless` when it matters.

## Host portability notes

`scanner` runs on macOS, which ships **bash 3.2** and none of `flock`, `setsid`, or
`timeout`. So: no `mapfile` or associative arrays, `mkdir` as the mutex, `nohup` for
detaching, and `perl` for timeouts — and perl must stay as the parent and reap its
child, or the shell prints `Alarm clock: 14` and the exit status is lost.

## VM lifecycle

`scanner` owns it entirely. There is no launchd agent — a permanently registered
background job is the thing this design avoids. After each scan it arms one detached
timer, guarded by a pidfile, which stops the VM once idle.

```sh
limactl shell scanbox        # poke around inside
limactl delete scanbox       # start over; the next scan rebuilds it
```

Cold start is ~26s. First ever run is several minutes (downloads the Debian image and
the HP plugin).

## History

The `_scanner._tcp` bridge was once exposed to Preview and Image Capture via an
AirSane eSCL layer, so the printer appeared as a native macOS scanner. That was
removed in favour of a CLI-only design. To get it back, branch from `ea3d852`.
