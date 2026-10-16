"""Shares IBKR has to lend, so a short it can't borrow is known before the
order goes in rather than from its rejection (BSP, 2026-10-15: "The
contract is not available for short sale", twice).

The source is IBKR's public short-stock file: every symbol with shares to
lend, refreshed about every 15 minutes. A symbol missing from it has none.
It needs no market data subscription, unlike the shortable tick (generic
tick 236), which only comes on a streaming request -- and this login's
API gets snapshots only in regular hours.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
URL = "ftp://shortstock:@ftp2.interactivebrokers.com/usa.txt"
REFRESH_SECONDS = 15 * 60


def parse(text: str) -> tuple[datetime | None, dict[str, int]]:
    """(as of, shares available per US symbol). The file says '>10000000'
    for plenty; that reads as 10,000,001. Rows look like
    SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|."""
    as_of = None
    available: dict[str, int] = {}
    for line in text.splitlines():
        cols = line.split("|")
        if cols[0] == "#BOF" and len(cols) >= 3:
            try:
                as_of = datetime.strptime(f"{cols[1]} {cols[2]}", "%Y.%m.%d %H:%M:%S").replace(tzinfo=ET)
            except ValueError:
                pass
            continue
        if line.startswith("#") or len(cols) < 8 or cols[1] != "USD":
            continue
        n = cols[7].strip()
        try:
            available[cols[0]] = 10_000_001 if n.startswith(">") else int(n)
        except ValueError:
            continue
    return as_of, available


def _download() -> str:
    with urllib.request.urlopen(URL, timeout=60) as r:
        return r.read().decode("latin-1")


class ShortableList:
    """The latest copy of the file, kept fresh by `run`. Lookups never
    wait on the download (about 5s)."""

    def __init__(self) -> None:
        self.as_of: datetime | None = None
        self._available: dict[str, int] | None = None

    def available(self, symbol: str) -> int | None:
        """Shares IBKR has to lend in `symbol`: 0 when it is not in the
        file, None when the file hasn't been read yet."""
        if self._available is None:
            return None
        return self._available.get(symbol, 0)

    async def refresh(self) -> None:
        as_of, available = parse(await asyncio.to_thread(_download))
        if not available:
            raise ValueError("short-stock file had no US rows")
        self.as_of, self._available = as_of, available

    async def run(self) -> None:
        """Refresh forever; a failed download keeps the last copy."""
        while True:
            try:
                await self.refresh()
            except Exception as e:
                logger.warning("Shortable list not refreshed (%r); keeping the last one", e)
            await asyncio.sleep(REFRESH_SECONDS)
