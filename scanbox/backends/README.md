# Scanner backends

Backends implement the normalized contracts in `scanbox.contracts`. The
production CLI still uses the original HPLIP path while discovery, routing, and
output assembly are migrated in issues #2, #4, and #8.

## WSD through sane-airscan

`WSDBackend` performs WS-Discovery on the macOS host. A discovered scanner has
two deliberately different identifiers:

- `Scanner.id` is the device's stable WS-Addressing UUID and is safe to persist;
- `Scanner.endpoint` is the current HTTP WSD endpoint and is valid only for the
  current discovery result.

The guest is started and provisioned lazily with `sane-airscan` and
`sane-utils`; this path does not install HPLIP or HP's proprietary plugin. Every
guest command receives
`SANE_AIRSCAN_DEVICE=wsd:scanbox-wsd:<host-discovered-endpoint>`. This both
disables guest discovery and forces WSD, even though sane-airscan also supports
eSCL.

`inspect()` maps SANE's Flatbed, ADF, ADF Duplex, Color, Gray, and resolution
options into shared capabilities. `prepare()` validates the normalized request
without moving paper. `ScanJob.scan()` is the sole acquisition boundary and
returns PNG pages staged in a host temporary directory. The future output
assembler owns those staged pages and must remove their directory after it has
saved the requested PDF or image output.

Automatic source selection tries a compatible feeder first. It falls back to
the flatbed only after an explicit empty-feeder response. An ambiguous error,
jam, or partially acquired batch stops in the WSD backend; protocol routing
must never retry through another backend after this boundary.
