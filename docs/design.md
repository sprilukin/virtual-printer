# Design notes

Why the virtual printer is built the way it is: goals, non-goals, key decisions, and the evidence behind the decision to write our own IPP server.

## Goals

1. **Named profiles.** `vp up <profile>...` starts virtual printers that behave like specific real ones (duplex, margins, colour, resolution, faults, TLS/auth).
2. **Two uses.** A manual playground (point a phone or emulator at it) and a scriptable fixture for instrumented tests (start a profile, send a job, ask what arrived).
3. **Fidelity from real data.** Profiles are built from real `Get-Printer-Attributes` dumps and the DNS-SD TXT record. Hand-written variants are small overlays on a real base.
4. **A collection tool.** A documented CLI flow for turning a real printer into a scrubbed profile.

## Non-goals

- Emulating network isolation (VLAN, client isolation, firewalls) or a phone OS's local-network permission behaviour. Those belong to the network and the phone.
- Enterprise access control (allow-lists, print-release PINs).
- Native PDF, URF, multi-copy collation, trays or finishing. Only PWG raster is decoded.
- Forwarding jobs to a real printer. Never.

## Decisions

| Topic | Decision |
|---|---|
| Engine | Own Python IPP server (see "Engine decision" below); `ippeveprinter` is kept only as a reference |
| mDNS | Our own responder (python `zeroconf`), so the stored TXT record is replayed verbatim |
| Profile format | One lossless JSON attribute tree (name, value tag, values, nested collections). Base dump plus optional overlay file |
| Raw captures | Binary IPP responses and raw tool output stay in gitignored `captures/raw/` |
| Scrubbing | Mandatory. A denylist of sensitive attributes is replaced by stable fakes, then an exact-match leak check (real IP, hostname, serial, MAC) fails the run on any survivor, including nested values |
| Validate-Job | Not probed. A client only sends parameters the printer advertised, so probing unadvertised values has little value. The one probe run on the Brother is recorded below as a reference |
| Job validation | Checked against the advertised `*-supported` attributes only. Strict (reject with `client-error-attributes-or-values-not-supported`) or lenient (accept) per profile, default strict |
| Naming | Every advertised name starts with `[VIRTUAL]` |
| Control API | HTTP+JSON on its own port, localhost by default |

## Known limits

- **Fidelity.** The simulator replays what printers advertise, not what they do with a real job. A pass here is not proof on a real printer.
- **Real printers are lenient.** The Brother answers `successful-ok` or "substituted" to values it does not advertise; the simulator does not reproduce that.
- **Emulator networking.** The Android emulator does not see the host's mDNS multicast. Discovery needs a physical phone; printing can use `10.0.2.2`.
- **Scrubbing is only as good as the denylist.** The exact-match leak check is the safety net. Unknown vendor attributes are kept as-is unless on the denylist, so review a scrubbed profile before committing it.

## Engine decision



**Write our own IPP server in Python. Do not wrap `ippeveprinter`.**

The rule of thumb was: mostly yes means wrap `ippeveprinter`, mostly no means write our own. Of the four questions, only TLS is a clean yes. A wrapper would need a proxy in front of `ippeveprinter` for matrix replay, transport faults, auth and runtime state. At that point the proxy is most of the server. `ippeveprinter` stays useful as a reference implementation.

## Evidence

| # | Question | Answer | Evidence |
|---|---|---|---|
| 1 | Validates jobs against advertised capabilities? | **Partly.** | Against a conf built from the Brother dump, `Validate-Job` rejected `printer-resolution=720dpi`, `sides=two-sided-long-edge`, `media=iso_a3_297x420mm` and `document-format=application/pdf` with `client-error-attributes-or-values-not-supported`. It accepted `print-color-mode=color` although only monochrome modes were advertised. It checks `*-supported` only, so it cannot replay a recorded accept/reject matrix. |
| 2 | `printer-state-reasons` and job state change at runtime? | **Only from inside a job.** | The `-c` print command can emit `STATE: +media-jam-warning` and the printer reported it during the job. The state persisted afterwards as `media-jam-report` when the command exited 1. There is no external switch. The web forms on `/media` and `/supplies` exist but are not a scriptable control API. |
| 3 | Fault injection (mid-job failure, stall)? | **Job-level only.** | The `-c` command can sleep or exit non-zero, which aborts the job. Nothing covers `server-error-busy`, a connection reset mid-upload, a stalled body, going offline, or a 401 on demand. These are transport-level and need our own listener. |
| 4 | Basic auth without PAM; TLS with a self-signed cert? | **TLS yes. Auth no.** | TLS: `ippeveprinter` serves `ipps://` on the same port with an auto-generated self-signed cert (CN = host name, 1 year). `-K keypath` selects a cert. Auth: `-A` returns 401 for plain HTTP and makes `Validate-Job` hang on a credential prompt, so the 401 path works. Credentials are checked via PAM, and macOS has no `/etc/pam.d/cups`, so a successful login can't be tested without root-level PAM setup. |

Other findings:

- **Conf format.** `-a file.conf` takes ipptool-style lines, `ATTR <tag> <name> <value>`. Bare `name value` lines are silently ignored. Collections can't be written this way without more syntax, and only the attributes in the file replace the defaults, so a full dump replay is awkward.
- **Option parsing.** `-a` combined with `-f` or `-m` exited with a bare usage message in my runs. `-k`, `-F` and `-d` were fine. I did not find out why.
- **Same machine.** A local `ippeveprinter` often holds port 8631. Our server must not assume that port.

## Brother DCP-L1630W capture (read-only)

- `ippfind` found `ipp://<redacted-hostname>.local:631/ipp/print`. One `Get-Printer-Attributes` request, no jobs.
- **`media-col-database` is returned.** The stock `get-printer-attributes.test` returned it (about 16 KB total), so there is no need to name it. This answers the open question in `docs/collecting-profiles.md`.
- The dump advertises `sides-supported=one-sided`, `print-color-mode-supported=monochrome,auto,auto-monochrome`, 300/600/1200 dpi, `image/urf` and `image/pwg-raster`, and fixed 423 (1/100 mm) margins. The TXT record also carries the real UUID and hostname, which the scrubber replaces.
- `ippfind -T` / `dns-sd -L "<name>" _ipp._tcp local` gave the full TXT record. Note `dns-sd` writes spaces in names as `\032`.

## Brother `Validate-Job` probes (run after approval)

16 probes (baseline plus one factor at a time), no document data. Raw output is not committed. I could not watch the printer for paper output, so confirm nothing came out of the tray.

| Probe | Brother answer |
|---|---|
| baseline (600dpi, monochrome, one-sided, A4, 1 copy) | `successful-ok` |
| 300dpi, 1200dpi; `auto` colour mode; Letter, Legal, A5 | `successful-ok` |
| 720dpi; `color`; `bi-level`; A3; copies 2, 999, 1000 | `successful-ok-ignored-or-substituted-attributes` |
| `two-sided-long-edge`, `two-sided-short-edge` | `successful-ok` **although only `one-sided` is advertised** |

- The Brother never returns `client-error-attributes-or-values-not-supported`. Unsupported values come back as the "substituted" success code, so the matrix must record that third status.
- It answers plain `successful-ok` for duplex, which it can't do. This is the "Validate-Job can lie" risk from the plan, now seen on real hardware.
- Copies 2 is "substituted" even though `copies-supported` is 1-999, which suggests the printer wants copies handled by the client.
- `ippeveprinter` would have rejected the duplex, 720dpi and A3 probes outright, so it can't reproduce this behaviour. That supports the decision above.

## Not done
- Auth with a successful credential, and an `ipps://` cert trusted by a device, were not tested.

## Consequences for later phases

- The server is built on a Python server (stdlib `asyncio` / `http.server` plus an IPP codec, with `zeroconf` for mDNS), with a small codec for the IPP wire format that the JSON profile tree encodes into directly.
- Transport faults come from owning the socket, and auth is a plain Basic header check with no PAM.
- The `ipptool` conf is not used as a profile format. The JSON attribute tree stays as planned.
