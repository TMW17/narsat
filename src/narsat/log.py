"""Structured logging.

JSON lines in containers, a readable console renderer on a terminal. One processor
redacts secrets, because the FIRMS API puts the MAP_KEY in the URL path and HTTP
client errors like to quote URLs.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_FIRMS_KEY_IN_PATH = re.compile(r"(/api/[a-z_]+/csv/)[0-9A-Za-z]{16,}")
_FIRMS_KEY_IN_QUERY = re.compile(r"(MAP_KEY=)[0-9A-Za-z]{16,}")
_URL_PASSWORD = re.compile(r"(://[^:/@\s]+:)[^@/\s]+(@)")
_SENSITIVE_KEYS = ("password", "secret", "token", "map_key", "authorization", "dsn")


def redact(text: str) -> str:
    """Remove credentials that can appear inside URLs and connection strings."""
    text = _FIRMS_KEY_IN_PATH.sub(r"\1***", text)
    text = _FIRMS_KEY_IN_QUERY.sub(r"\1***", text)
    return _URL_PASSWORD.sub(r"\1***\2", text)


def _redact_event(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key, value in list(event_dict.items()):
        if any(marker in key.lower() for marker in _SENSITIVE_KEYS):
            event_dict[key] = "***"
        elif isinstance(value, str):
            event_dict[key] = redact(value)
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    """Configure structlog and route the standard library loggers through it."""
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _redact_event,
    ]
    renderer: Any
    if json_output:
        shared.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Third-party chatter that hides the pipeline's own messages.
    for noisy in ("httpx", "httpcore", "rasterio", "botocore", "pyiceberg", "alembic.runtime"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
