"""Trace correlation contracts.

One AOI's journey is five processes, four Redis streams, and minutes to days of
wall-clock. `run_id` is what makes it queryable as a unit. These tests pin the four
properties that make it trustworthy — each of which, if broken, degrades silently
rather than failing:

* **It reaches code that knows nothing about it.** The value of a contextvar-based
  approach is that `app/eo/*` gets correlated for free; a regression to explicit
  threading would leave those lines uncorrelated and nobody would notice until an
  incident.
* **Concurrent runs stay separate.** `--concurrency 4` and
  `run_for_subscriber`'s gather both run several traces in one process. A
  module-level global would interleave them and produce logs that look correct and
  attribute work to the wrong area.
* **It survives every hand-off.** A single missed copy truncates the trace at that
  stage — invisible until someone follows a run and finds it stops after Analyst.
* **Legacy envelopes still parse.** A required field would dead-letter every job
  already on a stream at deploy time, silently dropping the in-flight scans of
  every active subscriber.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import pathlib
import re

from app import tracing
from app.logging_config import HumanFormatter, JsonFormatter
from app.models.enums import JobStage
from app.models.schemas import JobEnvelope
from app.tracing import TraceFilter


def _capture(formatter: logging.Formatter) -> tuple[io.StringIO, logging.Logger]:
    """A logger wired exactly as `configure_logging` wires the real one."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(formatter)
    handler.addFilter(TraceFilter())

    logger = logging.getLogger(f"test.trace.{id(buf)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return buf, logger


# --------------------------------------------------------------------------- #
# Injection
# --------------------------------------------------------------------------- #


def test_trace_reaches_callers_that_never_mention_it():
    """The whole reason for a contextvar rather than a parameter.

    `app/eo/cog.py` logs windowed reads and has no idea a pipeline exists. If this
    breaks, every adapter log line silently loses its correlation.
    """
    buf, logger = _capture(JsonFormatter())

    with tracing.trace("run_deadbeef", stage="analyst", aoi_id="aoi_1"):
        # No extra= at all — the point is that none is needed.
        logger.info("windowed read")

    record = json.loads(buf.getvalue())
    assert record["run_id"] == "run_deadbeef"
    assert record["stage"] == "analyst"
    assert record["aoi_id"] == "aoi_1"


def test_no_trace_fields_outside_a_run():
    """API handlers and startup must not gain phantom fields."""
    buf, logger = _capture(JsonFormatter())

    logger.info("serving request")

    record = json.loads(buf.getvalue())
    assert "run_id" not in record
    assert "stage" not in record
    assert "aoi_id" not in record


def test_explicit_extra_wins_over_the_contextvar():
    """`Agent.execute` already passes a stage; its value is the more specific.

    A filter that clobbered `extra=` would silently discard per-call detail.
    """
    buf, logger = _capture(JsonFormatter())

    with tracing.trace("run_1", stage="oracle", aoi_id="aoi_1"):
        logger.info("done", extra={"stage": "oracle-detail", "aoi_id": "aoi_override"})

    record = json.loads(buf.getvalue())
    assert record["stage"] == "oracle-detail"
    assert record["aoi_id"] == "aoi_override"
    # run_id is ours alone and is always injected.
    assert record["run_id"] == "run_1"


def test_human_formatter_appends_the_trace():
    buf, logger = _capture(HumanFormatter())

    with tracing.trace("run_abc"):
        logger.info("scanning")

    assert "[run_abc]" in buf.getvalue()


def test_human_formatter_does_not_break_untraced_lines():
    """A `%(run_id)s` in the format string would raise KeyError on every line
    logged outside a run — which is most of them at startup."""
    buf, logger = _capture(HumanFormatter())

    logger.info("no trace active")

    output = buf.getvalue()
    assert "no trace active" in output
    assert "[" not in output.split("no trace active")[-1]


def test_filter_never_drops_records():
    """It annotates; it must not filter. A dropped log line during an incident is
    worse than an uncorrelated one."""
    assert TraceFilter().filter(
        logging.LogRecord("x", logging.INFO, "f", 1, "m", (), None)
    ) is True


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #


async def test_concurrent_runs_do_not_interleave():
    """`--concurrency 4` and `run_for_subscriber`'s gather both do this.

    A module-level global would produce logs that look right and attribute work to
    the wrong area — the worst kind of observability bug, because it misleads
    rather than merely omitting.
    """
    buf, logger = _capture(JsonFormatter())

    async def one(run_id: str, aoi: str) -> None:
        with tracing.trace(run_id, stage="analyst", aoi_id=aoi):
            logger.info("start")
            # Yield, so the other task definitely runs in between.
            await asyncio.sleep(0)
            logger.info("finish")

    await asyncio.gather(one("run_A", "aoi_A"), one("run_B", "aoi_B"))

    records = [json.loads(line) for line in buf.getvalue().strip().splitlines()]
    assert len(records) == 4

    # Every line's aoi must match its own run — the pairing is what would break.
    for record in records:
        expected = "aoi_A" if record["run_id"] == "run_A" else "aoi_B"
        assert record["aoi_id"] == expected, f"trace bled across tasks: {record}"


def test_trace_restores_the_previous_value():
    """Nesting must be safe: a worker processing job A then job B must not leak
    A's id into B's logs."""
    with tracing.trace("outer"):
        assert tracing.current_run_id() == "outer"
        with tracing.trace("inner"):
            assert tracing.current_run_id() == "inner"
        assert tracing.current_run_id() == "outer"

    assert tracing.current_run_id() is None


def test_none_run_id_binds_nothing():
    """Replaying a pre-upgrade envelope passes None. It must be a no-op rather than
    binding the literal string 'None'."""
    with tracing.trace(None, stage="scout"):
        assert tracing.current_run_id() is None
        assert tracing.current_stage() == "scout"


# --------------------------------------------------------------------------- #
# Propagation across hand-offs
# --------------------------------------------------------------------------- #


def test_envelope_run_id_is_optional():
    """A required field would fail `model_validate_json` on every envelope already
    on a Redis stream, dead-lettering the in-flight scans of every subscriber at
    deploy time."""
    legacy = json.dumps(
        {"id": "job_x", "stage": "scout", "status": "queued", "payload": {}}
    )
    job = JobEnvelope.model_validate_json(legacy)

    assert job.run_id is None
    assert job.stage is JobStage.SCOUT


def test_hand_off_helper_carries_the_trace():
    """Every stage transition goes through `worker._next`.

    Loaded by AST rather than import: `app.queue.worker` pulls in `app.eo.cog`,
    which needs GDAL — and the point of this test is that it runs anywhere.
    """
    import ast

    tree = ast.parse(pathlib.Path("app/queue/worker.py").read_text())
    fn = next(n for n in tree.body if getattr(n, "name", "") == "_next")
    namespace: dict = {"JobEnvelope": JobEnvelope, "JobStage": JobStage}
    exec(  # noqa: S102 — executing our own source, in-process, for one function
        compile(
            ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
            "<worker>",
            "exec",
        ),
        namespace,
    )

    source = JobEnvelope(
        stage=JobStage.SCOUT,
        subscriber_id="sub_1",
        aoi_id="aoi_1",
        run_id="run_keepme",
        payload={"aoi": {}},
    )
    successor = namespace["_next"](source, JobStage.ANALYST, {"x": 1})

    assert successor.run_id == "run_keepme", "the trace was dropped at hand-off"
    assert successor.stage is JobStage.ANALYST
    assert successor.subscriber_id == "sub_1"
    assert successor.aoi_id == "aoi_1"
    # A new queue entry, deliberately: job.id identifies one stream message,
    # run_id identifies the whole journey.
    assert successor.id != source.id


def test_worker_builds_every_successor_through_the_helper():
    """Structural guard on the reason the helper exists.

    A hand-written `JobEnvelope(stage=...)` in a handler would compile, run, and
    silently truncate the trace. The only permitted construction site is `_next`.
    """
    source = pathlib.Path("app/queue/worker.py").read_text()

    constructions = re.findall(r"JobEnvelope\(", source)
    assert len(constructions) == 1, (
        "worker.py should construct JobEnvelope exactly once (inside `_next`); "
        f"found {len(constructions)}. A hand-built envelope drops run_id."
    )


def test_both_enqueue_paths_mint_a_trace():
    """A scan or verification queued without a trace is unfollowable, and nothing
    at runtime would complain."""
    source = pathlib.Path("app/agents/pipeline.py").read_text()

    for function in ("enqueue_scan", "enqueue_verification"):
        body = source.split(f"async def {function}(")[1].split("\nasync def ")[0]
        assert "tracing.new_run_id()" in body, f"{function} does not mint a run_id"
        assert "run_id=run_id" in body, f"{function} does not set it on the envelope"


def test_inline_path_is_traced_too():
    """`POST /risk/assess` runs the same stages synchronously and should be just as
    followable as a scheduled scan."""
    source = pathlib.Path("app/agents/pipeline.py").read_text()

    for function in ("assess", "run_inline"):
        body = source.split(f"async def {function}(")[1].split("\nasync def ")[0]
        assert "tracing.trace(" in body, f"{function} is untraced"


def test_run_id_is_short_enough_to_live_on_every_line():
    """It appears on every log line of a multi-minute run across five processes.
    A full UUID is 4x the bytes for no practical gain."""
    run_id = tracing.new_run_id()

    assert run_id.startswith("run_")
    assert len(run_id) == len("run_") + 12
    # And distinct across calls, which is the only property that matters.
    assert tracing.new_run_id() != run_id
