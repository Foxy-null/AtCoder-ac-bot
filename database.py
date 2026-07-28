import sqlite3


DB_PATH = "bot.db"


def init_db(db_path=DB_PATH):
    with sqlite3.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        created = "subscriptions" not in tables
        if created:
            conn.execute(
                """
                CREATE TABLE subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id INTEGER,
                    atcoder_handle TEXT NOT NULL,
                    channel_id INTEGER NOT NULL,
                    last_submission_id INTEGER,
                    last_checked_time INTEGER,
                    UNIQUE(atcoder_handle, channel_id)
                )
                """
            )
            if "users" in tables:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO subscriptions (
                        discord_id,
                        atcoder_handle,
                        channel_id,
                        last_submission_id,
                        last_checked_time
                    )
                    SELECT
                        discord_id,
                        atcoder_handle,
                        channel_id,
                        last_submission_id,
                        last_checked_time
                    FROM users
                    """
                )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submission_poll_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_checked_time INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO submission_poll_state (id, last_checked_time)
            SELECT
                1,
                COALESCE(MAX(last_checked_time), unixepoch() - 60)
            FROM subscriptions
            """
        )
        return created


def delete_subscriptions_for_channel(channel_id, db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "DELETE FROM subscriptions WHERE channel_id = ?",
            (channel_id,),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def get_submission_poll_time(db_path=DB_PATH):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT last_checked_time FROM submission_poll_state WHERE id = 1"
        ).fetchone()[0]


def set_submission_poll_time(last_checked_time, db_path=DB_PATH):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE submission_poll_state
            SET last_checked_time = ?
            WHERE id = 1
            """,
            (last_checked_time,),
        )
