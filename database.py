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
        if "subscriptions" in tables:
            return False

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
        return True
