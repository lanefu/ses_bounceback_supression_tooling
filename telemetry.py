#!/usr/bin/env python3

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator, Optional


class _NoopCounter:
    def add(self, amount: int, attributes: Optional[dict[str, str]] = None) -> None:
        return None


class _NoopHistogram:
    def record(self, amount: float, attributes: Optional[dict[str, str]] = None) -> None:
        return None


class Telemetry:
    def __init__(self) -> None:
        self.enabled = False
        self.sns_messages = _NoopCounter()
        self.sns_verification_failures = _NoopCounter()
        self.bounce_events = _NoopCounter()
        self.bounce_recipients = _NoopCounter()
        self.aws_suppression_attempts = _NoopCounter()
        self.webhook_latency = _NoopHistogram()

    @contextmanager
    def span(self, name: str, attributes: Optional[dict[str, str]] = None) -> Iterator[None]:
        yield

    @contextmanager
    def latency(self, histogram: _NoopHistogram, attributes: Optional[dict[str, str]] = None) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            histogram.record(time.monotonic() - started, attributes or {})


telemetry = Telemetry()
_otel_initialized = False


def configure_telemetry(service_name: str, endpoint: Optional[str] = None, resource_attributes: Optional[str] = None) -> None:
    global _otel_initialized
    configured_endpoint = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not configured_endpoint:
        os.environ.setdefault("OTEL_SERVICE_NAME", service_name)
        return
    os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", configured_endpoint)
    if resource_attributes:
        os.environ.setdefault("OTEL_RESOURCE_ATTRIBUTES", resource_attributes)
    os.environ.setdefault("OTEL_SERVICE_NAME", service_name)
    if _otel_initialized:
        return

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except Exception:
        return

    resource = Resource.create({"service.name": service_name})
    try:
        trace_provider = TracerProvider(resource=resource)
        trace_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(trace_provider)

        metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))
        _otel_initialized = True
    except Exception:
        return

    meter = metrics.get_meter("ses_bounce_webhook")
    telemetry.enabled = True
    telemetry.sns_messages = meter.create_counter("ses_bounce_sns_messages_total")
    telemetry.sns_verification_failures = meter.create_counter("ses_bounce_sns_verification_failures_total")
    telemetry.bounce_events = meter.create_counter("ses_bounce_events_total")
    telemetry.bounce_recipients = meter.create_counter("ses_bounce_recipients_total")
    telemetry.aws_suppression_attempts = meter.create_counter("ses_bounce_aws_suppression_attempts_total")
    telemetry.webhook_latency = meter.create_histogram("ses_bounce_webhook_processing_seconds")

    tracer = trace.get_tracer("ses_bounce_webhook")

    @contextmanager
    def real_span(name: str, attributes: Optional[dict[str, str]] = None) -> Iterator[None]:
        with tracer.start_as_current_span(name, attributes=attributes or {}):
            yield

    telemetry.span = real_span  # type: ignore[method-assign]


def instrument_fastapi(app) -> None:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except Exception:
        return
    FastAPIInstrumentor.instrument_app(app)
