import sqlite3


DB_PATH = "bot.db"


def _table_exists(cursor, table_name):
    return (
        cursor.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        ).fetchone()
        is not None
    )


def init_db(db_path=DB_PATH):
    with sqlite3.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()

        if _table_exists(cursor, "subscriptions"):
            return False

        cursor.execute(
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

        if _table_exists(cursor, "users"):
            cursor.execute(
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

        return True
