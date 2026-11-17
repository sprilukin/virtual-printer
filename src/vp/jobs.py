"""Jobs: storage on disk, processing of received documents, receipts."""

from __future__ import annotations

import gzip
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import png, pwg

PENDING, PENDING_HELD, PROCESSING, PROCESSING_STOPPED, CANCELED, ABORTED, COMPLETED = (
    3,
    4,
    5,
    6,
    7,
    8,
    9,
)
STATE_NAMES = {
    3: "pending",
    4: "pending-held",
    5: "processing",
    6: "processing-stopped",
    7: "canceled",
    8: "aborted",
    9: "completed",
}
RASTER_FORMATS = {"image/pwg-raster", "application/octet-stream"}
SIDES = ("top", "bottom", "left", "right")


def to_plain(a: dict) -> object:
    """Attribute tree to plain JSON: one value stays scalar, collections become dicts."""
    vals = [
        {m["name"]: to_plain(m) for m in v["members"]} if a["tag"] == "collection" else v
        for v in a["values"]
    ]
    return vals[0] if len(vals) == 1 else vals


@dataclass
class Job:
    id: int
    name: str
    user: str
    operation: str
    attributes: list[dict]  # job-template attributes as received
    created: float = field(default_factory=time.time)
    state: int = PENDING
    reasons: list[str] = field(default_factory=lambda: ["none"])
    document_format: str = "application/octet-stream"
    compression: str = "none"
    validation: dict = field(default_factory=dict)
    dir: Path | None = None
    receipt: dict | None = None
    pages_completed: int = 0
    started: float | None = None
    finished: float | None = None
    canceled: bool = False

    def attr(self, name: str) -> dict | None:
        return next((a for a in self.attributes if a["name"] == name), None)

    @property
    def finished_state(self) -> bool:
        return self.state in (CANCELED, ABORTED, COMPLETED)


class JobStore:
    def __init__(self, spool: Path):
        self.spool = spool
        self.jobs: dict[int, Job] = {}
        self._next = 1

    def create(self, **kw) -> Job:
        job = Job(id=self._next, **kw)
        self._next += 1
        job.dir = self.spool / f"job-{job.id:04d}"
        job.dir.mkdir(parents=True, exist_ok=True)
        self.jobs[job.id] = job
        return job

    def reset(self) -> None:
        self.jobs.clear()
        self._next = 1

    def get(self, job_id: int) -> Job | None:
        return self.jobs.get(job_id)


# ---------------------------------------------------------------- margins


def effective_margins(job: Job, profile: dict) -> dict[str, int]:
    """Unprintable margins in 1/100 mm: the job's media-col, else the profile default."""
    attrs = {a["name"]: a for a in profile["attributes"]}
    found: dict[str, int] = {}

    def from_col(col: dict) -> None:
        for m in col["members"]:
            for s in SIDES:
                if m["name"] == f"media-{s}-margin" and m["values"]:
                    found.setdefault(s, m["values"][0])

    for src in (job.attr("media-col"), attrs.get("media-col-default")):
        for col in src["values"] if src else []:
            from_col(col)
    for s in SIDES:
        if s not in found:
            sup = attrs.get(f"media-{s}-margin-supported")
            found[s] = sup["values"][0] if sup and sup["tag"] == "integer" and sup["values"] else 0
    return found


def _px(hundredths_mm: int, dpi: int) -> int:
    return round(hundredths_mm / 2540 * dpi)


# ---------------------------------------------------------------- processing


def _media_of(job: Job, profile: dict) -> dict:
    attrs = {a["name"]: a for a in profile["attributes"]}
    media = job.attr("media")
    col = job.attr("media-col") or attrs.get("media-col-default")
    size = None
    if col and col["values"]:
        for m in col["values"][0]["members"]:
            if m["name"] == "media-size":
                size = {k["name"]: k["values"][0] for k in m["values"][0]["members"]}
    name = (
        media["values"][0]
        if media
        else (attrs.get("media-default") or {"values": [None]})["values"][0]
    )
    return {"name": name, "size_hundredths_mm": size}


def _decode_document(job: Job, data: bytes) -> tuple[bytes, list[pwg.Page] | None, str | None]:
    if job.compression == "gzip":
        try:
            data = gzip.decompress(data)
        except OSError as exc:
            return data, None, f"gzip: {exc}"
    if job.document_format in RASTER_FORMATS and data[:4] == pwg.SYNC:
        try:
            return data, pwg.decode(data), None
        except pwg.RasterError as exc:
            return data, None, f"pwg-raster: {exc}"
    if job.document_format == "image/pwg-raster":
        return data, None, "pwg-raster: missing RaS2 sync word"
    return data, None, f"document format {job.document_format} is stored but not decoded"


def process(job: Job, profile: dict, data: bytes) -> dict:
    """Decode the document, write raster + PNGs + receipt, set final job state."""
    job.state, job.started = PROCESSING, time.time()
    assert job.dir is not None
    raw, pages, error = _decode_document(job, data)
    suffix = ".pwg" if pages or job.document_format == "image/pwg-raster" else ".bin"
    (job.dir / f"document{suffix}").write_bytes(raw)

    attrs = {a["name"]: a for a in profile["attributes"]}
    strict = profile.get("options", {}).get("validation", "strict") == "strict"
    warnings: list[str] = []
    page_info: list[dict] = []
    margins = effective_margins(job, profile)
    for i, page in enumerate(pages or [], start=1):
        dpi = page.resolution[0] or 1
        px = (
            _px(margins["top"], dpi),
            _px(margins["bottom"], dpi),
            _px(margins["left"], dpi),
            _px(margins["right"], dpi),
        )
        png_name = f"page-{i:03d}.png"
        (job.dir / png_name).write_bytes(png.render(page, px))
        job.pages_completed = i
        page_info.append(
            {
                "index": i,
                "png": png_name,
                "width_px": page.width,
                "height_px": page.height,
                "resolution_dpi": list(page.resolution),
                "color_space": page.color_space_name,
                "type": page.type_name,
                "bits_per_pixel": page.bits_per_pixel,
                "duplex": page.duplex,
                "tumble": page.tumble,
                "num_copies": page.num_copies,
                "page_size_pts": list(page.page_size_pts),
                "page_size_name": page.page_size_name,
                "margins_px": {"top": px[0], "bottom": px[1], "left": px[2], "right": px[3]},
            }
        )
        res_sup = attrs.get("pwg-raster-document-resolution-supported") or attrs.get(
            "printer-resolution-supported"
        )
        if (
            res_sup
            and {"x": page.resolution[0], "y": page.resolution[1], "units": "dpi"}
            not in res_sup["values"]
        ):
            warnings.append(
                f"page {i}: raster resolution {page.resolution[0]}x{page.resolution[1]} dpi not advertised"
            )
        type_sup = attrs.get("pwg-raster-document-type-supported")
        if type_sup and page.type_name not in type_sup["values"]:
            warnings.append(f"page {i}: raster type {page.type_name} not advertised")

    if error and not pages:
        warnings.append(error)
    duplex = any(p["duplex"] for p in page_info)
    sides = job.attr("sides")
    copies = job.attr("copies")
    receipt = {
        "job_id": job.id,
        "job_name": job.name,
        "user": job.user,
        "received_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(job.created)),
        "profile": profile["name"],
        "document_format": job.document_format,
        "compression": job.compression,
        "bytes_received": len(data),
        "validation": job.validation,
        "sides": sides["values"][0] if sides else ("two-sided" if duplex else "one-sided"),
        "copies": copies["values"][0] if copies else 1,
        "media": _media_of(job, profile),
        "margins_hundredths_mm": margins,
        "page_count": len(page_info),
        "sheet_count": -(-len(page_info) // 2) if duplex else len(page_info),
        "pages": page_info,
        "job_attributes": {a["name"]: to_plain(a) for a in job.attributes},
        "warnings": warnings,
    }
    if page_info:
        first = page_info[0]
        receipt["resolution_dpi"] = first["resolution_dpi"]
        receipt["color_space"] = first["color_space"]
        receipt["pixel_dimensions"] = [first["width_px"], first["height_px"]]

    failed = bool(error and not pages) or (strict and any("not advertised" in w for w in warnings))
    if job.canceled:
        job.state, job.reasons = CANCELED, ["job-canceled-by-user"]
    elif failed:
        job.state, job.reasons = ABORTED, ["document-format-error", "aborted-by-system"]
    else:
        job.state, job.reasons = COMPLETED, ["job-completed-successfully"]
    job.finished = time.time()
    receipt["state"] = STATE_NAMES[job.state]
    receipt["state_reasons"] = job.reasons
    job.receipt = receipt
    (job.dir / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt
