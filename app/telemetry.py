"""Observability — every model decision emits a trace.

Phase 2 onward, the classification, extraction and compliance-gateway calls
each emit a Langfuse-style trace. ``telemetry_backend`` selects the sink:

- ``console`` (default, no network/keys): traces are rendered to stderr as
  structured JSON and retained in an in-memory ring buffer so tests can
  assert on them.
- ``langfuse``: forwards to a real Langfuse instance using the configured
  keys/host.

The in-memory buffer is the source of truth for acceptance checks: a pipeline
"works" only if the decision is reproducible, so every decorated call records
its inputs, the rule set / model name used, confidence, cost, and outcome.
"""

from __future__ import annotations

import json
import sys
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator
from uuid import uuid4

from app.config import settings

MAX_TRACES = 10_000


@dataclass
class Trace:
    trace_id: str
    name: str
    inputs: dict[str, Any]
    output: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_TRACES: deque[Trace] = deque(maxlen=MAX_TRACES)


def iter_traces() -> list[Trace]:
    return list(_TRACES)


def clear_traces() -> None:
    _TRACES.clear()


def find_trace(name: str, *, after: int = 0) -> Trace | None:
    matches = [t for t in _TRACES if t.name == name]
    if after >= len(matches):
        return None
    return matches[-1 - after]


class Tracer:
    """Base tracer. Concrete backends implement ``_emit``."""

    def __init__(self, name: str):
        self.name = name

    def span(self, name: str, **inputs: Any) -> "Span":
        trace = Trace(trace_id=str(uuid4()), name=name, inputs=inputs)
        return Span(self, trace)

    def _emit(self, trace: Trace) -> None:
        raise NotImplementedError


class Span:
    def __init__(self, tracer: Tracer, trace: Trace):
        self._tracer = tracer
        self._trace = trace

    def __enter__(self) -> "Span":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if exc_info and exc_info[0] is not None:  # exception inside
            self._trace.output = {
                **self._trace.output,
                "error": f"{exc_info[0].__name__}: {exc_info[1]}",
            } if self._trace.output else {
                "error": f"{exc_info[0].__name__}: {exc_info[1]}",
            }
        self.emit()

    def emit(self) -> None:
        """Emit the trace to the backend. Call after ``finish()`` when using
        the span directly rather than as a context manager."""
        self._tracer._emit(self._trace)

    def log_event(self, event: str, **fields: Any) -> None:
        self._trace.events.append({"event": event, **fields})

    def finish(
        self,
        output: dict[str, Any] | None = None,
        *,
        confidence: float | None = None,
        cost: float | None = None,
        model: str | None = None,
        **metadata: Any,
    ) -> None:
        self._trace.output = output
        if confidence is not None:
            metadata["confidence"] = confidence
        if cost is not None:
            metadata["cost"] = cost
        if model is not None:
            metadata["model"] = model
        self._trace.metadata.update(metadata)


class ConsoleTracer(Tracer):
    """Renders traces as JSON to stderr and keeps them in the buffer."""

    def _emit(self, trace: Trace) -> None:
        _TRACES.append(trace)
        body = {
            "trace_id": trace.trace_id,
            "name": trace.name,
            "inputs": trace.inputs,
            "output": trace.output,
            "events": trace.events,
            "metadata": trace.metadata,
        }
        print(json.dumps({"telemetry": "console", **body}, default=str, sort_keys=True), file=sys.stderr, flush=True)


class LangfuseTracer(Tracer):
    """Wraps the official Langfuse client (lazy import)."""

    def __init__(self):
        super().__init__("langfuse")
        from langfuse import Langfuse

        self._client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )

    def _emit(self, trace: Trace) -> None:
        _TRACES.append(trace)
        gen = self._client.generation(
            name=trace.name,
            input=trace.inputs,
            output=trace.output,
            metadata=trace.metadata,
            model=trace.metadata.get("model"),
            usage={
                "input": trace.metadata.get("input_cost", 0.0),
                "output": trace.metadata.get("output_cost", 0.0),
            },
        )
        for event in trace.events:
            gen.update(metadata={"event": event})
        self._client.flush()


_tracer: Tracer | None = None


def get_tracer() -> Tracer:
    global _tracer
    if _tracer is None:
        backend = settings.telemetry_backend.lower()
        if backend == "langfuse":
            _tracer = LangfuseTracer()
        else:
            _tracer = ConsoleTracer("console")
    return _tracer


@contextmanager
def traced(name: str, **inputs: Any) -> Iterator[Span]:
    """Convenience: ``with traced("classify", text=txt) as span: ... span.finish(...)``"""
    with get_tracer().span(name, **inputs) as span:
        yield span