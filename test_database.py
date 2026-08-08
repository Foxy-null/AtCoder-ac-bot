import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
from database import (
    delete_subscriptions,
    delete_subscriptions_for_channel,
    get_subscriptions_for_channels,
    get_submission_poll_time,
    init_db,
    set_submission_poll_time,
)


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

    def test_deletes_every_subscription_for_one_channel(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot.db"
            init_db(db_path)
            with sqlite3.connect(db_path) as conn:
                conn.executemany(
                    """
                    INSERT INTO subscriptions (
                        atcoder_handle,
                        channel_id
                    )
                    VALUES (?, ?)
                    """,
                    [
                        ("alice", 101),
                        ("bob", 101),
                        ("carol", 102),
                    ],
                )

            self.assertEqual(
                delete_subscriptions_for_channel(101, db_path),
                2,
            )
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT atcoder_handle, channel_id FROM subscriptions"
                ).fetchall()

            self.assertEqual(rows, [("carol", 102)])

    def test_initializes_and_updates_submission_poll_time(self):
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
                conn.executemany(
                    "INSERT INTO users VALUES (?, ?, ?, ?, ?)",
                    [
                        (1, "alice", 101, 1001, 10001),
                        (2, "bob", 102, 1002, 10002),
                    ],
                )

            init_db(db_path)
            self.assertEqual(get_submission_poll_time(db_path), 10002)

            set_submission_poll_time(20001, db_path)
            self.assertEqual(get_submission_poll_time(db_path), 20001)

    def test_rewinds_existing_delivery_cursor_once_for_reconciliation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot.db"
            init_db(db_path)
            with sqlite3.connect(db_path) as conn:
                conn.execute("DROP TABLE notified_submissions")
                conn.executemany(
                    """
                    INSERT INTO subscriptions (
                        atcoder_handle,
                        channel_id,
                        last_submission_id,
                        last_checked_time
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        ("alice", 101, 50, 200),
                        ("bob", 102, None, 200),
                    ],
                )

            with patch.object(
                database.time,
                "time",
                return_value=database.SUBMISSION_LOOKBACK_SECONDS + 100,
            ):
                init_db(db_path)
                init_db(db_path)

            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT atcoder_handle, last_checked_time
                    FROM subscriptions
                    ORDER BY atcoder_handle
                    """
                ).fetchall()

            self.assertEqual(rows, [("alice", 100), ("bob", 200)])

    def test_lists_by_channel_and_deletes_only_the_owner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot.db"
            init_db(db_path)
            with sqlite3.connect(db_path) as conn:
                conn.executemany(
                    """
                    INSERT INTO subscriptions (
                        discord_id,
                        atcoder_handle,
                        channel_id
                    )
                    VALUES (?, ?, ?)
                    """,
                    [
                        (1, "alice", 101),
                        (2, "bob", 101),
                        (1, "carol", 102),
                        (None, "dave", 103),
                    ],
                )

            rows = get_subscriptions_for_channels([101, 102], db_path)
            self.assertEqual(
                [
                    (row["discord_id"], row["atcoder_handle"], row["channel_id"])
                    for row in rows
                ],
                [(1, "alice", 101), (2, "bob", 101), (1, "carol", 102)],
            )

            targets = [(row["id"], row["channel_id"]) for row in rows]
            self.assertEqual(delete_subscriptions(targets, 1, db_path), 2)

            remaining = get_subscriptions_for_channels([101, 102, 103], db_path)
            self.assertEqual(
                [
                    (row["discord_id"], row["atcoder_handle"])
                    for row in remaining
                ],
                [(2, "bob"), (None, "dave")],
            )

            targets = [
                (row["id"], row["channel_id"])
                for row in remaining
            ]
            self.assertEqual(
                delete_subscriptions(
                    targets,
                    db_path=db_path,
                    unlinked_only=True,
                ),
                1,
            )
            remaining = get_subscriptions_for_channels([101, 102, 103], db_path)
            self.assertEqual(
                [
                    (row["discord_id"], row["atcoder_handle"])
                    for row in remaining
                ],
                [(2, "bob")],
            )


if __name__ == "__main__":
    unittest.main()
