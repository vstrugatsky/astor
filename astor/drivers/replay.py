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
import re
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path

from ib_async import IB, Stock

from astor.core.scanner import Scanner
from astor.core.types import BarEvent, DailyLevels, VolumeCurve

logger = logging.getLogger(__name__)

ET = __import__("zoneinfo").ZoneInfo("America/New_York")

# ib_async returns [] instead of raising when its own timeout fires, so every
# request passes an explicit timeout and empty results are treated as failures.
REQUEST_TIMEOUT = 60.0

ADR_SESSIONS = 14
RVOL_SESSIONS = 20  # the user's own spec: "1-min bars for the last 20 days"

# A cache entry written before ADR existed lacks these, so it must be refetched
# rather than loaded with zeroed volatility -- which would silently disable the
# filters instead of failing visibly.
REQUIRED_LEVEL_KEYS = {
    "session", "pdh", "pdl", "adr_pct", "prev_close", "adr_sessions", "adv",
}

# Matches the YYYYMMDD embedded in every BarCache filename (see
# BarCache._path: f"{symbol}_{session:%Y%m%d}_{bar_size}.json").
_CACHE_FILENAME_DATE_RE = re.compile(r"_(\d{8})_")


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


def compute_adv(daily_bars, sessions: int = ADR_SESSIONS) -> tuple[float, int]:
    """Average Dollar Volume: mean of (close * volume) over the last N
    sessions, in dollars. Feeds astor.core.grading's rvol tiers -- the more
    liquid a name, the less relative volume it takes for a move to be
    unusual. Free from the same daily-bar request ADR already uses, same
    as ADR is free from PDH/PDL: no second data source.
    """
    dollars = [
        b.close * b.volume
        for b in daily_bars[-sessions:]
        if b.close and b.close > 0 and b.volume and b.volume > 0
    ]
    if not dollars:
        return 0.0, 0
    return round(sum(dollars) / len(dollars), 2), len(dollars)


def build_cum_volume_curve(
    rows_by_session: dict[date, list[dict]],
) -> tuple[dict[time, float], int]:
    """The mean cumulative share volume by minute-of-day across a set of
    sessions' 1-min bar rows (the `BarCache` "intraday" row shape: dicts with
    at least "t" and "v"). Returns (cum_volume_by_minute, sessions_used) --
    a session contributing no usable rows doesn't count toward the mean, and
    a minute only some sessions have bars for is averaged over just those.
    """
    sums: dict[time, float] = {}
    counts: dict[time, int] = {}
    used = 0
    for d, rows in rows_by_session.items():
        cum = 0.0
        contributed = False
        for r in sorted(rows, key=lambda r: r["t"]):
            start = datetime.fromisoformat(r["t"])
            if start.date() != d:
                continue  # guard against a request returning a neighbouring day
            cum += float(r["v"])
            key = start.timetz().replace(tzinfo=None)
            sums[key] = sums.get(key, 0.0) + cum
            counts[key] = counts.get(key, 0) + 1
            contributed = True
        if contributed:
            used += 1
    return {k: sums[k] / counts[k] for k in sums}, used


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


def next_trading_session(now_et: datetime) -> date:
    """The trading day a process starting right now should target.

    Before today's close, that's today (covers "started mid-session" and
    every pre-open case). At/after today's close, it's the next weekday --
    the night-before-prep case, which is also what a cron-scheduled
    warm-cache run wants by default. This does not know about market
    holidays: a holiday immediately after would roll to a closed day. Low
    blast radius if it happens (a cache-key/label mismatch, not a wrong PDH/
    PDL -- the "last N sessions" lookups already skip closed days on their
    own since IBKR simply has no bars for them), and not worth a market
    calendar for a first pass.
    """
    d = now_et.date() if now_et.time() < time(16, 0) else now_et.date() + timedelta(days=1)
    while d.weekday() >= 5:  # Saturday=5, Sunday=6
        d += timedelta(days=1)
    return d


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

    def prune(
        self, protected_sessions: set[str], retention_days: int, today: date
    ) -> dict[str, int]:
        """Delete cache entries older than retention_days, except any date
        in protected_sessions (ISO date strings) -- a date that was ever an
        actual replay or live session (astor.cli.cmd_warm_cache passes the
        distinct `session` values from data/astor.db's runs table) stays
        forever as a real trading record, no matter its age. Everything
        else is either the RVOL curve's rolling window (still needed, so
        younger than retention_days) or pure curve-building filler for a
        symbol that was never actually traded -- safe to delete once it
        ages out.

        `today` is passed in rather than read from the clock here so this
        stays a pure function of its inputs, testable without patching
        datetime.
        """
        cutoff = today - timedelta(days=retention_days)
        removed = {"levels": 0, "curve": 0, "intraday": 0}
        for kind in removed:
            d = self.root / kind
            if not d.exists():
                continue
            for p in d.glob("*.json"):
                m = _CACHE_FILENAME_DATE_RE.search(p.name)
                if not m:
                    continue
                entry_date = datetime.strptime(m.group(1), "%Y%m%d").date()
                if entry_date >= cutoff or entry_date.isoformat() in protected_sessions:
                    continue
                p.unlink()
                removed[kind] += 1
        return removed


class ReplayDriver:
    def __init__(
        self,
        ib: IB,
        session: date,
        bar_size: str = "1 min",
        cache_dir: Path | None = None,
        concurrency: int = 10,
        request_pace_seconds: float = 0.0,
    ):
        self.ib = ib
        self.session = session
        self.bar_size = bar_size
        self.bar_seconds = parse_bar_seconds(bar_size)
        self.cache = BarCache(cache_dir or Path("data/bars"))
        self.sem = asyncio.Semaphore(concurrency)
        # A per-slot delay after each historical-data request, on top of the
        # concurrency cap -- unset (0.0, no change in behavior) for
        # interactive Replay/Live runs, set by astor.cli's warm-cache
        # command for a cold pass across hundreds of symbols, where IBKR's
        # pacing limits -- not modeled anywhere in this codebase, but real,
        # per ACTIVATION_PACE_SECONDS in live.py existing because of one --
        # are the actual constraint, not local concurrency.
        self.request_pace_seconds = request_pace_seconds

    # -- fetching ----------------------------------------------------------

    async def qualify(self, symbols: list[str]) -> dict[str, object]:
        raw = [Stock(s, "SMART", "USD") for s in symbols]
        qualified = await self.ib.qualifyContractsAsync(*raw)
        good = {c.symbol: c for c in qualified if c and getattr(c, "conId", 0) > 0}
        missing = sorted(set(symbols) - set(good))
        if missing:
            logger.warning("Could not qualify: %s", ", ".join(missing))
        return good

    async def _historical(self, contract, **kwargs):
        """One reqHistoricalDataAsync call, gated by the concurrency
        semaphore and (if set) paced by request_pace_seconds. The one choke
        point every historical-data request in this class goes through, so
        pacing applies uniformly regardless of which fetch method is
        calling."""
        async with self.sem:
            result = await self.ib.reqHistoricalDataAsync(
                contract, timeout=REQUEST_TIMEOUT, **kwargs
            )
            if self.request_pace_seconds:
                await asyncio.sleep(self.request_pace_seconds)
            return result

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
        contracts = await self.qualify(symbols)
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
                    cached["adv"],
                )
                return
            bars = await self._historical(
                contract,
                endDateTime=f"{self.session:%Y%m%d} 00:00:00 US/Eastern",
                durationStr=f"{duration_days} D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
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
            adv, _ = compute_adv(bars, adr_sessions)

            out[sym] = DailyLevels(
                sym,
                prior_session,
                float(prior.high),
                float(prior.low),
                adr_pct,
                float(prior.close),
                used,
                adv,
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
                    "adv": adv,
                },
            )

        await asyncio.gather(*[one(s, c) for s, c in contracts.items()])
        return out

    async def fetch_bars(self, symbols: list[str]) -> dict[str, list[BarEvent]]:
        contracts = await self.qualify(symbols)
        out: dict[str, list[BarEvent]] = {}

        async def one(sym: str, contract) -> None:
            rows = self.cache.load("intraday", sym, self.session, self.bar_size)
            if rows is None:
                bars = await self._historical(
                    contract,
                    endDateTime=f"{self.session:%Y%m%d} 16:00:00 US/Eastern",
                    durationStr="1 D",
                    barSizeSetting=self.bar_size,
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=1,
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

    async def fetch_volume_curve(
        self, symbols: list[str], sessions: int = RVOL_SESSIONS
    ) -> dict[str, VolumeCurve]:
        """Expected cumulative share volume by minute-of-day, averaged over
        each symbol's last `sessions` RTH sessions -- what RVOL divides
        today's running volume against.

        Discovers those session dates the same way `fetch_levels` finds the
        prior session: a daily-bar request ending before `self.session`,
        which is anchored to that end date rather than to "now", so this
        behaves identically called the night before or minutes before the
        open. Each session's 1-min bars go through the same `BarCache`
        ("intraday", ...) shape `fetch_bars` writes, so a day already cached
        by a Replay run -- or a previous curve build -- is never re-fetched
        from IBKR, and a day this method fetches is usable by `fetch_bars`
        later. The derived curve itself is also cached (kind "curve") so an
        unchanged rolling window doesn't get rebuilt from its 20 days of
        1-min bars on every call.
        """
        contracts = await self.qualify(symbols)
        out: dict[str, VolumeCurve] = {}
        duration_days = max(15, int(sessions * 1.6) + 5)

        async def one(sym: str, contract) -> None:
            cached = self.cache.load("curve", sym, self.session, self.bar_size)
            if cached and cached.get("sessions") == sessions:
                out[sym] = VolumeCurve(
                    sym,
                    cached["sessions"],
                    {
                        time.fromisoformat(t): v
                        for t, v in cached["cum_volume_by_minute"].items()
                    },
                )
                return

            daily = await self._historical(
                contract,
                endDateTime=f"{self.session:%Y%m%d} 00:00:00 US/Eastern",
                durationStr=f"{duration_days} D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
            if not daily:
                logger.warning("No daily bars for %s; cannot build a volume curve", sym)
                return

            session_dates: list[date] = []
            for b in daily[-sessions:]:
                d = b.date.date() if isinstance(b.date, datetime) else b.date
                if isinstance(d, date) and d < self.session:
                    session_dates.append(d)
            if not session_dates:
                logger.warning("No prior sessions for %s; skipping its curve", sym)
                return

            rows_by_session: dict[date, list[dict]] = {}
            for d in session_dates:
                rows = self.cache.load("intraday", sym, d, self.bar_size)
                if rows is None:
                    bars = await self._historical(
                        contract,
                        endDateTime=f"{d:%Y%m%d} 16:00:00 US/Eastern",
                        durationStr="1 D",
                        barSizeSetting=self.bar_size,
                        whatToShow="TRADES",
                        useRTH=True,
                        formatDate=1,
                    )
                    if not bars:
                        logger.warning(
                            "No intraday bars for %s on %s; skipped for its curve",
                            sym,
                            d,
                        )
                        continue
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
                    self.cache.save("intraday", sym, d, self.bar_size, rows)
                rows_by_session[d] = rows

            cum_volume_by_minute, used = build_cum_volume_curve(rows_by_session)
            if not cum_volume_by_minute:
                logger.warning("No usable sessions for %s; skipping its curve", sym)
                return

            out[sym] = VolumeCurve(sym, used, cum_volume_by_minute)
            self.cache.save(
                "curve",
                sym,
                self.session,
                self.bar_size,
                {
                    "sessions": used,
                    "cum_volume_by_minute": {
                        k.isoformat(): v for k, v in cum_volume_by_minute.items()
                    },
                },
            )

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
