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
    extras_json   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_triggers_run ON triggers(run_id);
CREATE INDEX IF NOT EXISTS idx_triggers_symbol ON triggers(symbol, bar_start);
"""


class TriggerStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

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

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
