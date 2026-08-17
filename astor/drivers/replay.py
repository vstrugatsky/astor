"""Replay driver: fetch a market day from IBKR, feed it to the Scanner as a
time-ordered stream of completed bars.

The driver owns the dataset; the Scanner never sees it. Bars from all tickers
are merged into a single chronological stream, so the Scanner observes the same
interleaving it would see live rather than one ticker's whole day at a time.

Bars are cached on disk per (symbol, session, bar size), so iterating on setup
rules does not re-hit IBKR and replays work offline.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path

from ib_async import IB, Stock

from astor.core.scanner import Scanner
from astor.core.types import BarEvent, DailyLevels

logger = logging.getLogger(__name__)

ET = __import__("zoneinfo").ZoneInfo("America/New_York")

# ib_async returns [] instead of raising when its own timeout fires, so every
# request passes an explicit timeout and empty results are treated as failures.
REQUEST_TIMEOUT = 60.0

ADR_SESSIONS = 14

# A cache entry written before ADR existed lacks these, so it must be refetched
# rather than loaded with zeroed volatility -- which would silently disable the
# filters instead of failing visibly.
REQUIRED_LEVEL_KEYS = {"session", "pdh", "pdl", "adr_pct", "prev_close", "adr_sessions"}


def compute_adr_pct(daily_bars, sessions: int = ADR_SESSIONS) -> tuple[float, int]:
    """Deepvue-compatible ADR: mean of (High / Low - 1) over the last N sessions.

    Returns (adr_pct, sessions_used). Bars with a non-positive low are skipped
    rather than producing a division error; a symbol with no usable bars gets
    0.0, which the setups read as "unscalable" and skip filtering for.
    """
    ratios = [
        b.high / b.low - 1.0
        for b in daily_bars[-sessions:]
        if b.low and b.low > 0 and b.high and b.high > 0
    ]
    if not ratios:
        return 0.0, 0
    return round(sum(ratios) / len(ratios) * 100.0, 4), len(ratios)


def parse_bar_seconds(bar_size: str) -> int:
    parts = bar_size.strip().split()
    if len(parts) != 2:
        raise ValueError(f"Unrecognized bar size: {bar_size!r}")
    n, unit = int(parts[0]), parts[1].rstrip("s")
    factor = {"sec": 1, "min": 60, "hour": 3600}.get(unit)
    if factor is None:
        raise ValueError(f"Unrecognized bar size unit: {unit!r}")
    return n * factor


def to_et(dt) -> datetime | None:
    if not isinstance(dt, datetime):
        return None
    return dt.replace(tzinfo=ET) if dt.tzinfo is None else dt.astimezone(ET)


class BarCache:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, kind: str, symbol: str, session: date, bar_size: str) -> Path:
        safe = bar_size.replace(" ", "")
        return self.root / kind / f"{symbol}_{session:%Y%m%d}_{safe}.json"

    def load(self, kind: str, symbol: str, session: date, bar_size: str) -> list | None:
        p = self._path(kind, symbol, session, bar_size)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                logger.warning("Corrupt cache entry, refetching: %s", p)
        return None

    def save(
        self, kind: str, symbol: str, session: date, bar_size: str, rows: list
    ) -> None:
        p = self._path(kind, symbol, session, bar_size)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rows))


class ReplayDriver:
    def __init__(
        self,
        ib: IB,
        session: date,
        bar_size: str = "1 min",
        cache_dir: Path | None = None,
        concurrency: int = 10,
    ):
        self.ib = ib
        self.session = session
        self.bar_size = bar_size
        self.bar_seconds = parse_bar_seconds(bar_size)
        self.cache = BarCache(cache_dir or Path("data/bars"))
        self.sem = asyncio.Semaphore(concurrency)

    # -- fetching ----------------------------------------------------------

    async def _qualify(self, symbols: list[str]) -> dict[str, object]:
        raw = [Stock(s, "SMART", "USD") for s in symbols]
        qualified = await self.ib.qualifyContractsAsync(*raw)
        good = {c.symbol: c for c in qualified if c and getattr(c, "conId", 0) > 0}
        missing = sorted(set(symbols) - set(good))
        if missing:
            logger.warning("Could not qualify: %s", ", ".join(missing))
        return good

    async def fetch_levels(
        self, symbols: list[str], adr_sessions: int = ADR_SESSIONS
    ) -> dict[str, DailyLevels]:
        """PDH/PDL and ADR from RTH sessions *before* the replay day.

        Requesting daily bars that end at 00:00 on the replay date guarantees the
        replay day itself is excluded, so this cannot leak the day being
        replayed into its own reference levels -- and equally cannot leak it into
        the ADR used to judge that day's triggers.

        ADR comes free from the same request that produces PDH/PDL, which is why
        computing it here is cheaper than importing it: no second data source, no
        CSV step before adding a ticker mid-session, and it is correct as of the
        replayed date rather than as of whenever an export was taken.
        """
        contracts = await self._qualify(symbols)
        out: dict[str, DailyLevels] = {}

        # Enough calendar days to cover adr_sessions trading days plus holidays.
        duration_days = max(15, int(adr_sessions * 1.6) + 5)

        async def one(sym: str, contract) -> None:
            cached = self.cache.load("levels", sym, self.session, "1day")
            if cached and REQUIRED_LEVEL_KEYS <= set(cached):
                out[sym] = DailyLevels(
                    sym,
                    date.fromisoformat(cached["session"]),
                    cached["pdh"],
                    cached["pdl"],
                    cached["adr_pct"],
                    cached["prev_close"],
                    cached["adr_sessions"],
                )
                return
            async with self.sem:
                bars = await self.ib.reqHistoricalDataAsync(
                    contract,
                    endDateTime=f"{self.session:%Y%m%d} 00:00:00 US/Eastern",
                    durationStr=f"{duration_days} D",
                    barSizeSetting="1 day",
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=1,
                    timeout=REQUEST_TIMEOUT,
                )
            if not bars:
                logger.warning("No daily bars for %s; cannot derive PDH/PDL", sym)
                return
            prior = bars[-1]
            prior_session = prior.date if isinstance(prior.date, date) else None
            if isinstance(prior.date, datetime):
                prior_session = prior.date.date()
            if prior_session and prior_session >= self.session:
                logger.error(
                    "%s: prior daily bar is %s, not before replay day %s -- skipping",
                    sym,
                    prior_session,
                    self.session,
                )
                return

            adr_pct, used = compute_adr_pct(bars, adr_sessions)
            if used < adr_sessions:
                logger.warning(
                    "%s: only %d of %d sessions available for ADR",
                    sym,
                    used,
                    adr_sessions,
                )

            out[sym] = DailyLevels(
                sym,
                prior_session,
                float(prior.high),
                float(prior.low),
                adr_pct,
                float(prior.close),
                used,
            )
            self.cache.save(
                "levels",
                sym,
                self.session,
                "1day",
                {
                    "session": prior_session.isoformat(),
                    "pdh": prior.high,
                    "pdl": prior.low,
                    "adr_pct": adr_pct,
                    "prev_close": prior.close,
                    "adr_sessions": used,
                },
            )

        await asyncio.gather(*[one(s, c) for s, c in contracts.items()])
        return out

    async def fetch_bars(self, symbols: list[str]) -> dict[str, list[BarEvent]]:
        contracts = await self._qualify(symbols)
        out: dict[str, list[BarEvent]] = {}

        async def one(sym: str, contract) -> None:
            rows = self.cache.load("intraday", sym, self.session, self.bar_size)
            if rows is None:
                async with self.sem:
                    bars = await self.ib.reqHistoricalDataAsync(
                        contract,
                        endDateTime=f"{self.session:%Y%m%d} 16:00:00 US/Eastern",
                        durationStr="1 D",
                        barSizeSetting=self.bar_size,
                        whatToShow="TRADES",
                        useRTH=True,
                        formatDate=1,
                        timeout=REQUEST_TIMEOUT,
                    )
                if not bars:
                    logger.warning("No intraday bars for %s on %s", sym, self.session)
                    return
                rows = [
                    {
                        "t": to_et(b.date).isoformat(),
                        "o": b.open,
                        "h": b.high,
                        "l": b.low,
                        "c": b.close,
                        "v": b.volume,
                    }
                    for b in bars
                    if to_et(b.date) is not None
                ]
                self.cache.save("intraday", sym, self.session, self.bar_size, rows)

            events = []
            for r in rows:
                start = datetime.fromisoformat(r["t"])
                if start.date() != self.session:
                    continue  # guard against a request returning a neighbouring day
                events.append(
                    BarEvent(
                        symbol=sym,
                        start=start,
                        end=start + timedelta(seconds=self.bar_seconds),
                        open=float(r["o"]),
                        high=float(r["h"]),
                        low=float(r["l"]),
                        close=float(r["c"]),
                        volume=float(r["v"]),
                    )
                )
            out[sym] = events

        await asyncio.gather(*[one(s, c) for s, c in contracts.items()])
        return out

    # -- streaming ---------------------------------------------------------

    @staticmethod
    def stream(
        bars_by_symbol: dict[str, list[BarEvent]],
        start_time: time | None = None,
        end_time: time | None = None,
    ) -> Iterator[BarEvent]:
        """Yield every bar in chronological order, one at a time.

        A generator rather than a list, so the Scanner is handed events it
        cannot look past. Ties are broken by symbol purely for determinism.
        """
        merged = [b for bars in bars_by_symbol.values() for b in bars]
        merged.sort(key=lambda b: (b.start, b.symbol))
        for bar in merged:
            t = bar.start.timetz().replace(tzinfo=None)
            if start_time and t < start_time:
                continue
            if end_time and t >= end_time:
                continue
            yield bar


def run_replay(
    scanner: Scanner,
    bars_by_symbol: dict[str, list[BarEvent]],
    start_time: time | None = None,
    end_time: time | None = None,
    on_trigger=None,
) -> int:
    """Drive the scanner over a replay stream. Returns bars fed."""
    fed = 0
    for bar in ReplayDriver.stream(bars_by_symbol, start_time, end_time):
        fed += 1
        for trigger in scanner.on_bar(bar):
            if on_trigger:
                on_trigger(trigger)
    return fed
