"""SQLite persistence for room messages.

The database is the source of truth for chat history. Recalls are soft deletes
so a reconnecting client's last seen msg_id can still be resolved.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


class ChatDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.connection = sqlite3.connect(str(self.path), timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id TEXT NOT NULL UNIQUE,
                room TEXT NOT NULL,
                name TEXT NOT NULL,
                ip TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                time TEXT NOT NULL,
                edited INTEGER NOT NULL DEFAULT 0,
                recalled INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER,
                recalled_at INTEGER
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_room_seq "
            "ON messages(room, seq)"
        )
        self.connection.commit()

    @staticmethod
    def _to_message(row: sqlite3.Row) -> dict:
        return {
            "msg_id": row["msg_id"],
            "created_at": row["created_at"],
            "time": row["time"],
            "name": row["name"],
            "ip": row["ip"],
            "text": row["text"],
            "edited": bool(row["edited"]),
        }

    def add_message(
        self,
        *,
        msg_id: str,
        room: str,
        name: str,
        ip: str,
        text: str,
        created_at: int,
        time_text: str,
    ) -> dict:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO messages
                    (msg_id, room, name, ip, text, created_at, time)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (msg_id, room, name, ip, text, created_at, time_text),
            )
        return {
            "msg_id": msg_id,
            "created_at": created_at,
            "time": time_text,
            "name": name,
            "ip": ip,
            "text": text,
            "edited": False,
        }

    def get_history(
        self,
        room: str,
        last_msg_id: str | None = None,
        *,
        recent_limit: int = 50,
        replay_limit: int = 500,
    ) -> list[dict]:
        """Return recent room messages plus all visible messages after msg_id.

        On a first visit this is the latest ``recent_limit`` messages. On a
        reconnect, missed messages after the client's last known msg_id are
        merged in, while recalled messages stay hidden.
        """
        recent_rows = self.connection.execute(
            """
            SELECT * FROM messages
            WHERE room = ? AND recalled = 0
            ORDER BY seq DESC
            LIMIT ?
            """,
            (room, recent_limit),
        ).fetchall()

        rows_by_id = {row["msg_id"]: row for row in recent_rows}

        if last_msg_id:
            last_row = self.connection.execute(
                """
                SELECT seq FROM messages
                WHERE room = ? AND msg_id = ?
                """,
                (room, last_msg_id),
            ).fetchone()

            if last_row is not None:
                missed_rows = self.connection.execute(
                    """
                    SELECT * FROM messages
                    WHERE room = ? AND seq > ? AND recalled = 0
                    ORDER BY seq ASC
                    LIMIT ?
                    """,
                    (room, last_row["seq"], replay_limit),
                ).fetchall()
                for row in missed_rows:
                    rows_by_id.setdefault(row["msg_id"], row)

        ordered = sorted(rows_by_id.values(), key=lambda row: row["seq"])
        return [self._to_message(row) for row in ordered]

    def get_message(self, room: str, msg_id: str) -> dict | None:
        row = self.connection.execute(
            """
            SELECT * FROM messages
            WHERE room = ? AND msg_id = ?
            """,
            (room, msg_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def edit_message(
        self,
        room: str,
        msg_id: str,
        text: str,
        updated_at: int,
    ) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE messages
                SET text = ?, edited = 1, updated_at = ?
                WHERE room = ? AND msg_id = ? AND recalled = 0
                """,
                (text, updated_at, room, msg_id),
            )
        return cursor.rowcount > 0

    def recall_message(self, room: str, msg_id: str, recalled_at: int) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE messages
                SET recalled = 1, recalled_at = ?
                WHERE room = ? AND msg_id = ? AND recalled = 0
                """,
                (recalled_at, room, msg_id),
            )
        return cursor.rowcount > 0

    def visible_count(self, room: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS amount FROM messages WHERE room = ? AND recalled = 0",
            (room,),
        ).fetchone()
        return int(row["amount"])

    def close(self) -> None:
        self.connection.close()
