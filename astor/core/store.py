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
    is_tradeable  TEXT NOT NULL DEFAULT 'N', -- the trader's own call, 'Y' | 'N'
    notes         TEXT                       -- the trader's own notes
);

CREATE INDEX IF NOT EXISTS idx_triggers_run ON triggers(run_id);
CREATE INDEX IF NOT EXISTS idx_triggers_symbol ON triggers(symbol, bar_start);
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
    t.is_tradeable,
    t.notes,
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
        if "is_tradeable" not in columns:
            self.conn.execute(
                "ALTER TABLE triggers ADD COLUMN is_tradeable TEXT NOT NULL DEFAULT 'N'"
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
            self.conn.execute("DELETE FROM runs WHERE run_id != ?", (keep,))
        else:
            n = self.conn.execute("SELECT count(*) FROM runs").fetchone()[0]
            self.conn.execute("DELETE FROM triggers")
            self.conn.execute("DELETE FROM runs")
        self.conn.commit()
        return n

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
