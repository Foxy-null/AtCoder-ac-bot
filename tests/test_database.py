import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import init_db


USERS_SCHEMA = """
CREATE TABLE users (
    discord_id INTEGER PRIMARY KEY,
    atcoder_handle TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    last_submission_id INTEGER,
    last_checked_time INTEGER
)
"""


class InitDbTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bot.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def connect(self):
        return sqlite3.connect(self.db_path)

    def test_migrates_users_when_subscriptions_is_created(self):
        with self.connect() as conn:
            conn.execute(USERS_SCHEMA)
            conn.executemany(
                """
                INSERT INTO users VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (1, "alice", 101, 1001, 10001),
                    (2, "bob", 102, None, 10002),
                ],
            )

        self.assertTrue(init_db(self.db_path))

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT discord_id, atcoder_handle, channel_id,
                       last_submission_id, last_checked_time
                FROM subscriptions
                ORDER BY discord_id
                """
            ).fetchall()

        self.assertEqual(
            rows,
            [
                (1, "alice", 101, 1001, 10001),
                (2, "bob", 102, None, 10002),
            ],
        )

    def test_does_not_copy_users_again_when_subscriptions_exists(self):
        with self.connect() as conn:
            conn.execute(USERS_SCHEMA)
            conn.execute(
                "INSERT INTO users VALUES (?, ?, ?, ?, ?)",
                (1, "alice", 101, 1001, 10001),
            )

        init_db(self.db_path)

        with self.connect() as conn:
            conn.execute(
                """
                UPDATE subscriptions
                SET last_submission_id = ?, last_checked_time = ?
                WHERE atcoder_handle = ? AND channel_id = ?
                """,
                (2001, 20001, "alice", 101),
            )
            conn.execute(
                "INSERT INTO users VALUES (?, ?, ?, ?, ?)",
                (2, "bob", 102, 1002, 10002),
            )

        self.assertFalse(init_db(self.db_path))

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT atcoder_handle, last_submission_id, last_checked_time
                FROM subscriptions
                """
            ).fetchall()

        self.assertEqual(rows, [("alice", 2001, 20001)])

    def test_fresh_database_does_not_create_legacy_users_table(self):
        self.assertTrue(init_db(self.db_path))

        with self.connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                    """
                )
            }

        self.assertIn("subscriptions", tables)
        self.assertNotIn("users", tables)


if __name__ == "__main__":
    unittest.main()
