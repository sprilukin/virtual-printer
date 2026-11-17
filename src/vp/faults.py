"""Fault catalogue: what can be switched on, with which parameters.

A `FaultSet` holds the active faults of one printer. Profiles list their
startup faults under `options.faults`; the control API changes them at runtime.
Faults that act on requests take an optional `count`: they fire that many times
and then switch themselves off.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

BUSY = "busy"
RESET = "reset-mid-upload"
STALL = "stall-body"
OFFLINE = "offline"
UNAUTHORIZED = "unauthorized"
SLOW = "slow-completion"
MEDIA_EMPTY = "media-empty"
MEDIA_JAM = "media-jam"
TONER_EMPTY = "toner-empty"

SEVERITIES = ("error", "warning", "report")
STATE_REASON_FAULTS = {MEDIA_EMPTY, MEDIA_JAM, TONER_EMPTY}
JOB_MODES = ("hold", "abort")


@dataclass(frozen=True)
class Spec:
    params: dict[str, type]
    defaults: dict[str, object]
    counted: bool = True


SPECS: dict[str, Spec] = {
    # Print-Job / Create-Job / Send-Document answer server-error-busy
    BUSY: Spec({}, {}),
    # connection reset once a request body exceeds `after_bytes`
    RESET: Spec({"after_bytes": int}, {"after_bytes": 1024}),
    # stop reading the body after `after_bytes`; hold the connection `seconds` (None: until closed)
    STALL: Spec({"after_bytes": int, "seconds": float}, {"after_bytes": 1024}),
    # accept the connection and request, never answer
    OFFLINE: Spec({"seconds": float}, {}),
    # HTTP 401 with a Basic challenge
    UNAUTHORIZED: Spec({}, {}),
    # jobs stay in `processing` for `seconds` before completing
    SLOW: Spec({"seconds": float}, {"seconds": 5.0}),
    # printer-state-reasons plus printer-state=stopped; jobs are held or aborted
    **{
        t: Spec(
            {"severity": str, "job_mode": str}, {"severity": "error", "job_mode": "hold"}, False
        )
        for t in sorted(STATE_REASON_FAULTS)
    },
}


class FaultError(ValueError):
    pass


def validate(ftype: object, params: object = None) -> tuple[str, dict]:
    """Check a fault request; return (type, params with defaults applied)."""
    if not isinstance(ftype, str) or ftype not in SPECS:
        raise FaultError(f"unknown fault type {ftype!r}; known: {', '.join(sorted(SPECS))}")
    spec = SPECS[ftype]
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise FaultError("params must be an object")
    allowed = {**spec.params, **({"count": int} if spec.counted else {})}
    out: dict[str, Any] = dict(spec.defaults)
    given: dict[str, Any] = params
    for k, v in given.items():
        if k not in allowed:
            raise FaultError(
                f"{ftype}: unknown parameter {k!r}; allowed: {sorted(allowed) or 'none'}"
            )
        want = allowed[k]
        if isinstance(v, bool) or not isinstance(v, (int, float) if want is float else want):
            raise FaultError(f"{ftype}: {k} must be {want.__name__}")
        num: Any = v
        out[k] = float(num) if want is float else num
        if want in (int, float) and num < 0:
            raise FaultError(f"{ftype}: {k} must not be negative")
    if out.get("severity", "error") not in SEVERITIES:
        raise FaultError(f"{ftype}: severity must be one of {list(SEVERITIES)}")
    if out.get("job_mode", "hold") not in JOB_MODES:
        raise FaultError(f"{ftype}: job_mode must be one of {list(JOB_MODES)}")
    if out.get("count") == 0:
        raise FaultError(f"{ftype}: count must be at least 1")
    return ftype, out


def validate_list(items: object) -> None:
    """Validate `options.faults` of a profile: a list of {type, params}."""
    if not isinstance(items, list):
        raise FaultError("options.faults must be a list")
    for it in items:
        if not isinstance(it, dict) or set(it) - {"type", "params"}:
            raise FaultError("each fault is {type, params?}")
        validate(it.get("type"), it.get("params"))


class FaultSet:
    def __init__(self, startup: list[dict] | None = None):
        self.startup = copy.deepcopy(startup or [])
        self.active: dict[str, dict] = {}
        self.reset()

    def reset(self) -> None:
        self.active = {}
        for it in self.startup:
            self.set(it["type"], it.get("params"))

    def set(self, ftype: object, params: object = None) -> dict:
        t, p = validate(ftype, params)
        self.active[t] = p
        return p

    def clear(self, ftype: str) -> bool:
        return self.active.pop(ftype, None) is not None

    def get(self, ftype: str) -> dict | None:
        return self.active.get(ftype)

    def fire(self, ftype: str) -> dict | None:
        """Use the fault once: returns its params (and counts it down), or None if inactive."""
        p = self.active.get(ftype)
        if p is None:
            return None
        if "count" in p:
            p["count"] -= 1
            if p["count"] <= 0:
                del self.active[ftype]
        return dict(p)

    def state_reasons(self) -> list[str]:
        return [
            f"{t}-{self.active[t]['severity']}"
            for t in sorted(STATE_REASON_FAULTS & set(self.active))
        ]

    def blocking(self) -> dict | None:
        """The first printer fault that stops processing, if any."""
        for t in sorted(STATE_REASON_FAULTS & set(self.active)):
            return self.active[t]
        return None

    def to_json(self) -> dict:
        return copy.deepcopy(self.active)
