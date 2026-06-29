from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics, trace

try:
    from azure.monitor.opentelemetry import configure_azure_monitor
except Exception:  # pragma: no cover - optional dependency guard
    configure_azure_monitor = None


_CONFIG_LOCK = threading.Lock()
_CONFIGURED = False


@dataclass(frozen=True)
class TelemetryBundle:
    tracer: trace.Tracer
    requests: Any
    failures: Any
    latency_ms: Any
    prompt_tokens: Any
    completion_tokens: Any
    total_tokens: Any


def setup_telemetry(service_name: str) -> TelemetryBundle:
    global _CONFIGURED

    with _CONFIG_LOCK:
        if not _CONFIGURED:
            connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
            if connection_string and configure_azure_monitor is not None:
                configure_azure_monitor()
            _CONFIGURED = True

    meter = metrics.get_meter(service_name)
    tracer = trace.get_tracer(service_name)
    return TelemetryBundle(
        tracer=tracer,
        requests=meter.create_counter("workiq_requests_total"),
        failures=meter.create_counter("workiq_request_failures_total"),
        latency_ms=meter.create_histogram("workiq_request_latency_ms"),
        prompt_tokens=meter.create_counter("workiq_prompt_tokens_total"),
        completion_tokens=meter.create_counter("workiq_completion_tokens_total"),
        total_tokens=meter.create_counter("workiq_total_tokens_total"),
    )


def span_context_attributes(**attributes: Any) -> dict[str, Any]:
    return {key: value for key, value in attributes.items() if value is not None}


def extract_usage(response: Any) -> dict[str, int]:
    candidates = [
        response,
        getattr(response, "usage", None),
        getattr(response, "metadata", None),
        getattr(response, "result", None),
        getattr(response, "message", None),
        getattr(response, "data", None),
    ]

    prompt_tokens = _find_number(candidates, "prompt_tokens", "input_tokens")
    completion_tokens = _find_number(
        candidates, "completion_tokens", "output_tokens"
    )
    total_tokens = _find_number(candidates, "total_tokens")

    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens

    usage: dict[str, int] = {}
    if prompt_tokens is not None:
        usage["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        usage["completion_tokens"] = completion_tokens
    if total_tokens is not None:
        usage["total_tokens"] = total_tokens
    return usage


def record_usage(bundle: TelemetryBundle, response: Any, span: trace.Span | None = None) -> dict[str, int]:
    usage = extract_usage(response)
    if not usage:
        return usage

    attributes = {f"ai.{name}": value for name, value in usage.items()}
    if span is not None:
        for key, value in attributes.items():
            span.set_attribute(key, value)

    if "prompt_tokens" in usage:
        bundle.prompt_tokens.add(usage["prompt_tokens"])
    if "completion_tokens" in usage:
        bundle.completion_tokens.add(usage["completion_tokens"])
    if "total_tokens" in usage:
        bundle.total_tokens.add(usage["total_tokens"])

    return usage


def _find_number(candidates: list[Any], *names: str) -> int | None:
    for candidate in candidates:
        value = _lookup(candidate, *names)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _lookup(candidate: Any, *names: str) -> Any:
    if candidate is None:
        return None

    if isinstance(candidate, dict):
        for name in names:
            if name in candidate:
                return candidate[name]
        for value in candidate.values():
            nested = _lookup(value, *names)
            if nested is not None:
                return nested
        return None

    for name in names:
        if hasattr(candidate, name):
            return getattr(candidate, name)

    if hasattr(candidate, "model_dump"):
        try:
            return _lookup(candidate.model_dump(), *names)
        except Exception:
            return None

    if hasattr(candidate, "dict"):
        try:
            return _lookup(candidate.dict(), *names)
        except Exception:
            return None

    return None