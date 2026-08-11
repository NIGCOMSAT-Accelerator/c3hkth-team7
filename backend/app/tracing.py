"""Trace correlation across pipeline stages.

One AOI's journey is five processes, four Redis streams, and minutes to days of
wall-clock. Reconstructing it from logs previously meant grepping `aoi_id` and
hoping — which breaks the moment two subscribers watch the same area, or the same
area is scanned twice in a day.

A `run_id` fixes that: minted once when the scheduler enqueues a scan, carried on
the `JobEnvelope` through every hand-off, and stamped on every log line the stage
emits.

**Why a contextvar rather than threading it through call signatures.** There are
~80 `log.*(..., extra={...})` calls across the agents, EO adapters and dispatchers.
Passing a trace id to each would mean touching every one, and the next one written
would forget. A `ContextVar` plus a logging filter injects it into *every* record
automatically — including those in `app/eo/`, which have no idea a pipeline exists.

`ContextVar` is the right primitive because `asyncio` copies the context per task:
the worker runs several consumers concurrently, and each gets its own value with no
locking and no leakage between jobs. A module-level global would interleave.

Cost is a dict lookup per log record. Nothing is serialised, nothing is stored, and
when no trace is active the fields are simply absent.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

#: The current trace. `None` outside a pipeline run — API request handlers, the
#: scheduler's own bookkeeping, startup.
_run_id: ContextVar[str | None] = ContextVar("shelter_run_id", default=None)

#: Which stage is executing. Redundant with the logger name for agents, but the EO
#: adapters and dispatchers log under their own names, and knowing a COG read
#: happened *during Analyst* is the useful fact.
_stage: ContextVar[str | None] = ContextVar("shelter_stage", default=None)

#: The AOI this run concerns. Already passed explicitly by most agent log calls,
#: but not by the adapters underneath them.
_aoi_id: ContextVar[str | None] = ContextVar("shelter_aoi_id", default=None)


def new_run_id() -> str:
    """Mint a trace id.

    Short (12 hex chars) on purpose: it appears on every log line of a multi-minute
    run across five processes, and a full UUID is four times the bytes for no extra
    practical uniqueness. 12 hex chars is 48 bits — at this volume, collision is not
    a real concern, and a collision costs a confusing log query rather than
    incorrect behaviour.
    """
    return f"run_{uuid.uuid4().hex[:12]}"


def current_run_id() -> str | None:
    return _run_id.get()


def current_stage() -> str | None:
    return _stage.get()


@contextmanager
def trace(
    run_id: str | None,
    *,
    stage: str | None = None,
    aoi_id: str | None = None,
):
    """Bind a trace for the duration of a block.

    Restores the previous values on exit, so nesting is safe and a worker that
    processes job A then job B cannot leak A's id into B's logs.

    A `None` run_id is accepted and binds nothing — that keeps callers free of
    `if run_id is not None` noise when replaying an envelope written before this
    field existed.
    """
    tokens = []
    if run_id is not None:
        tokens.append((_run_id, _run_id.set(run_id)))
    if stage is not None:
        tokens.append((_stage, _stage.set(stage)))
    if aoi_id is not None:
        tokens.append((_aoi_id, _aoi_id.set(aoi_id)))

    try:
        yield run_id
    finally:
        # Reset in reverse, which is what ContextVar tokens require.
        for var, token in reversed(tokens):
            var.reset(token)


class TraceFilter(logging.Filter):
    """Injects the active trace into every log record.

    A `Filter` rather than a `LogRecordFactory` because a factory is global process
    state that any other library can overwrite; a filter is attached to our handler
    and stays ours.

    Returns True always — this filter annotates, it never drops records.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        run_id = _run_id.get()
        if run_id is not None:
            record.run_id = run_id

        stage = _stage.get()
        if stage is not None:
            # Do not clobber an explicit `extra={"stage": ...}`. Agent.execute
            # already passes one, and its value is the more specific.
            if not hasattr(record, "stage"):
                record.stage = stage

        aoi_id = _aoi_id.get()
        if aoi_id is not None and not hasattr(record, "aoi_id"):
            record.aoi_id = aoi_id

        return True
