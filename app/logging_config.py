"""Structured JSON logging configuration.

Sets up structlog as the unified logging entry point. All existing
stdlib `logging.getLogger(...)` calls are routed through structlog
via a handler bridge, so we don't have to rewrite call sites - they
automatically gain JSON output and request-scoped context binding.

Output is always JSON, in a shape designed to be ingested directly
by Loki / Grafana Cloud:

    {
      "timestamp": "2026-04-22T14:32:01.123456Z",
      "level": "info",
      "logger": "app.main",
      "event": "request_completed",
      "request_id": "a3f1c2d8",
      "user_id": null,
      "duration_ms": 25840,
      "status": "success",
      ...
    }

Field naming conventions (do not change without coordination):
    - "event" is the canonical name of what happened (snake_case verb_noun)
    - "request_id" uniquely identifies one HTTP request
    - "user_id" identifies the authenticated user (null for now,
      populated once per-user keys land)
    - duration fields are always milliseconds, suffixed _ms
    - error fields are { "error_type": "...", "error_message": "..." }

The Event constants below are the source of truth for event names.
Use them everywhere instead of string literals so dashboards stay
queryable as we evolve the service.
"""

import logging
import sys

import structlog


class Event:
    """Canonical event names. Using these consistently is what makes
    structured logs queryable in dashboards.
    """

    # Request lifecycle
    REQUEST_STARTED = "request_started"
    REQUEST_COMPLETED = "request_completed"
    REQUEST_FAILED = "request_failed"

    # URL handling
    URL_VALIDATED = "url_validated"
    URL_REJECTED = "url_rejected"

    # Download
    DOWNLOAD_STARTED = "download_started"
    DOWNLOAD_SUCCESS = "download_success"
    DOWNLOAD_FAILED = "download_failed"

    # Provider (Gemini, Qwen, future)
    PROVIDER_CALL_STARTED = "provider_call_started"
    PROVIDER_CALL_SUCCESS = "provider_call_success"
    PROVIDER_FALLBACK = "provider_fallback"  # structured -> freeform

    # Cache
    CACHE_INITIALIZED = "cache_initialized"
    CACHE_HIT = "cache_hit"
    CACHE_MISS = "cache_miss"
    CACHE_WRITE = "cache_write"
    CACHE_WRITE_FAILED = "cache_write_failed"


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog + stdlib logging to emit JSON.

    Idempotent - safe to call multiple times (FastAPI reloads etc).
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # The shared chain of processors. Both structlog-native loggers
    # and stdlib loggers feed through these.
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,  # pulls in request_id etc.
        structlog.stdlib.add_logger_name,  # populates "logger" field
        structlog.stdlib.add_log_level,  # populates "level" field
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,  # renders exc_info if present
    ]

    # Configure structlog itself.
    structlog.configure(
        processors=shared_processors
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Bridge stdlib logging into structlog's processor chain.
    # `extra={...}` kwargs from logger.info("event", extra={...}) get
    # picked up and merged into the JSON output automatically.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    # Replace existing handlers so we don't end up with duplicate output
    # if uvicorn has already configured one.
    root_logger.handlers = [handler]
    root_logger.setLevel(log_level)

    # uvicorn's loggers default to their own formatters; route them through
    # ours for consistency.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(noisy)
        uv_logger.handlers = []
        uv_logger.propagate = True


def bind_request_context(**kwargs: object) -> None:
    """Attach key/value pairs to every log line emitted by the current task.

    Uses contextvars under the hood, which means it's task-local in async
    code - one request's bindings don't leak into another's even when
    they run concurrently.
    """
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_request_context() -> None:
    """Clear all bound context. Called at the end of each request."""
    structlog.contextvars.clear_contextvars()
