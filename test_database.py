import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import init_db


class InitDbTest(unittest.TestCase):
    def test_migrates_users_only_when_subscriptions_is_created(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE users (
                        discord_id INTEGER PRIMARY KEY,
                        atcoder_handle TEXT NOT NULL,
                        channel_id INTEGER NOT NULL,
                        last_submission_id INTEGER,
                        last_checked_time INTEGER
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO users VALUES (?, ?, ?, ?, ?)",
                    (1, "alice", 101, 1001, 10001),
                )

            self.assertTrue(init_db(db_path))
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE subscriptions SET last_submission_id = 2001"
                )
                conn.execute(
                    "INSERT INTO users VALUES (?, ?, ?, ?, ?)",
                    (2, "bob", 102, 1002, 10002),
                )

            self.assertFalse(init_db(db_path))
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT atcoder_handle, last_submission_id
                    FROM subscriptions
                    """
                ).fetchall()

            self.assertEqual(rows, [("alice", 2001)])


if __name__ == "__main__":
    unittest.main()
