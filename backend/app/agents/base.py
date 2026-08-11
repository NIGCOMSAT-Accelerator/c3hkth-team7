"""Agent contract.

Each of the four agents is a self-contained stage: it takes the previous
stage's result, does one job, and hands off. They never call each other
directly — the queue is the only coupling, which is what lets a slow inference
stage back up without stalling discovery.
"""

from __future__ import annotations

import abc
import time
from typing import Any, Generic, TypeVar

from app.logging_config import get_logger
from app.models.enums import JobStage

TIn = TypeVar("TIn")
TOut = TypeVar("TOut")


class Agent(abc.ABC, Generic[TIn, TOut]):
    """One pipeline stage."""

    #: Which stream this agent consumes.
    stage: JobStage
    #: Where its output goes; None means end of pipeline.
    next_stage: JobStage | None = None

    def __init__(self) -> None:
        self.log = get_logger(f"agent.{self.stage.value}")

    @abc.abstractmethod
    async def run(self, payload: TIn) -> TOut:
        """Do the work. Raise to trigger the broker's retry path."""

    async def execute(self, payload: TIn) -> TOut:
        """`run` with timing and failure logging wrapped around it."""
        started = time.perf_counter()
        try:
            result = await self.run(payload)
        except Exception:
            self.log.exception(
                "agent failed",
                extra={
                    "stage": self.stage.value,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000),
                },
            )
            raise

        self.log.info(
            "agent completed",
            extra={
                "stage": self.stage.value,
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
            },
        )
        return result


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Keep a score inside its declared range.

    Used liberally — several risk terms are ratios that can drift slightly out
    of bounds through interpolation, and a score of 1.02 would fail validation
    at the API boundary rather than where it was produced.
    """
    return max(low, min(high, value))


def first_or_none(items: list[Any]) -> Any | None:
    return items[0] if items else None
