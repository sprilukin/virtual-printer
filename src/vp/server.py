"""One virtual printer: an asyncio HTTP listener speaking IPP.

Operations: Get-Printer-Attributes, Print-Job, Validate-Job, Create-Job,
Send-Document, Cancel-Job, Get-Job-Attributes, Get-Jobs, Identify-Printer.
A profile can ask for TLS (`options.tls`, served as `ipps://`) and HTTP Basic
auth (`options.auth`); see `vp.tls` and docs/running.md.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import copy
import hmac
import ssl
import time
from datetime import UTC, datetime
from pathlib import Path

from . import faults, ipp, jobs
from .validate import Validator

MAX_BODY = 256 * 1024 * 1024
IDLE, PROCESSING_STATE, STOPPED = 3, 4, 5
JOB_TEMPLATE_STEMS_FALLBACK = {
    "copies",
    "sides",
    "media",
    "media-col",
    "print-color-mode",
    "print-quality",
    "printer-resolution",
    "orientation-requested",
    "finishings",
    "output-bin",
}

OP_BY_CODE = {v: k for k, v in ipp.OPERATIONS.items()}
OP_BY_CODE[0x003C] = "Identify-Printer"

HTTP_REASONS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
    500: "Internal Server Error",
}


class HttpError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(message)
        self.status = status


def _op_attrs(*extra: dict) -> dict:
    return {
        "tag": "operation-attributes",
        "attributes": [
            ipp.attr("attributes-charset", "charset", "utf-8"),
            ipp.attr("attributes-natural-language", "naturalLanguage", "en"),
            *extra,
        ],
    }


def _date_time(ts: float) -> dict:
    d = datetime.fromtimestamp(ts, UTC)
    raw = (
        d.year.to_bytes(2, "big")
        + bytes([d.month, d.day, d.hour, d.minute, d.second, d.microsecond // 100000])
        + b"+\x00\x00"
    )
    return {"hex": raw.hex()}


class Printer:
    def __init__(
        self,
        profile: dict,
        spool: Path,
        host: str = "127.0.0.1",
        port: int = 0,
        ssl_context: ssl.SSLContext | None = None,
    ):
        self.profile = profile
        self.ssl_context = ssl_context
        self.scheme = "ipps" if ssl_context else "ipp"
        self.auth: dict | None = profile.get("options", {}).get("auth")
        self.auth_failures = 0
        self.name = profile["name"]
        self.host, self.port = host, port
        self.store = jobs.JobStore(spool)
        self.validator = Validator(profile)
        self.attrs = {a["name"]: a for a in profile["attributes"]}
        self.started = time.time()
        self.accepting = True
        self.state_reasons = ["none"]
        self.faults = faults.FaultSet(profile.get("options", {}).get("faults"))
        self.connections: set[asyncio.Task] = set()
        self._wake = asyncio.Event()  # set when faults change or a job is cancelled
        self.tasks: set[asyncio.Task] = set()
        self._server: asyncio.AbstractServer | None = None
        txt = dict(profile.get("dnssd", {}).get("txt", []))
        self.resource = "/" + txt.get("rp", "ipp/print").strip("/")
        self._fake_hosts = self._find_fake_hosts()

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_connection, self.host, self.port, ssl=self.ssl_context
        )
        self.port = self._server.sockets[0].getsockname()[1]

    @property
    def listening(self) -> bool:
        return self._server is not None

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await asyncio.sleep(0)  # let just-accepted connections register themselves
            while self.connections:  # held (stalled/offline) connections end here
                for t in list(self.connections):
                    t.cancel()
                await asyncio.gather(*self.connections, return_exceptions=True)
            await self._server.wait_closed()
        for t in list(self.tasks):
            await asyncio.gather(t, return_exceptions=True)

    def set_fault(self, ftype: object, params: object = None) -> dict:
        p = self.faults.set(ftype, params)
        self._notify()
        return p

    def clear_fault(self, ftype: str) -> bool:
        cleared = self.faults.clear(ftype)
        self._notify()
        return cleared

    def reset(self) -> None:
        """Forget all jobs and runtime faults; back to the profile's startup state."""
        self.faults.reset()
        self.store.reset()
        self.accepting = True
        self.state_reasons = ["none"]
        self.auth_failures = 0
        self._notify()

    def _notify(self) -> None:
        self._wake.set()
        self._wake = asyncio.Event()

    @property
    def effective_state_reasons(self) -> list[str]:
        extra = self.faults.state_reasons()
        base = [r for r in self.state_reasons if r != "none"]
        return [*extra, *base] or ["none"]

    @property
    def printer_state(self) -> int:
        if self.faults.blocking():
            return STOPPED
        busy = any(j.state == jobs.PROCESSING for j in self.store.jobs.values())
        return PROCESSING_STATE if busy else IDLE

    # ------------------------------------------------------------ HTTP

    async def _on_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task:
            self.connections.add(task)
        try:
            while await self._serve_one(reader, writer):
                pass
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
            pass
        finally:
            if task:
                self.connections.discard(task)
            writer.close()

    async def _serve_one(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        line = await reader.readline()
        if not line:
            return False
        try:
            method, target, _ = line.decode("latin-1").split()
        except ValueError:
            await self._http(writer, 400, close=True)
            return False
        headers: dict[str, str] = {}
        while True:
            h = (await reader.readline()).decode("latin-1").strip()
            if not h:
                break
            k, _, v = h.partition(":")
            headers[k.strip().lower()] = v.strip()
        close = headers.get("connection", "").lower() == "close"
        path = target.split("?", 1)[0]
        try:
            if await self._request_fault(reader, writer, method):
                return False
            if method == "POST" and not self._authorized(headers):
                self.auth_failures += 1
                await self._challenge(writer)
                return False
            if method != "POST":
                body = (f"{self.profile['name']}: virtual IPP printer\n").encode()
                await self._http(
                    writer,
                    200 if path.rstrip("/") in ("", self.resource) else 404,
                    body,
                    "text/plain",
                    close,
                )
                return not close
            if path.rstrip("/") not in ("", self.resource):
                raise HttpError(404)  # noqa: TRY301 - caught below to build the response
            if headers.get("content-type", "").split(";")[0].strip() != "application/ipp":
                raise HttpError(  # noqa: TRY301 - caught below to build the response
                    400, "content-type must be application/ipp"
                )
            if headers.get("expect", "").lower() == "100-continue":
                writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                await writer.drain()
            body = await self._read_body(reader, writer, headers)
            host = headers.get("host") or f"{self.host}:{self.port}"
            response = await self.handle_ipp(body, host)
            await self._http(writer, 200, response, "application/ipp", close)
        except HttpError as exc:
            await self._http(writer, exc.status, close=True)
            return False
        return not close

    async def _request_fault(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, method: str
    ) -> bool:
        """Request-level faults. True when the connection was dealt with (answered or dropped)."""
        if (p := self.faults.fire(faults.OFFLINE)) is not None:
            await self._hold(reader, p.get("seconds"), drain=True)
            return True
        if method == "POST" and self.faults.fire(faults.UNAUTHORIZED) is not None:
            await self._challenge(writer)
            return True
        return False

    def _authorized(self, headers: dict[str, str]) -> bool:
        """True when no auth is configured or the Basic credentials match a configured user."""
        if self.auth is None:
            return True
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            user, sep, password = (
                base64.b64decode(token.strip(), validate=True).decode().partition(":")
            )
        except (binascii.Error, UnicodeDecodeError):
            return False
        expected = self.auth["users"].get(user)
        return (
            bool(sep)
            and expected is not None
            and hmac.compare_digest(expected.encode(), password.encode())
        )

    async def _challenge(self, writer: asyncio.StreamWriter) -> None:
        realm = (self.auth or {}).get("realm", "virtual-printer")
        await self._http(
            writer, 401, close=True, extra=f'WWW-Authenticate: Basic realm="{realm}"\r\n'
        )

    @staticmethod
    async def _hold(reader: asyncio.StreamReader, seconds: float | None, drain: bool) -> None:
        """Keep a connection open without answering. `drain` keeps reading (client sees no stall)."""
        try:
            if drain:
                await asyncio.wait_for(reader.read(), seconds)
            else:
                await asyncio.sleep(seconds if seconds is not None else 3600 * 24)
        except TimeoutError:
            pass

    async def _body_fault(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, received: int
    ) -> None:
        """Mid-upload faults: fire once `received` bytes of a request body have arrived."""
        for ftype in (faults.RESET, faults.STALL):
            spec = self.faults.get(ftype)
            if spec is None or received < spec["after_bytes"]:
                continue
            p = self.faults.fire(ftype) or spec
            if ftype == faults.RESET:
                writer.transport.abort()  # TCP RST, not a clean close
                raise ConnectionResetError("fault: reset-mid-upload")
            await self._hold(reader, p.get("seconds"), drain=False)
            raise ConnectionResetError("fault: stall-body")

    async def _read_body(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, headers: dict[str, str]
    ) -> bytes:
        if headers.get("transfer-encoding", "").lower() == "chunked":
            chunks: list[bytes] = []
            total = 0
            while True:
                size = int((await reader.readline()).split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while (await reader.readline()).strip():
                        pass
                    return b"".join(chunks)
                total += size
                if total > MAX_BODY:
                    raise HttpError(413)
                chunks.append(await reader.readexactly(size))
                await reader.readexactly(2)
                await self._body_fault(reader, writer, total)
        n = int(headers.get("content-length", "0") or 0)
        if n > MAX_BODY:
            raise HttpError(413)
        for ftype in (faults.RESET, faults.STALL):
            spec = self.faults.get(ftype)
            if spec and 0 < spec["after_bytes"] < n:
                head = await reader.readexactly(spec["after_bytes"])
                await self._body_fault(reader, writer, len(head))
                return head + await reader.readexactly(n - len(head))
        return await reader.readexactly(n)

    async def _http(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes = b"",
        ctype: str = "text/plain",
        close: bool = False,
        extra: str = "",
    ) -> None:
        head = (
            f"HTTP/1.1 {status} {HTTP_REASONS.get(status, '')}\r\nContent-Type: {ctype}\r\n"
            f"Content-Length: {len(body)}\r\n{extra}"
            f"Connection: {'close' if close else 'keep-alive'}\r\n\r\n"
        )
        writer.write(head.encode() + body)
        await writer.drain()

    # ------------------------------------------------------------ IPP

    async def handle_ipp(self, data: bytes, host: str) -> bytes:
        try:
            req, document = ipp.decode_message(data)
        except ipp.IppError:
            return self._response(1, 1, 0x0400, 1, status_message="malformed IPP message")
        major, minor = (int(p) for p in req["version"].split("."))
        rid = req["request_id"]
        if req["version"] not in self.attrs["ipp-versions-supported"]["values"]:
            return self._response(2, 0, 0x0503, rid)
        ops = req["groups"][0]["attributes"] if req["groups"] else []
        if [a["name"] for a in ops[:2]] != ["attributes-charset", "attributes-natural-language"]:
            return self._response(
                major, minor, 0x0400, rid, status_message="charset/language missing"
            )
        op = OP_BY_CODE.get(req["code"])
        if op is None or req["code"] not in self.attrs["operations-supported"]["values"]:
            return self._response(major, minor, 0x0501, rid)
        try:
            handler = getattr(self, "op_" + op.lower().replace("-", "_"))
            return await handler(req, bytes(document), host, major, minor)
        except Exception as exc:  # a bug must not kill the connection
            return self._response(
                major, minor, 0x0500, rid, status_message=f"{type(exc).__name__}: {exc}"
            )

    def _response(
        self,
        major: int,
        minor: int,
        code: int,
        rid: int,
        *groups: dict,
        status_message: str | None = None,
        unsupported: list[dict] | None = None,
    ) -> bytes:
        extra = (
            [ipp.attr("status-message", "textWithoutLanguage", status_message)]
            if status_message
            else []
        )
        out = [_op_attrs(*extra), *groups]
        if unsupported:
            out.insert(1, {"tag": "unsupported-attributes", "attributes": unsupported})
        return ipp.encode(
            {"version": f"{major}.{minor}", "code": code, "request_id": rid, "groups": out}
        )

    @staticmethod
    def _op(req: dict, name: str) -> object | None:
        a = ipp.find_attr(req, name, "operation-attributes")
        return a["values"][0] if a and a["values"] else None

    # -- printer attributes

    def _find_fake_hosts(self) -> set[str]:
        hosts = set()
        for a in self.profile["attributes"]:
            if a["name"] in (
                "printer-uri-supported",
                "printer-icons",
                "printer-more-info",
                "printer-supply-info-uri",
                "printer-strings-uri",
            ):
                for v in a["values"]:
                    if "://" in v:
                        hosts.add(v.split("://", 1)[1].split("/", 1)[0])
        return hosts

    def _rewrite_uris(self, a: dict, host: str) -> dict:
        if a["name"] == "printer-uri-supported":
            return ipp.attr(a["name"], "uri", f"{self.scheme}://{host}{self.resource}")
        if a["name"] == "uri-security-supported":
            return ipp.attr(a["name"], "keyword", "tls" if self.ssl_context else "none")
        if a["name"] == "uri-authentication-supported":
            return ipp.attr(a["name"], "keyword", "basic" if self.auth else "none")
        if a["tag"] == "uri":
            a = copy.deepcopy(a)
            for i, v in enumerate(a["values"]):
                for fake in self._fake_hosts:
                    v = v.replace(fake, host)
                a["values"][i] = v
        return a

    def printer_attributes(self, host: str) -> list[dict]:
        now = time.time()
        dynamic = {
            "printer-state": ipp.attr("printer-state", "enum", self.printer_state),
            "printer-state-reasons": ipp.attr(
                "printer-state-reasons", "keyword", *self.effective_state_reasons
            ),
            "printer-is-accepting-jobs": ipp.attr(
                "printer-is-accepting-jobs", "boolean", self.accepting
            ),
            "queued-job-count": ipp.attr(
                "queued-job-count",
                "integer",
                sum(1 for j in self.store.jobs.values() if not j.finished_state),
            ),
            "printer-up-time": ipp.attr("printer-up-time", "integer", int(now - self.started) + 1),
            "printer-current-time": ipp.attr("printer-current-time", "dateTime", _date_time(now)),
        }
        out = [
            self._rewrite_uris(dynamic.get(a["name"], a), host) for a in self.profile["attributes"]
        ]
        out += [v for k, v in dynamic.items() if k not in self.attrs]
        return out

    def _group_of(self, name: str) -> str:
        jcas = self.attrs.get("job-creation-attributes-supported")
        stems = set(jcas["values"]) if jcas else JOB_TEMPLATE_STEMS_FALLBACK
        for suffix in ("-default", "-supported", "-ready"):
            if name.endswith(suffix) and name[: -len(suffix)] in stems:
                return "job-template"
        return "printer-description"

    async def op_get_printer_attributes(self, req, document, host, major, minor) -> bytes:
        req_attr = ipp.find_attr(req, "requested-attributes", "operation-attributes")
        wanted = set(req_attr["values"]) if req_attr else {"all"}
        have = self.printer_attributes(host)
        names = {a["name"] for a in have}
        if "all" in wanted:
            chosen = have
        else:
            groups = {w for w in wanted if w in ("job-template", "printer-description")}
            chosen = [a for a in have if a["name"] in wanted or self._group_of(a["name"]) in groups]
        missing = [
            ipp.attr(w, "unsupported")
            for w in sorted(wanted)
            if w not in names
            and w not in ("all", "job-template", "printer-description", "job-description")
        ]
        code = 0x0001 if missing else 0x0000
        return self._response(
            major,
            minor,
            code,
            req["request_id"],
            {"tag": "printer-attributes", "attributes": chosen},
            unsupported=missing or None,
        )

    async def op_identify_printer(self, req, document, host, major, minor) -> bytes:
        return self._response(major, minor, 0x0000, req["request_id"])

    # -- jobs

    def _job_group(self, job: jobs.Job, host: str, full: bool = True) -> dict:
        base = f"{self.scheme}://{host}{self.resource}"
        a = [
            ipp.attr("job-id", "integer", job.id),
            ipp.attr("job-uri", "uri", f"{base}/jobs/{job.id}"),
            ipp.attr("job-printer-uri", "uri", base),
            ipp.attr("job-state", "enum", job.state),
            ipp.attr("job-state-reasons", "keyword", *job.reasons),
        ]
        if full:
            a += [
                ipp.attr("job-name", "nameWithoutLanguage", job.name),
                ipp.attr("job-originating-user-name", "nameWithoutLanguage", job.user),
                ipp.attr("number-of-documents", "integer", 1),
                ipp.attr("job-impressions-completed", "integer", job.pages_completed),
                ipp.attr("job-media-sheets-completed", "integer", job.pages_completed),
                ipp.attr("time-at-creation", "integer", int(job.created - self.started)),
                ipp.attr(
                    "time-at-processing",
                    "integer",
                    int((job.started or job.created) - self.started),
                ),
                ipp.attr("time-at-completed", "integer", int(job.finished - self.started))
                if job.finished
                else ipp.attr("time-at-completed", "no-value"),
            ]
        return {"tag": "job-attributes", "attributes": a}

    def _new_job(self, req: dict, operation: str) -> jobs.Job:
        fmt = self._op(req, "document-format")
        job_attrs: list[dict] = next(
            (g["attributes"] for g in req["groups"] if g["tag"] == "job-attributes"), []
        )
        return self.store.create(
            name=str(self._op(req, "job-name") or "untitled"),
            user=str(self._op(req, "requesting-user-name") or "anonymous"),
            operation=operation,
            attributes=job_attrs,
            document_format=str(fmt or self.attrs["document-format-default"]["values"][0]),
            compression=str(self._op(req, "compression") or "none"),
        )

    def _gate(self, req: dict, major: int, minor: int):
        """Common Print-Job/Validate-Job/Create-Job checks. Returns (response|None, outcome)."""
        outcome = self.validator.validate(req)
        if not self.accepting:
            return self._response(major, minor, 0x0506, req["request_id"]), outcome
        if not outcome.accepted:
            return self._response(
                major,
                minor,
                outcome.code,
                req["request_id"],
                status_message="; ".join(outcome.notes) or None,
                unsupported=outcome.unsupported,
            ), outcome
        return None, outcome

    async def op_validate_job(self, req, document, host, major, minor) -> bytes:
        early, outcome = self._gate(req, major, minor)
        return early or self._response(
            major, minor, outcome.code, req["request_id"], unsupported=outcome.unsupported or None
        )

    def _busy(self, req: dict, major: int, minor: int) -> bytes | None:
        if self.faults.fire(faults.BUSY) is None:
            return None
        return self._response(
            major, minor, 0x0507, req["request_id"], status_message="printer busy"
        )

    async def op_print_job(self, req, document, host, major, minor) -> bytes:
        if busy := self._busy(req, major, minor):
            return busy
        early, outcome = self._gate(req, major, minor)
        if early:
            return early
        job = self._new_job(req, "Print-Job")
        job.validation = outcome.to_json()
        self._spawn(job, document)
        return self._response(
            major,
            minor,
            outcome.code,
            req["request_id"],
            self._job_group(job, host, False),
            unsupported=outcome.unsupported or None,
        )

    async def op_create_job(self, req, document, host, major, minor) -> bytes:
        if busy := self._busy(req, major, minor):
            return busy
        early, outcome = self._gate(req, major, minor)
        if early:
            return early
        job = self._new_job(req, "Create-Job")
        job.validation = outcome.to_json()
        job.state = jobs.PENDING_HELD
        job.reasons = ["job-incoming"]
        return self._response(
            major,
            minor,
            outcome.code,
            req["request_id"],
            self._job_group(job, host, False),
            unsupported=outcome.unsupported or None,
        )

    def _job_from(self, req: dict) -> jobs.Job | None:
        jid = self._op(req, "job-id")
        if jid is None:
            uri = str(self._op(req, "job-uri") or "")
            jid = int(uri.rsplit("/", 1)[-1]) if uri.rsplit("/", 1)[-1].isdigit() else None
        return self.store.get(jid) if isinstance(jid, int) else None

    async def op_send_document(self, req, document, host, major, minor) -> bytes:
        if busy := self._busy(req, major, minor):
            return busy
        job = self._job_from(req)
        if job is None:
            return self._response(major, minor, 0x0406, req["request_id"])
        if job.state != jobs.PENDING_HELD:
            return self._response(major, minor, 0x0404, req["request_id"])
        fmt = self._op(req, "document-format")
        if fmt:
            job.document_format = str(fmt)
        job.compression = str(self._op(req, "compression") or job.compression)
        job.state, job.reasons = jobs.PENDING, ["none"]
        self._spawn(job, document)
        return self._response(
            major, minor, 0x0000, req["request_id"], self._job_group(job, host, False)
        )

    async def op_cancel_job(self, req, document, host, major, minor) -> bytes:
        job = self._job_from(req)
        if job is None:
            return self._response(major, minor, 0x0406, req["request_id"])
        if job.finished_state:
            return self._response(major, minor, 0x0404, req["request_id"])
        job.canceled = True
        self._notify()
        if job.state in (jobs.PENDING, jobs.PENDING_HELD):
            job.state, job.reasons, job.finished = (
                jobs.CANCELED,
                ["job-canceled-by-user"],
                time.time(),
            )
        return self._response(major, minor, 0x0000, req["request_id"])

    async def op_get_job_attributes(self, req, document, host, major, minor) -> bytes:
        job = self._job_from(req)
        if job is None:
            return self._response(major, minor, 0x0406, req["request_id"])
        return self._response(major, minor, 0x0000, req["request_id"], self._job_group(job, host))

    async def op_get_jobs(self, req, document, host, major, minor) -> bytes:
        which = self._op(req, "which-jobs") or "not-completed"
        limit = self._op(req, "limit")
        sel = [j for j in self.store.jobs.values() if j.finished_state == (which == "completed")]
        if isinstance(limit, int):
            sel = sel[:limit]
        return self._response(
            major, minor, 0x0000, req["request_id"], *(self._job_group(j, host, False) for j in sel)
        )

    # -- processing

    def _spawn(self, job: jobs.Job, data: bytes) -> None:
        job.state, job.reasons = jobs.PROCESSING, ["job-printing"]
        task = asyncio.get_running_loop().create_task(self._process(job, data))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _hold_for_faults(self, job: jobs.Job) -> bool:
        """Wait out printer faults (media-empty, jam, toner). False: the job ended here."""
        while (blocking := self.faults.blocking()) is not None and not job.canceled:
            if blocking["job_mode"] == "abort":
                job.state, job.reasons = jobs.ABORTED, ["aborted-by-system"]
                job.finished = time.time()
                return False
            job.state, job.reasons = jobs.PROCESSING_STOPPED, ["printer-stopped"]
            await self._wake.wait()
        job.state, job.reasons = jobs.PROCESSING, ["job-printing"]
        return True

    async def _process(self, job: jobs.Job, data: bytes) -> None:
        try:
            if not await self._hold_for_faults(job):
                return
            if (slow := self.faults.fire(faults.SLOW)) is not None:
                deadline = time.monotonic() + slow["seconds"]
                while (left := deadline - time.monotonic()) > 0 and not job.canceled:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), left)
            if job.canceled:
                job.state, job.reasons = jobs.CANCELED, ["job-canceled-by-user"]
                job.finished = time.time()
                return
            await asyncio.to_thread(jobs.process, job, self.profile, data)
        except Exception as exc:
            job.state, job.reasons, job.finished = jobs.ABORTED, ["aborted-by-system"], time.time()
            (job.dir / "error.txt").write_text(f"{type(exc).__name__}: {exc}\n")  # type: ignore[operator]

    async def wait_idle(self, timeout: float = 30.0) -> None:  # noqa: ASYNC109 - explicit deadline
        """Wait until every job has finished processing (tests, scripts)."""
        deadline = time.monotonic() + timeout
        while self.tasks and time.monotonic() < deadline:
            await asyncio.wait(list(self.tasks), timeout=0.1)
