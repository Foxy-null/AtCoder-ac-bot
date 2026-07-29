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


def get_subscriptions_for_channels(channel_ids, db_path=DB_PATH):
    channel_ids = tuple(channel_ids)
    if not channel_ids:
        return []

    placeholders = ", ".join("?" for _ in channel_ids)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            f"""
            SELECT id, discord_id, atcoder_handle, channel_id
            FROM subscriptions
            WHERE channel_id IN ({placeholders})
            ORDER BY atcoder_handle COLLATE NOCASE, channel_id, id
            """,
            channel_ids,
        ).fetchall()


def delete_subscriptions(
    subscriptions,
    discord_id=None,
    db_path=DB_PATH,
    unlinked_only=False,
):
    subscriptions = tuple(subscriptions)
    if not subscriptions:
        return 0

    if unlinked_only:
        query = (
            "DELETE FROM subscriptions "
            "WHERE id = ? AND channel_id = ? AND discord_id IS NULL"
        )
        parameters = subscriptions
    elif discord_id is None:
        query = "DELETE FROM subscriptions WHERE id = ? AND channel_id = ?"
        parameters = subscriptions
    else:
        query = (
            "DELETE FROM subscriptions "
            "WHERE id = ? AND channel_id = ? AND discord_id = ?"
        )
        parameters = [
            (subscription_id, channel_id, discord_id)
            for subscription_id, channel_id in subscriptions
        ]

    with sqlite3.connect(db_path) as conn:
        before = conn.total_changes
        conn.executemany(query, parameters)
        return conn.total_changes - before


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
