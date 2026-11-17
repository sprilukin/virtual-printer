# Collecting a printer profile

> Verified against a Brother DCP-L1630W.

A profile is a scrubbed snapshot of one printer model: its IPP attributes and its DNS-SD record. Collecting one never prints anything: the only IPP request sent is one `Get-Printer-Attributes`. No `Print-Job`, no `Validate-Job`, no document data.

## What you need

- A computer on the same network as the printer, with Python 3.11+ and this repo installed (`python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`). Capture uses its own IPP client, not `ipptool`, so the response is stored as binary and converts losslessly.
- The printer's address: `ippfind` (ships with macOS; `cups-ipp-utils` on Linux) prints it.
- The printer's real MAC address and serial number (status page). The hostname and resolved IP are taken from the capture automatically; `--ip`/`--host` add more. The scrubber needs all of them for its leak check. Leaving out the serial or MAC is allowed but the check then cannot catch them.

## Steps

1. **Find the printer.**
   ```bash
   ippfind
   ```
2. **Capture** (read-only). Writes `captures/raw/<name>/` (gitignored): `get-printer-attributes.ipp` (binary response), `txt.json` (mDNS TXT record), `capture.json` (URI, host, resolved IP).
   ```bash
   vp capture ipp://<host>:631/ipp/print --name <vendor>-<model>
   ```
   `requested-attributes` is `all` plus `media-col-database`; the Brother returns `media-col-database` for `all` already. If mDNS finds no service, `capture` warns. Save `dns-sd -L "<service name>" _ipp._tcp local` output as `captures/raw/<name>/txt.txt` and carry on (the converter parses it; `dns-sd` writes spaces in names as `\032`).
3. **Convert** (optional, `scrub` does it too). Writes the unscrubbed canonical tree to `captures/raw/<name>/canonical.json`.
   ```bash
   vp convert <name>
   ```
4. **Scrub.**
   ```bash
   vp scrub <name> --mac <mac> --serial <serial> [--ip <ip>] [--host <hostname>]
   ```
   Writes `profiles/<name>/printer.json`. The scrubber drops volatile attributes (time, uptime, job counts, alerts), replaces name, UUID and location with stable fakes derived from the profile name, rewrites every other occurrence of the hostname, IP, serial and MAC in any string, then runs the leak check over the whole result (names, nested collections, decoded octet strings). Any survivor fails the run without writing the profile and without echoing the identifier. Extend the denylist in `src/vp/scrub.py` and rerun.
5. **Validate, review, commit.**
   ```bash
   vp validate
   ```
   Read the scrubbed JSON once. Commit only `profiles/<name>/`; `captures/raw/` is gitignored.

## Naming

`<vendor>-<model>`, lowercase, for example `brother-dcp-l1630w`. Variants made from a real base are overlay files named for the difference (`duplex-color-laser-300dpi`).

## Collecting from someone else's printer

Ask them to run steps 1-2 and send you `captures/raw/<name>/` privately, then run steps 3-5 yourself with the printer's MAC and serial. The raw capture contains the printer's real address, name, serial and MAC, so it should never go to a public place.

## Privacy checklist

- Raw captures stay in `captures/raw/`.
- `vp scrub` passed with the real MAC and serial given.
- Read the scrubbed JSON once before committing it, and never put raw captures in a public place.
