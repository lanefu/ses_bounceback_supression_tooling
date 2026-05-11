from __future__ import annotations

import json
import logging

import pytest

from logging_config import JsonFormatter, TextFormatter


pytest.importorskip("opentelemetry")
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState, use_span


def _active_span_context() -> NonRecordingSpan:
    span_context = SpanContext(
        trace_id=0x1234567890ABCDEF1234567890ABCDEF,
        span_id=0x1234567890ABCDEF,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    return NonRecordingSpan(span_context)


def _record(message: str = "hello") -> logging.LogRecord:
    return logging.LogRecord(
        name="ses_bounce.web",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_json_formatter_includes_trace_context_only_when_span_is_active() -> None:
    formatter = JsonFormatter()

    outside = json.loads(formatter.format(_record("outside")))
    assert "trace_id" not in outside
    assert "span_id" not in outside

    with use_span(_active_span_context(), end_on_exit=False):
        inside = json.loads(formatter.format(_record("inside")))

    assert inside["trace_id"] == "1234567890abcdef1234567890abcdef"
    assert inside["span_id"] == "1234567890abcdef"


def test_text_formatter_appends_trace_context_only_when_span_is_active() -> None:
    formatter = TextFormatter("%(levelname)s:%(name)s:%(message)s")

    outside = formatter.format(_record("outside"))
    assert outside == "INFO:ses_bounce.web:outside"

    with use_span(_active_span_context(), end_on_exit=False):
        inside = formatter.format(_record("inside"))

    assert inside == "INFO:ses_bounce.web:inside trace_id=1234567890abcdef1234567890abcdef span_id=1234567890abcdef"
