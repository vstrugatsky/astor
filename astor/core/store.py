"""Append-only trigger store.

Every trigger is written with the run that produced it and the exact parameters
that run used. Without that, two Replay runs cannot be compared -- a difference
in results is indistinguishable from a difference in settings.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from astor.core.types import Trigger

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    mode        TEXT NOT NULL,          -- 'replay' | 'live'
    created_at  TEXT NOT NULL,
    params_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS triggers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL REFERENCES runs(run_id),
    symbol        TEXT NOT NULL,
    setup         TEXT NOT NULL,
    direction     TEXT NOT NULL,
    trigger_price REAL NOT NULL,
    bar_start     TEXT NOT NULL,        -- bar timestamp (bar start), ET
    bar_end       TEXT NOT NULL,        -- when the bar completed
    detection_ts  TEXT NOT NULL,        -- when this run detected it
    level_name    TEXT NOT NULL,        -- 'PDH' | 'PDL'
    level_value   REAL NOT NULL,
    extras_json   TEXT NOT NULL,
    -- The trader's own setup-quality grade, D=1 (non-tradeable) .. A=4
    -- (best). NULL until reviewed -- not a default like 0/1, since ungraded
    -- is a real, distinct state from "graded D".
    grade         INTEGER CHECK (grade IS NULL OR grade BETWEEN 1 AND 4),
    notes         TEXT                       -- the trader's own notes
);

CREATE INDEX IF NOT EXISTS idx_triggers_run ON triggers(run_id);
CREATE INDEX IF NOT EXISTS idx_triggers_symbol ON triggers(symbol, bar_start);

-- Capitulation-in-progress alerts and sell-rule track alerts (breakeven/sell)
-- share one table: both are real-time console notifications distinct from a
-- setup firing (triggers) -- neither is itself a trade signal, and neither
-- used to be persisted at all (track alerts were console-only) until this
-- table gave both a place to land for later review.
CREATE TABLE IF NOT EXISTS alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL REFERENCES runs(run_id),
    kind          TEXT NOT NULL,        -- 'capitulation' | 'breakeven' | 'sell'
    symbol        TEXT NOT NULL,
    direction     TEXT NOT NULL,
    bar_start     TEXT NOT NULL,
    bar_end       TEXT,                 -- NULL for track alerts (no bar_end concept there)
    price         REAL NOT NULL,        -- close at detection
    detection_ts  TEXT NOT NULL,
    extras_json   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_run ON alerts(run_id);
CREATE INDEX IF NOT EXISTS idx_alerts_symbol ON alerts(symbol, bar_start);
"""

# Views are derived, so they are dropped and rebuilt on every connect rather than
# guarded with IF NOT EXISTS -- otherwise a changed definition here would never
# reach a database that already exists.
VIEWS = """
DROP VIEW IF EXISTS trigger_detail;
CREATE VIEW trigger_detail AS
SELECT
    t.id,
    t.run_id,
    -- NOT date()/time(): those parse the ISO offset and normalize to UTC, which
    -- silently shifts every ET timestamp by 4-5 hours. Timestamps are always
    -- written as tz-aware ET isoformat, so slicing the fixed-width string keeps
    -- them in the session's own clock -- the only one the trader reasons in.
    substr(t.bar_start, 1, 10) AS session,
    substr(t.bar_start, 12, 8) AS trigger_time,
    t.symbol,
    t.setup,
    t.direction,
    t.trigger_price,
    t.level_value,
    -- The trader's own review, entered after the fact (DataGrip-editable real
    -- columns, not extras_json -- a rerun of the same session never touches
    -- an existing row's review, since record() only ever inserts new rows).
    t.grade,
    t.notes,
    -- The program's own real-time call (astor.core.grading), computed once
    -- at trigger time and never hand-edited, same as every other diagnostic
    -- below -- kept in extras_json rather than a real column, unlike grade.
    json_extract(t.extras_json, '$.auto_grade')                AS auto_grade,
    -- U&R and O&F record mirrored key names; coalescing gives one column each.
    COALESCE(json_extract(t.extras_json, '$.bars_below'),
             json_extract(t.extras_json, '$.bars_above'))     AS bars_beyond,
    json_extract(t.extras_json, '$.bars_armed')               AS bars_armed,
    json_extract(t.extras_json, '$.excursion_adr')            AS excursion_adr,
    json_extract(t.extras_json, '$.excursion_pct')            AS excursion_pct,
    json_extract(t.extras_json, '$.reclaim_adr')              AS reclaim_adr,
    json_extract(t.extras_json, '$.reclaim_pct')              AS reclaim_pct,
    json_extract(t.extras_json, '$.combined_pct')             AS combined_pct,
    json_extract(t.extras_json, '$.adr_pct')                  AS adr_pct,
    json_extract(t.extras_json, '$.avwap')                    AS avwap,
    json_extract(t.extras_json, '$.avwap_pct')                AS avwap_pct,
    json_extract(t.extras_json, '$.rvol')                     AS rvol,
    -- Notify's trade-decision fields: two stop/position-size framings, one
    -- off the technical level (PDH/PDL, in level_value already) and one off
    -- the session's actual high/low, which can be much farther for a wide
    -- excursion.
    json_extract(t.extras_json, '$.max_loss_dollars')         AS max_loss_dollars,
    json_extract(t.extras_json, '$.level_stop_pct')           AS level_stop_pct,
    json_extract(t.extras_json, '$.level_stop_shares')        AS level_stop_shares,
    json_extract(t.extras_json, '$.level_stop_dollars')       AS level_stop_dollars,
    -- Session open/high/low -- the day's actual range, independent of setup
    -- direction, so applicable the same way to every setup.
    json_extract(t.extras_json, '$.session_open')              AS session_open,
    json_extract(t.extras_json, '$.session_high')              AS session_high,
    json_extract(t.extras_json, '$.session_low')               AS session_low,
    json_extract(t.extras_json, '$.session_extreme')          AS session_extreme,
    json_extract(t.extras_json, '$.session_extreme_pct')      AS session_extreme_pct,
    json_extract(t.extras_json, '$.session_stop_shares')      AS session_stop_shares,
    json_extract(t.extras_json, '$.session_stop_dollars')     AS session_stop_dollars,
    -- Reversal (REV-L/REV-S) only: the leg it fired on. anchor/leg_extreme
    -- are dollars, leg_size_adr/leg_size_pct describe the same leg in ADR
    -- and percent-of-anchor terms, leg_duration_minutes is how long the push
    -- itself took to build (anchor bar to extreme bar -- a slow multi-hour
    -- drift is a much weaker "sharp move" than a fast one of the same size,
    -- not yet filtered on but recorded for future tuning), and
    -- retrace_pct/retrace_minutes/move_rvol/leg_duration_minutes are among
    -- the filters that actually gate the fire (10-50% retracement or an
    -- EMA9 reclaim -- see retrace_via -- >= the confirm wait since the
    -- extreme, RVOL frozen at the extreme bar, >= the leg-duration floor).
    json_extract(t.extras_json, '$.anchor')                   AS anchor,
    json_extract(t.extras_json, '$.leg_extreme')               AS leg_extreme,
    json_extract(t.extras_json, '$.leg_size_adr')              AS leg_size_adr,
    json_extract(t.extras_json, '$.leg_size_pct')              AS leg_size_pct,
    -- Where leg_duration_minutes actually starts counting from: the true
    -- anchor's own timestamp, unless the leg died and revived, in which
    -- case this is the dead retracement's own turnaround instead (the
    -- unrelated dead stretch before that doesn't count as part of "how
    -- long this push took"). `anchor` itself (the price level) never
    -- moves regardless -- this only affects the duration measurement.
    json_extract(t.extras_json, '$.push_started_at')          AS push_started_at,
    json_extract(t.extras_json, '$.leg_duration_minutes')      AS leg_duration_minutes,
    json_extract(t.extras_json, '$.retrace_pct')               AS retrace_pct,
    json_extract(t.extras_json, '$.retrace_minutes')           AS retrace_minutes,
    -- 'pct' (cleared the retrace floor) or 'ema9_reclaim' (retrace was still
    -- short of the floor, but price crossed back through the 9EMA).
    json_extract(t.extras_json, '$.retrace_via')               AS retrace_via,
    json_extract(t.extras_json, '$.move_rvol')                 AS move_rvol,
    -- ema9_at_extreme/ema9_extension_adr: the 9EMA reading frozen at the
    -- leg's extreme, and how far (in ADR units) the extreme finished from
    -- it -- the capitulation "extended from the 9EMA" filter. Measured
    -- close-to-close (extreme_close, the extreme bar's own close), not
    -- against the high/low wick the extreme itself is, since the EMA line
    -- was never computed from a wick either. accel_price: net close-to-
    -- close displacement per bar, last few bars vs. the rest of the leg's
    -- (not average bar range -- rewards actual directional speed, not
    -- wick noise). accel_vol: average per-bar volume, last few bars vs.
    -- the rest. Both now filtered on too (min_accel_price/min_accel_vol),
    -- same as ema9_extension_adr.
    json_extract(t.extras_json, '$.ema9_at_extreme')           AS ema9_at_extreme,
    json_extract(t.extras_json, '$.extreme_close')             AS extreme_close,
    json_extract(t.extras_json, '$.ema9_extension_adr')        AS ema9_extension_adr,
    json_extract(t.extras_json, '$.accel_price')                AS accel_price,
    json_extract(t.extras_json, '$.accel_vol')                  AS accel_vol,
    -- The retired U&R (REV-L)/O&F (REV-S) breach+reclaim condition against
    -- PDL/PDH -- one of these two keys is present depending on direction,
    -- coalesced the same way bars_below/bars_above already are above.
    COALESCE(json_extract(t.extras_json, '$.meets_ur_condition'),
             json_extract(t.extras_json, '$.meets_of_condition')) AS meets_legacy_condition,
    -- AVWAP touch (AVT-L/AVT-S) only. touch_adr is signed -- which side of
    -- AVWAP the pullback wick actually reached, since the band is symmetric
    -- and a poke through is still a touch. depth_adr is how far that wick
    -- was from the day's extreme, recovery_adr how far past AVWAP the
    -- trigger bar closed, and bars_since_touch how long the continuation
    -- took (0 = the same bar did both). stop_source says which term set the
    -- stop: the pullback extreme, or the percent-of-entry floor under it.
    json_extract(t.extras_json, '$.day_range_adr')             AS day_range_adr,
    json_extract(t.extras_json, '$.touch_adr')                 AS touch_adr,
    json_extract(t.extras_json, '$.touch_at')                  AS touch_at,
    json_extract(t.extras_json, '$.touch_price')               AS touch_price,
    json_extract(t.extras_json, '$.depth_adr')                 AS depth_adr,
    json_extract(t.extras_json, '$.recovery_adr')              AS recovery_adr,
    json_extract(t.extras_json, '$.bars_since_touch')          AS bars_since_touch,
    json_extract(t.extras_json, '$.rvol_floor')                AS rvol_floor,
    json_extract(t.extras_json, '$.stop_source')               AS stop_source,
    -- detection_ts equals bar_end exactly in Replay, so showing both is noise.
    -- The difference is the only part that carries information, and it is what
    -- Live will populate: how long after the bar closed the trigger was seen.
    round((julianday(t.detection_ts) - julianday(t.bar_end)) * 86400, 1)
                                                              AS latency_sec
FROM triggers t;

-- The most recent run *per session date, per mode*, not the single most
-- recent run overall. Rerunning Friday and then today's session are
-- different sessions, so latest_triggers should show both -- but rerunning
-- the SAME session in the SAME mode twice (a threshold tweak) should still
-- show only the newer of the two, not both. Keying on (session, mode) gets
-- both properties within a mode: it grows to cover every session you have
-- run, while a rerun of any one session in that mode transparently
-- supersedes its own earlier attempt in that mode.
--
-- Partitioned by mode, not just session: a live run is never superseded by
-- a later replay of the same date, or vice versa -- they answer different
-- questions (what was actually caught in real time vs. a full reprocessing
-- after the fact) and are worth comparing side by side, not clobbering one
-- another. A rerun *within* the same mode still supersedes as before, so
-- replaying the same day twice to retune filters doesn't leave two replay
-- attempts sitting side by side.
DROP VIEW IF EXISTS latest_run_per_session;
CREATE VIEW latest_run_per_session AS
SELECT run_id, session, created_at FROM (
    SELECT
        r.run_id,
        json_extract(r.params_json, '$.session') AS session,
        r.created_at,
        ROW_NUMBER() OVER (
            PARTITION BY json_extract(r.params_json, '$.session'), r.mode
            ORDER BY r.created_at DESC, r.run_id DESC
        ) AS rn
    FROM runs r
)
WHERE rn = 1;

DROP VIEW IF EXISTS latest_triggers;
CREATE VIEW latest_triggers AS
SELECT * FROM trigger_detail
WHERE run_id IN (SELECT run_id FROM latest_run_per_session)
ORDER BY session, trigger_time;

-- Every other column here is derived (from triggers/runs, or computed by a
-- run) -- only grade/notes are meant to be hand-edited, so this is the one
-- write path the trigger allows. DROP VIEW already drops any trigger
-- defined on it, but this still needs re-creating here since it is itself
-- dropped and rebuilt right along with the view every connect.
DROP TRIGGER IF EXISTS latest_triggers_review_update;
CREATE TRIGGER latest_triggers_review_update
INSTEAD OF UPDATE ON latest_triggers
FOR EACH ROW
BEGIN
    UPDATE triggers
    SET grade = NEW.grade,
        notes = NEW.notes
    WHERE id = OLD.id;
END;

-- Capitulation and track alerts, one row shape for both -- see the `alerts`
-- table comment. Capitulation-only and track-only columns simply read NULL
-- for the other kind's rows, same COALESCE-free "just extract what's there"
-- approach trigger_detail uses for U&R/O&F's mirrored key names.
DROP VIEW IF EXISTS alert_detail;
CREATE VIEW alert_detail AS
SELECT
    a.id,
    a.run_id,
    substr(a.bar_start, 1, 10) AS session,
    substr(a.bar_start, 12, 8) AS alert_time,
    a.kind,
    a.symbol,
    a.direction,
    a.price,
    a.bar_end,
    -- Capitulation-only fields
    json_extract(a.extras_json, '$.leg_extreme')         AS leg_extreme,
    json_extract(a.extras_json, '$.anchor')              AS anchor,
    json_extract(a.extras_json, '$.leg_size_adr')        AS leg_size_adr,
    json_extract(a.extras_json, '$.move_rvol')           AS move_rvol,
    json_extract(a.extras_json, '$.ema9_extension_adr')  AS ema9_extension_adr,
    json_extract(a.extras_json, '$.accel_price')         AS accel_price,
    json_extract(a.extras_json, '$.accel_vol')           AS accel_vol,
    -- Track-alert-only fields (breakeven/sell)
    json_extract(a.extras_json, '$.entry')               AS entry,
    json_extract(a.extras_json, '$.stop')                AS stop,
    json_extract(a.extras_json, '$.peak')                AS peak,
    json_extract(a.extras_json, '$.r')                   AS r,
    json_extract(a.extras_json, '$.target_1r')           AS target_1r,
    json_extract(a.extras_json, '$.account')             AS account,
    round((julianday(a.detection_ts)
           - julianday(COALESCE(a.bar_end, a.bar_start))) * 86400, 1) AS latency_sec
FROM alerts a;

-- Same latest-run-per-session-per-mode partitioning latest_triggers uses,
-- via the same latest_run_per_session view defined above.
DROP VIEW IF EXISTS latest_alerts;
CREATE VIEW latest_alerts AS
SELECT * FROM alert_detail
WHERE run_id IN (SELECT run_id FROM latest_run_per_session)
ORDER BY session, alert_time;
"""


class TriggerStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        # WAL lets a reader (DataGrip) browse the store while a replay is
        # writing, instead of the two blocking each other. The setting is
        # persisted in the database file, so it survives reconnects.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.executescript(VIEWS)
        self.conn.commit()

    def _migrate(self) -> None:
        """CREATE TABLE IF NOT EXISTS only sets up a table the first time --
        a column added here later needs its own ALTER TABLE to reach a
        database file created before that column existed."""
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(triggers)")}
        if "is_tradeable" in columns:
            # Superseded by the numeric grade column below -- a Y/N flag
            # doesn't map onto a 4-point A-D scale, so the old values are
            # dropped rather than translated. DROP COLUMN fails while a view
            # (or the trigger attached to one) still references the column,
            # so those go first; VIEWS rebuilds both right after this runs.
            self.conn.execute("DROP VIEW IF EXISTS latest_triggers")
            self.conn.execute("DROP VIEW IF EXISTS trigger_detail")
            self.conn.execute("ALTER TABLE triggers DROP COLUMN is_tradeable")
        if "grade" not in columns:
            self.conn.execute(
                "ALTER TABLE triggers ADD COLUMN grade INTEGER"
                " CHECK (grade IS NULL OR grade BETWEEN 1 AND 4)"
            )
        if "notes" not in columns:
            self.conn.execute("ALTER TABLE triggers ADD COLUMN notes TEXT")

    def start_run(self, run_id: str, mode: str, params: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, mode, created_at, params_json)"
            " VALUES (?, ?, ?, ?)",
            (
                run_id,
                mode,
                datetime.now().astimezone().isoformat(),
                json.dumps(params, default=str),
            ),
        )
        self.conn.commit()

    def record(self, run_id: str, trigger: Trigger, detection_ts: datetime) -> None:
        self.conn.execute(
            "INSERT INTO triggers (run_id, symbol, setup, direction, trigger_price,"
            " bar_start, bar_end, detection_ts, level_name, level_value, extras_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                trigger.symbol,
                trigger.setup,
                trigger.direction,
                trigger.trigger_price,
                trigger.bar_start.isoformat(),
                trigger.bar_end.isoformat(),
                detection_ts.isoformat(),
                trigger.level_name,
                trigger.level_value,
                json.dumps(trigger.extras, default=str),
            ),
        )

    def record_alert(
        self,
        run_id: str,
        kind: str,
        symbol: str,
        direction: str,
        bar_start: datetime,
        price: float,
        detection_ts: datetime,
        extras: dict,
        bar_end: datetime | None = None,
    ) -> None:
        """Record a capitulation-in-progress alert or a sell-rule track
        alert (breakeven/sell) -- see the `alerts` table comment for why
        both share this one method/table. Not a Trigger: takes plain fields
        rather than a single typed object since CapitulationAlert and
        TrackAlert don't share a common shape."""
        self.conn.execute(
            "INSERT INTO alerts (run_id, kind, symbol, direction, bar_start, bar_end,"
            " price, detection_ts, extras_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                kind,
                symbol,
                direction,
                bar_start.isoformat(),
                bar_end.isoformat() if bar_end else None,
                price,
                detection_ts.isoformat(),
                json.dumps(extras, default=str),
            ),
        )

    def purge_runs(self, keep: str | None = None) -> int:
        """Delete stored runs in place, leaving the database file itself alone.

        Deleting the file instead would replace its inode, and any client with
        an open connection (DataGrip) keeps reading the old unlinked file --
        showing stale rows indefinitely with no indication anything is wrong.
        Clearing rows keeps every existing connection valid.
        """
        if keep:
            n = self.conn.execute(
                "SELECT count(*) FROM runs WHERE run_id != ?", (keep,)
            ).fetchone()[0]
            self.conn.execute("DELETE FROM triggers WHERE run_id != ?", (keep,))
            self.conn.execute("DELETE FROM alerts WHERE run_id != ?", (keep,))
            self.conn.execute("DELETE FROM runs WHERE run_id != ?", (keep,))
        else:
            n = self.conn.execute("SELECT count(*) FROM runs").fetchone()[0]
            self.conn.execute("DELETE FROM triggers")
            self.conn.execute("DELETE FROM alerts")
            self.conn.execute("DELETE FROM runs")
        self.conn.commit()
        return n

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
