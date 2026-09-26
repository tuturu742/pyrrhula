"""OpenTelemetry wiring, on from day one.

Per-turn traces are how context assembly gets debugged  — this is not
premature instrumentation, it's the only way anyone will understand why a given turn's
context looked the way it did. Exporter is OTLP over gRPC/HTTP when
``PYRRHULA_OTEL_EXPORTER_ENDPOINT`` is set; otherwise spans are created against a no-op
tracer provider so importing this module never requires a collector to be running.
"""

from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SpanExporter,
)

_configured = False


def configure_tracing(service_name: str) -> None:
    """Idempotent: safe to call from api/main.py and worker/main.py alike."""
    global _configured
    if _configured:
        return
    _configured = True

    resource = Resource.create({SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)

    endpoint = os.environ.get("PYRRHULA_OTEL_EXPORTER_ENDPOINT")
    exporter: SpanExporter | None
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter(endpoint=endpoint)
    else:
        exporter = ConsoleSpanExporter() if os.environ.get("PYRRHULA_OTEL_DEBUG") else None

    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)
