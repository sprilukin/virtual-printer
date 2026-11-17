"""Control API of one running printer: HTTP + JSON on its own port.

    GET  /state                    printer state, active faults, job counts
    GET  /jobs                     every job received
    GET  /jobs/<id>                one job with its receipt (once processed)
    GET  /jobs/<id>/pages/<n>.png  the page image with margins shaded
    POST /fault {type, params}     switch a fault on; {type, enabled: false} switches it off
    POST /reset                    forget jobs, return faults to the profile's startup set

No authentication: bind it to loopback (the default) unless you need it elsewhere.
"""

from __future__ import annotations

import asyncio
import json
import re
from http import HTTPStatus
from typing import Any

from . import faults, jobs
from .server import Printer

MAX_REQUEST = 1024 * 1024
JOB_PATH = re.compile(r"^/jobs/(\d+)(?:/pages/(\d+)\.png)?$")
PRINTER_STATES = {3: "idle", 4: "processing", 5: "stopped"}


class NotFoundError(Exception):
    pass


class ControlServer:
    def __init__(self, printer: Printer, host: str = "127.0.0.1", port: int = 0):
        self.printer = printer
        self.host, self.port = host, port
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[asyncio.Task[Any]] = set()

    @property
    def listening(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_connection, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            for t in list(self._connections):
                t.cancel()
            await asyncio.gather(*self._connections, return_exceptions=True)
            await self._server.wait_closed()

    # ------------------------------------------------------------ HTTP

    async def _on_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task:
            self._connections.add(task)
        try:
            status, ctype, body = await asyncio.wait_for(self._request(reader), 30)
            writer.write(
                f"HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\nContent-Type: {ctype}\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if task:
                self._connections.discard(task)
            writer.close()

    async def _request(self, reader: asyncio.StreamReader) -> tuple[int, str, bytes]:
        line = (await reader.readline()).decode("latin-1").split()
        if len(line) < 2:
            return self._json(400, {"error": "bad request line"})
        method, target = line[0], line[1]
        headers: dict[str, str] = {}
        while h := (await reader.readline()).decode("latin-1").strip():
            k, _, v = h.partition(":")
            headers[k.strip().lower()] = v.strip()
        length = headers.get("content-length", "0")
        if not length.isdigit() or int(length) > MAX_REQUEST:
            return self._json(400, {"error": "bad or oversized Content-Length"})
        raw = await reader.readexactly(int(length)) if int(length) else b""
        try:
            return self.route(method, target.split("?", 1)[0].rstrip("/") or "/", raw)
        except NotFoundError as exc:
            return self._json(404, {"error": str(exc)})
        except faults.FaultError as exc:
            return self._json(400, {"error": str(exc)})
        except json.JSONDecodeError:
            return self._json(400, {"error": "body must be JSON"})

    @staticmethod
    def _json(status: int, obj: object) -> tuple[int, str, bytes]:
        return status, "application/json", (json.dumps(obj, indent=2) + "\n").encode()

    # ------------------------------------------------------------ routes

    def route(self, method: str, path: str, raw: bytes) -> tuple[int, str, bytes]:
        known = path in ("/state", "/jobs", "/fault", "/reset") or JOB_PATH.match(path)
        if method == "GET":
            if path == "/state":
                return self._json(200, self.state())
            if path == "/jobs":
                return self._json(200, [self.job_json(j) for j in self.printer.store.jobs.values()])
            if m := JOB_PATH.match(path):
                return self._job_response(int(m[1]), m[2])
        elif method == "POST":
            if path == "/fault":
                return self._json(200, self._fault(json.loads(raw or b"{}")))
            if path == "/reset":
                self.printer.reset()
                return self._json(200, self.state())
        if known:
            return self._json(405, {"error": f"{method} not allowed on {path}"})
        raise NotFoundError(f"no such route: {method} {path}")

    def _job_response(self, job_id: int, page: str | None) -> tuple[int, str, bytes]:
        job = self.printer.store.get(job_id)
        if job is None:
            raise NotFoundError(f"no job {job_id}")
        if page is None:
            return self._json(200, self.job_json(job, with_receipt=True))
        png = job.dir / f"page-{int(page):03d}.png" if job.dir else None
        if png is None or not png.is_file():
            raise NotFoundError(f"job {job_id} has no page {int(page)}")
        return 200, "image/png", png.read_bytes()

    def _fault(self, req: object) -> dict[str, Any]:
        if not isinstance(req, dict) or set(req) - {"type", "params", "enabled"}:
            raise faults.FaultError("body is {type, params?, enabled?}")
        ftype = req.get("type")
        if req.get("enabled", True) is False:
            if not isinstance(ftype, str) or ftype not in faults.SPECS:
                raise faults.FaultError(f"unknown fault type {ftype!r}")
            self.printer.clear_fault(ftype)
        else:
            self.printer.set_fault(ftype, req.get("params"))
        return {"faults": self.printer.faults.to_json()}

    def state(self) -> dict[str, Any]:
        p = self.printer
        counts: dict[str, int] = {}
        for j in p.store.jobs.values():
            name = jobs.STATE_NAMES[j.state]
            counts[name] = counts.get(name, 0) + 1
        return {
            "name": p.name,
            "uri": f"{p.scheme}://{p.host}:{p.port}{p.resource}",
            "printer_state": PRINTER_STATES[p.printer_state],
            "state_reasons": p.effective_state_reasons,
            "accepting_jobs": p.accepting,
            "validation": p.profile.get("options", {}).get("validation", "strict"),
            "tls": p.ssl_context is not None,
            "auth": {"type": "basic", "users": sorted(p.auth["users"])} if p.auth else None,
            "auth_failures": p.auth_failures,
            "faults": p.faults.to_json(),
            "jobs": counts,
            "fault_types": sorted(faults.SPECS),
        }

    @staticmethod
    def job_json(job: jobs.Job, with_receipt: bool = False) -> dict[str, Any]:
        out = {
            "id": job.id,
            "name": job.name,
            "user": job.user,
            "operation": job.operation,
            "state": jobs.STATE_NAMES[job.state],
            "state_reasons": job.reasons,
            "finished": job.finished_state,
            "pages_completed": job.pages_completed,
            "validation": job.validation,
        }
        if job.dir and (job.dir / "error.txt").is_file():
            out["error"] = (job.dir / "error.txt").read_text().strip()
        if with_receipt:
            out["receipt"] = job.receipt
        return out
