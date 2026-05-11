from __future__ import annotations

import telemetry


def test_configure_telemetry_is_idempotent(monkeypatch):
    calls = {"trace": 0, "metrics": 0}

    class FakeTracerProvider:
        def __init__(self, resource=None):
            self.resource = resource
            self.span_processors = []

        def add_span_processor(self, processor):
            self.span_processors.append(processor)

    class FakeMeterProvider:
        def __init__(self, resource=None, metric_readers=None):
            self.resource = resource
            self.metric_readers = metric_readers or []

    class FakeBatchSpanProcessor:
        def __init__(self, exporter):
            self.exporter = exporter

    class FakeMetricReader:
        def __init__(self, exporter):
            self.exporter = exporter

    class FakeSpanExporter:
        def __init__(self, *args, **kwargs):
            pass

    class FakeMetricExporter:
        def __init__(self, *args, **kwargs):
            pass

    class _FakeSpan:
        def start_as_current_span(self, name, attributes=None):
            from contextlib import nullcontext

            return nullcontext()

    class _FakeMeter:
        def create_counter(self, name):
            return _FakeCounter()

        def create_histogram(self, name):
            return _FakeHistogram()

    class _FakeCounter:
        def add(self, amount, attributes=None):
            return None

    class _FakeHistogram:
        def record(self, amount, attributes=None):
            return None

    def fake_set_tracer_provider(provider):
        calls["trace"] += 1

    def fake_set_meter_provider(provider):
        calls["metrics"] += 1

    monkeypatch.setattr(telemetry, "_otel_initialized", False)
    monkeypatch.setattr(telemetry.telemetry, "enabled", False)
    monkeypatch.setattr("opentelemetry.sdk.trace.TracerProvider", FakeTracerProvider)
    monkeypatch.setattr("opentelemetry.sdk.trace.export.BatchSpanProcessor", FakeBatchSpanProcessor)
    monkeypatch.setattr("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter", FakeSpanExporter)
    monkeypatch.setattr("opentelemetry.sdk.metrics.MeterProvider", FakeMeterProvider)
    monkeypatch.setattr("opentelemetry.sdk.metrics.export.PeriodicExportingMetricReader", FakeMetricReader)
    monkeypatch.setattr("opentelemetry.exporter.otlp.proto.grpc.metric_exporter.OTLPMetricExporter", FakeMetricExporter)
    monkeypatch.setattr("opentelemetry.trace.set_tracer_provider", fake_set_tracer_provider)
    monkeypatch.setattr("opentelemetry.metrics.set_meter_provider", fake_set_meter_provider)
    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda name: _FakeSpan())
    monkeypatch.setattr("opentelemetry.metrics.get_meter", lambda name: _FakeMeter())
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    telemetry.configure_telemetry("ses-bounce-webhook")
    telemetry.configure_telemetry("ses-bounce-webhook")

    assert calls == {"trace": 1, "metrics": 1}
    assert telemetry.telemetry.enabled is True
