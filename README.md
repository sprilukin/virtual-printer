# Virtual Printer

A configurable virtual IPP printer for developing and testing print clients against printers you do not own. It speaks IPP/IPPS, advertises itself over mDNS/DNS-SD, accepts PWG-raster jobs, and never prints anything or forwards a job to a real printer.

It is a generic test target for anything that prints over IPP: mobile apps, drivers, CUPS setups, or your own client.

- **Named profiles.** `vp up <profile>...` starts one virtual printer per profile, each behaving like a specific real one: duplex, margins, colour, resolution, TLS, authentication.
- **Built from real data.** Profiles come from a real `Get-Printer-Attributes` response and the DNS-SD TXT record, scrubbed of identifying data. Variants are small overlays on a real base.
- **Playground and fixture.** Point a phone at it, or script it: start a profile, send a job, ask the control API what arrived.
- **See what a job did.** Every job leaves the raw raster, one PNG per page with the unprintable margins shaded, and a JSON receipt (sides, media, resolution, colour space, copies, page count).
- **Fault injection.** Busy, connection reset mid-upload, stalled upload, offline, 401, slow completion, media-empty, media-jam, toner-empty. Set per profile or switched at runtime.
- **Collect your own profiles.** `vp capture` reads a real printer (one read-only request, nothing is printed) and `vp scrub` removes its identity, with a leak check that fails the run if the hostname, IP, serial or MAC survives.

Every advertised printer name starts with `[VIRTUAL]`, so it is never mistaken for a real device.

## Install

Python 3.11+. Runs on macOS and Linux (mDNS advertising needs a network interface reachable from the client).

```bash
git clone https://github.com/sprilukin/virtual-printer.git
cd virtual-printer
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## Quick start

```bash
.venv/bin/vp up brother-dcp-l1630w                                           # loopback only, no mDNS
.venv/bin/vp up brother-dcp-l1630w duplex-color-laser-300dpi --bind 0.0.0.0  # LAN + mDNS, for a phone
```

Each printer prints its `ipp://` URI (ports from 6310) and its control URL (ports from 7310). A received job is written to `spool/<profile>/job-NNNN/`: `document.pwg`, `page-NNN.png` and `receipt.json`.

```bash
curl -s -XPOST localhost:7310/fault -d '{"type":"busy","params":{"count":1}}'   # next job gets server-error-busy
curl -s localhost:7310/jobs/1                                                   # what arrived
```

## Commands

| Command | Does |
|---|---|
| `vp up <profile>...` | run virtual printers until Ctrl-C |
| `vp capture <ipp-uri>` | read-only capture of a real printer (one `Get-Printer-Attributes` plus mDNS) |
| `vp convert <capture>` | raw capture to the canonical JSON tree |
| `vp scrub <capture>` | convert, scrub, leak-check and write `profiles/<name>/printer.json` |
| `vp validate [profile...]` | load and validate profiles |
| `vp ca` | create the local CA used by `--tls-mode ca` |

## Profiles

| Profile | Kind | What it models |
|---|---|---|
| `brother-dcp-l1630w` | real capture, scrubbed | mono laser, one-sided, 600 dpi raster, fixed margins |
| `duplex-color-laser-300dpi` | overlay | auto duplex, colour, 300 dpi |
| `duplex-mono-laser-normal` | overlay | native duplex on a mono laser |
| `borderless-inkjet-letter` | overlay | small margins, Letter default |
| `epson-et3950-inferred` | overlay, **inferred, not recorded** | a guess at an Epson ET-3950, not captured from a real device: HTTP 404 on every path except `/ipp`, colour inkjet, `image/pwg-raster`, `image/urf`, `application/pdf` |
| `ipps-auth-office` | overlay | `ipps://` with TLS and HTTP Basic auth (test user `office`, password `print-me`) |

Profiles live in `profiles/<name>/` as a `printer.json` (a full capture) or an `overlay.json` (changes on top of a base). To add one from a real printer, see [docs/collecting-profiles.md](docs/collecting-profiles.md).

## Documentation

- [docs/running.md](docs/running.md): running printers, job output, control API, faults, overlays, TLS and auth
- [docs/collecting-profiles.md](docs/collecting-profiles.md): capturing and scrubbing a real printer
- [docs/design.md](docs/design.md): goals, decisions, and why it is its own IPP server rather than a wrapper around `ippeveprinter`

## Limits

- The simulator replays what a printer advertises, not what it does with a real job. A pass here is not proof that a real printer behaves the same.
- Real printers are lenient with values they do not advertise. The simulator is strict by default (`--lenient` relaxes it).
- Only PWG raster is decoded. PDF and URF jobs are stored but not rendered.
- The control API has no authentication and listens on `127.0.0.1` by default. Do not expose it to an untrusted network.
- The Android emulator does not see mDNS multicast, so discovery needs a physical phone. Printing from the emulator works against `10.0.2.2`.

## Development

```bash
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/vulture
```

`pre-commit` runs the same checks and CI runs them on every push.

## Contributing

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). For profiles: only submit output of `vp scrub`, read it once first, and never attach raw captures, as they contain your printer's real address, serial and MAC.

## License

[MIT](LICENSE)
