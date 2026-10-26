"""Printing around a half-typed command.

Alerts and log lines print from the event loop while the console waits in
input() on another thread, so they used to land in the middle of whatever
the trader was typing and hide it -- 2026-10-23's 'trail untrail TTMI' was
an 'untrail' typed after a 'trail' that an alert had scrolled out of sight.

While a prompt is up, a background line now clears the input line, prints,
and redraws the prompt with what has been typed so far (readline keeps the
buffer), so the command being typed always sits whole on the last line.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading

try:  # line editing and history for input(); also the typed-so-far buffer
    import readline
except ImportError:  # pragma: no cover - always present on macOS/Linux
    readline = None  # type: ignore[assignment]

_lock = threading.Lock()
_prompt: str | None = None


def emit(text: str) -> None:
    """Print one background line without breaking the line being typed."""
    with _lock:
        if _prompt is None:
            print(text, flush=True)
            return
        typed = readline.get_line_buffer() if readline is not None else ""
        sys.stdout.write(f"\r\033[2K{text}\n{_prompt}{typed}")
        sys.stdout.flush()


async def prompt(loop: asyncio.AbstractEventLoop, text: str) -> str:
    """input(text) on an executor thread, with background lines printed
    above it rather than into it."""
    global _prompt
    with _lock:
        _prompt = text
    try:
        return await loop.run_in_executor(None, input, text)
    finally:
        with _lock:
            _prompt = None


class Handler(logging.StreamHandler):
    """Log records through `emit`, so an IBKR error doesn't land in the
    middle of a command either."""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            emit(self.format(record))
        except Exception:
            self.handleError(record)
