# scanbox

**Scanning again from an older HP MFP that macOS dropped support for** — HP LaserJet /
OfficeJet all-in-ones that still print fine over AirPrint but whose scanner vanished
somewhere around macOS Ventura. Runs HPLIP in an on-demand Debian VM and gives you a
`scanner` command.

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
scanner find                   # discover the scanner, then save it as shown
```

Or without Homebrew:

```sh
brew install lima          # the one prerequisite; scanner reports it, never installs it
git clone https://github.com/vincentcr/scanbox && cd scanbox
./bin/install              # symlinks scanner into ~/.local/bin
scanner find
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
edge** — feed two letter pages and a legal one and you get a PDF with two letter pages
and a legal page. Every page is kept, blanks included.

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
nothing. All Bonjour work is done by macOS (`lib/discover.sh`) and only a plain IPv4
address is passed in. `_scanner._tcp` is the right service type — close to exactly the
set `hpaio` can drive, and its TXT record carries the model and `feeder=T`/`flatbed=T`.

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

**5. File format is not where quality is lost.** Output is lossless (`/FlateDecode`)
and smaller than TIFF. The scanner JPEG-compresses *in transit* before the data ever
reaches us — the only lossy step, which no output format recovers. Use `--lossless`
when it matters.

## Host portability notes

`scanner` runs on macOS, which ships **bash 3.2** and none of `flock`, `setsid`, or
`timeout`. So: no `mapfile` or associative arrays, `mkdir` as the mutex, `nohup` for
detaching, and `perl` for timeouts — and perl must stay parent and *reap* its child,
because an exec'd process killed by SIGALRM makes the shell print `Alarm clock: 14` and
loses the exit status besides.

## VM lifecycle

`scanner` owns it entirely. There is no launchd agent — a permanently registered
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
