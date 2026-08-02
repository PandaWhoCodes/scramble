"""Storage layer for Scramble.

Turso (libsql) when TURSO_DATABASE_URL is set, local SQLite otherwise.
The DB is the single source of truth: phones and the host console are
stateless renderers of what's in these four tables, which is what makes
refreshes and connection drops harmless.

Tables:
  room    — single row (id=1): session, phase, join code, PIN hash,
            team count, current round pointer.
  player  — one row per person per session (pid from the phone's
            localStorage UUID). Holds name, ready flag, color team.
  answer  — the host's ordered answer list (idx 0..N-1).
  round   — one JSON snapshot per activated round: the answer, its shape,
            and the exact pid -> letters deal. Snapshots make Next/Back
            replayable and refresh-proof.
"""

import json
import os
import sqlite3
import threading

_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS room (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        session_id TEXT NOT NULL,
        phase TEXT NOT NULL,
        join_code TEXT NOT NULL,
        pin_hash TEXT NOT NULL,
        team_count INTEGER NOT NULL DEFAULT 1,
        current_round INTEGER NOT NULL DEFAULT -1,
        edge_marks INTEGER NOT NULL DEFAULT 1,
        allow_flips INTEGER NOT NULL DEFAULT 1,
        opened_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS player (
        session_id TEXT NOT NULL,
        pid TEXT NOT NULL,
        name TEXT NOT NULL,
        ready INTEGER NOT NULL DEFAULT 0,
        color_idx INTEGER NOT NULL DEFAULT -1,
        joined_at INTEGER NOT NULL,
        PRIMARY KEY (session_id, pid)
    )""",
    """CREATE TABLE IF NOT EXISTS answer (
        session_id TEXT NOT NULL,
        idx INTEGER NOT NULL,
        text TEXT NOT NULL,
        PRIMARY KEY (session_id, idx)
    )""",
    """CREATE TABLE IF NOT EXISTS round (
        session_id TEXT NOT NULL,
        idx INTEGER NOT NULL,
        payload TEXT NOT NULL,
        made_at INTEGER NOT NULL,
        PRIMARY KEY (session_id, idx)
    )""",
]


class Store:
    def __init__(self):
        url = os.getenv("TURSO_DATABASE_URL")
        token = os.getenv("TURSO_AUTH_TOKEN")
        self._lock = threading.Lock()
        if url:
            import libsql  # pip install libsql

            self._conn = libsql.connect(database=url, auth_token=token)
            self.backend = "turso"
        else:
            path = os.getenv("LOCAL_DB_PATH", "scramble.db")
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self.backend = "sqlite"
        with self._lock:
            for stmt in _STATEMENTS:
                self._conn.execute(stmt)
            for mig in (
                "ALTER TABLE room ADD COLUMN edge_marks INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE room ADD COLUMN allow_flips INTEGER NOT NULL DEFAULT 1",
            ):
                try:
                    self._conn.execute(mig)
                except Exception:
                    pass  # column already exists
            self._conn.commit()

    def _exec(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------- room

    def get_room(self):
        rows = self._query(
            "SELECT session_id, phase, join_code, pin_hash, team_count, current_round, "
            "edge_marks, allow_flips, opened_at FROM room WHERE id = 1"
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "session_id": r[0],
            "phase": r[1],
            "join_code": r[2],
            "pin_hash": r[3],
            "team_count": int(r[4]),
            "current_round": int(r[5]),
            "edge_marks": bool(r[6]),
            "allow_flips": bool(r[7]),
            "opened_at": r[8],
        }

    def open_room(self, session_id, join_code, pin_hash, ts):
        self._exec(
            "INSERT OR REPLACE INTO room (id, session_id, phase, join_code, pin_hash, team_count, current_round, "
            "edge_marks, allow_flips, opened_at) VALUES (1, ?, 'lobby', ?, ?, 1, -1, 1, 1, ?)",
            (session_id, join_code, pin_hash, ts),
        )

    def set_phase(self, phase):
        self._exec("UPDATE room SET phase = ? WHERE id = 1", (phase,))

    def set_team_count(self, n):
        self._exec("UPDATE room SET team_count = ? WHERE id = 1", (n,))

    def set_settings(self, edge_marks, allow_flips):
        self._exec(
            "UPDATE room SET edge_marks = ?, allow_flips = ? WHERE id = 1",
            (1 if edge_marks else 0, 1 if allow_flips else 0),
        )

    def set_current_round(self, idx):
        self._exec("UPDATE room SET current_round = ? WHERE id = 1", (idx,))

    def reset_to_lobby(self):
        """End the game but keep the crowd: colors and ready flags clear,
        rounds wiped, answers and players stay."""
        self._exec("UPDATE room SET phase = 'lobby', current_round = -1 WHERE id = 1")
        self._exec("UPDATE player SET ready = 0, color_idx = -1")
        self._exec("DELETE FROM round")

    def close_room(self):
        self._exec("DELETE FROM room")
        self._exec("DELETE FROM player")
        self._exec("DELETE FROM answer")
        self._exec("DELETE FROM round")

    # ---------------------------------------------------------- players

    def upsert_player(self, session_id, pid, name, ts):
        """New players insert; returning players keep ready/color, refresh name."""
        existing = self._query(
            "SELECT pid FROM player WHERE session_id = ? AND pid = ?", (session_id, pid)
        )
        if existing:
            self._exec(
                "UPDATE player SET name = ? WHERE session_id = ? AND pid = ?",
                (name, session_id, pid),
            )
        else:
            self._exec(
                "INSERT INTO player (session_id, pid, name, ready, color_idx, joined_at) "
                "VALUES (?, ?, ?, 0, -1, ?)",
                (session_id, pid, name, ts),
            )

    def get_player(self, session_id, pid):
        rows = self._query(
            "SELECT pid, name, ready, color_idx FROM player WHERE session_id = ? AND pid = ?",
            (session_id, pid),
        )
        if not rows:
            return None
        r = rows[0]
        return {"pid": r[0], "name": r[1], "ready": bool(r[2]), "color_idx": int(r[3])}

    def list_players(self, session_id):
        rows = self._query(
            "SELECT pid, name, ready, color_idx FROM player WHERE session_id = ? ORDER BY joined_at ASC",
            (session_id,),
        )
        return [
            {"pid": r[0], "name": r[1], "ready": bool(r[2]), "color_idx": int(r[3])}
            for r in rows
        ]

    def set_ready(self, session_id, pid, ready):
        self._exec(
            "UPDATE player SET ready = ? WHERE session_id = ? AND pid = ?",
            (1 if ready else 0, session_id, pid),
        )

    def set_color(self, session_id, pid, color_idx):
        self._exec(
            "UPDATE player SET color_idx = ? WHERE session_id = ? AND pid = ?",
            (color_idx, session_id, pid),
        )

    # ---------------------------------------------------------- answers

    def replace_answers(self, session_id, answers):
        self._exec("DELETE FROM answer WHERE session_id = ?", (session_id,))
        for i, text in enumerate(answers):
            self._exec(
                "INSERT INTO answer (session_id, idx, text) VALUES (?, ?, ?)",
                (session_id, i, text),
            )

    def list_answers(self, session_id):
        rows = self._query(
            "SELECT text FROM answer WHERE session_id = ? ORDER BY idx ASC", (session_id,)
        )
        return [r[0] for r in rows]

    # ------------------------------------------------------------ rounds

    def save_round(self, session_id, idx, payload, ts):
        self._exec(
            "INSERT OR REPLACE INTO round (session_id, idx, payload, made_at) VALUES (?, ?, ?, ?)",
            (session_id, idx, json.dumps(payload), ts),
        )

    def get_round(self, session_id, idx):
        rows = self._query(
            "SELECT payload FROM round WHERE session_id = ? AND idx = ?", (session_id, idx)
        )
        return json.loads(rows[0][0]) if rows else None

    def load_all(self):
        """Load entire game state from DB. Called once at startup to hydrate
        in-memory state. Returns (room, players, answers, rounds_dict)."""
        room = self.get_room()
        players, answers, rounds = [], [], {}
        if room:
            players = self.list_players(room["session_id"])
            answers = self.list_answers(room["session_id"])
            rows = self._query(
                "SELECT idx, payload FROM round WHERE session_id = ? ORDER BY idx",
                (room["session_id"],),
            )
            rounds = {int(r[0]): json.loads(r[1]) for r in rows}
        return room, players, answers, rounds

