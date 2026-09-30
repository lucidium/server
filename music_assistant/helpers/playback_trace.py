"""Opt-in playback timings without provider credentials or signed resource paths."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import aiohttp

TRACE_ENABLED = os.environ.get("MA_PLAYBACK_TRACE") == "1"
LOGGER = logging.getLogger("music_assistant.playback_trace")
if TRACE_ENABLED:
    LOGGER.setLevel(logging.INFO)

type TraceValue = str | int | float | bool | None


@dataclass(frozen=True)
class PlaybackTrace:
    """Correlation information inherited by a playback action's child tasks."""

    trace_id: str
    started: float


_TRACE: ContextVar[PlaybackTrace | None] = ContextVar("playback_trace", default=None)


def trace_event(event: str, **fields: TraceValue) -> None:
    """Log an event containing only explicitly selected diagnostic fields."""
    if not TRACE_ENABLED:
        return
    now = time.monotonic()
    context = _TRACE.get()
    LOGGER.info(
        "PLAYBACK_TRACE %s",
        json.dumps(
            {
                "event": event,
                "trace": context.trace_id if context else None,
                "monotonic_ms": round(now * 1000, 1),
                "elapsed_ms": round((now - context.started) * 1000, 1) if context else None,
                **fields,
            },
            separators=(",", ":"),
        ),
    )


@contextmanager
def trace_stage(stage: str, **fields: TraceValue) -> Iterator[None]:
    """Time a playback stage, preserving exceptions and nested correlation."""
    if not TRACE_ENABLED:
        yield
        return
    started = time.monotonic()
    token = None
    if _TRACE.get() is None:
        token = _TRACE.set(PlaybackTrace(uuid4().hex[:12], started))
    trace_event(stage + ".start", **fields)
    try:
        yield
    except BaseException as err:
        trace_event(
            stage + ".error",
            duration_ms=round((time.monotonic() - started) * 1000, 1),
            error_type=type(err).__name__,
            **fields,
        )
        raise
    else:
        trace_event(
            stage + ".done",
            duration_ms=round((time.monotonic() - started) * 1000, 1),
            **fields,
        )
    finally:
        if token is not None:
            _TRACE.reset(token)


def trace_url(value: str, base: str | None = None) -> str:
    """Return a URL's origin, omitting credentials, signed paths, query and fragment."""
    try:
        parsed = urlsplit(urljoin(base, value) if base is not None else value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return "<local-or-pipe>"
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}/<redacted>"
    except ValueError:
        return "<invalid-url>"


def create_playback_http_trace() -> aiohttp.TraceConfig:
    """Trace provider HTTP stages only inside a playback action context."""
    config = aiohttp.TraceConfig()

    async def request_start(
        _session: aiohttp.ClientSession, context: SimpleNamespace, params: Any
    ) -> None:
        context.playback = _TRACE.get()
        if context.playback is None:
            return
        context.request_id = uuid4().hex[:12]
        context.started = time.monotonic()
        trace_event(
            "http.start",
            request_id=context.request_id,
            method=params.method,
            url=trace_url(str(params.url)),
        )

    async def request_end(
        _session: aiohttp.ClientSession, context: SimpleNamespace, params: Any
    ) -> None:
        if getattr(context, "playback", None) is None:
            return
        trace_event(
            "http.headers",
            request_id=context.request_id,
            status=params.response.status,
            url=trace_url(str(params.url)),
            duration_ms=round((time.monotonic() - context.started) * 1000, 1),
        )

    async def request_redirect(
        _session: aiohttp.ClientSession, context: SimpleNamespace, params: Any
    ) -> None:
        if getattr(context, "playback", None) is None:
            return
        destination = params.response.headers.get("Location", "")
        trace_event(
            "http.redirect",
            request_id=context.request_id,
            status=params.response.status,
            url=trace_url(str(params.url)),
            destination=trace_url(destination, base=str(params.url)),
        )

    async def request_exception(
        _session: aiohttp.ClientSession, context: SimpleNamespace, params: Any
    ) -> None:
        if getattr(context, "playback", None) is None:
            return
        trace_event(
            "http.error",
            request_id=context.request_id,
            error_type=type(params.exception).__name__,
            duration_ms=round((time.monotonic() - context.started) * 1000, 1),
        )

    def phase_callback(phase: str) -> Any:
        async def callback(
            _session: aiohttp.ClientSession, context: SimpleNamespace, _params: Any
        ) -> None:
            if getattr(context, "playback", None) is not None:
                trace_event("http." + phase, request_id=context.request_id)

        return callback

    config.on_request_start.append(request_start)
    config.on_request_end.append(request_end)
    config.on_request_redirect.append(request_redirect)
    config.on_request_exception.append(request_exception)
    for signal, phase in (
        (config.on_connection_queued_start, "pool_wait.start"),
        (config.on_connection_queued_end, "pool_wait.done"),
        (config.on_connection_create_start, "connect.start"),
        (config.on_connection_create_end, "connect.done"),
        (config.on_connection_reuseconn, "connection_reused"),
        (config.on_dns_resolvehost_start, "dns.start"),
        (config.on_dns_resolvehost_end, "dns.done"),
        (config.on_dns_cache_hit, "dns.cached"),
    ):
        signal.append(phase_callback(phase))
    return config


def trace_ffmpeg_line(line: str, pid: int | None) -> bool:
    """Log allowed HTTP fields and return whether an event was emitted."""
    if not TRACE_ENABLED:
        return False
    if match := re.search(r"Opening '(https?://[^']+)' for reading", line):
        trace_event("ffmpeg.open", pid=pid, url=trace_url(match[1]))
    elif match := re.search(r"\brequest: (GET|HEAD|POST) ", line):
        trace_event("ffmpeg.http.request", pid=pid, method=match[1])
    elif match := re.fullmatch(r"Host: ([A-Za-z0-9.\[\]:_-]+)\s*", line):
        trace_event("ffmpeg.http.host", pid=pid, host=match[1])
    elif match := re.search(r"Location: (https?://[^\s\x27]+)", line):
        trace_event("ffmpeg.http.redirect", pid=pid, url=trace_url(match[1]))
    elif match := re.fullmatch(r"Range: bytes=(\d+)-(\d*)\s*", line):
        trace_event("ffmpeg.http.range", pid=pid, start=int(match[1]), end=match[2])
    elif match := re.search(r"(?:\bHTTP/\d(?:\.\d)? |\bHTTP error |\bhttp_code=)(\d{3})\b", line):
        trace_event("ffmpeg.http.status", pid=pid, status=int(match[1]))
    elif match := re.search(
        r"(Starting connection attempt|Successfully connected) to ([0-9a-fA-F.:]+) port (\d+)",
        line,
    ):
        trace_event("ffmpeg.connect", pid=pid, phase=match[1], address=match[2], port=int(match[3]))
    elif "Will reconnect" in line or "Connection timed out" in line:
        trace_event("ffmpeg.reconnect" if "Will reconnect" in line else "ffmpeg.timeout", pid=pid)
    else:
        return False
    return True
