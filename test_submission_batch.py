import unittest
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import main


class Response:
    def __init__(self, submissions, status=200):
        self.status = status
        self.submissions = submissions

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return self.submissions

    async def text(self):
        return self.submissions


class Session:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return Response(next(self.pages))


class ClientSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def submission(submission_id, epoch_second, user_id="alice"):
    return {
        "id": submission_id,
        "epoch_second": epoch_second,
        "user_id": user_id,
    }


class SubmissionBatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_and_normalizes_problem_title(self):
        session = Session(
            ["<title>A-Final - Trees &amp; Queries - System Test</title>"]
        )

        result = await main.fetch_problem_title(session, "problem-url")

        self.assertEqual(result, "A-Final. Trees & Queries - System Test")
        self.assertEqual(session.urls, ["problem-url"])

    async def test_fetches_one_batch(self):
        session = Session([[submission(1, 101)]])

        result = await main.fetch_user_submissions_since(
            session,
            "alice",
            100,
        )

        self.assertEqual(result, [submission(1, 101)])
        self.assertEqual(
            session.urls,
            [
                main.ATCODER_USER_SUBMISSIONS_URL.format(
                    handle="alice",
                    from_second=100,
                )
            ],
        )

    async def test_paginates_and_deduplicates_boundary(self):
        pages = [
            [submission(1, 101), submission(2, 102)],
            [submission(2, 102), submission(3, 103)],
            [submission(3, 103)],
        ]
        session = Session(pages)
        with (
            patch.object(main, "ATCODER_SUBMISSION_PAGE_LIMIT", 2),
            patch.object(main.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            result = await main.fetch_user_submissions_since(
                session,
                "alice",
                100,
            )

        self.assertEqual(
            result,
            [
                submission(1, 101),
                submission(2, 102),
                submission(3, 103),
            ],
        )
        self.assertEqual(
            session.urls,
            [
                main.ATCODER_USER_SUBMISSIONS_URL.format(
                    handle="alice",
                    from_second=100,
                ),
                main.ATCODER_USER_SUBMISSIONS_URL.format(
                    handle="alice",
                    from_second=102,
                ),
                main.ATCODER_USER_SUBMISSIONS_URL.format(
                    handle="alice",
                    from_second=103,
                ),
            ],
        )
        self.assertEqual(sleep.await_count, 2)

    async def test_stops_without_advancing_when_page_cannot_progress(self):
        session = Session(
            [[submission(1, 100), submission(2, 100)]]
        )
        with patch.object(main, "ATCODER_SUBMISSION_PAGE_LIMIT", 2):
            result = await main.fetch_user_submissions_since(
                session,
                "alice",
                100,
            )

        self.assertIsNone(result)

    async def test_retries_send_and_notifies_late_lower_id_once(self):
        high = {
            **submission(200, 102),
            "contest_id": "abc001",
            "problem_id": "abc001_a",
            "language": "Python",
            "result": "AC",
        }
        waiting = {**high, "id": 100, "epoch_second": 101, "result": "WJ"}
        accepted = {**waiting, "result": "AC"}
        channel = SimpleNamespace(
            send=AsyncMock(side_effect=[RuntimeError("down"), None, None])
        )

        async def populate_catalog(session, cache):
            cache["problems"] = {("abc001", "abc001_a"): "A"}
            cache["difficulty"] = {}

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot.db"
            main.init_db(db_path)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO subscriptions (
                        discord_id,
                        atcoder_handle,
                        channel_id,
                        last_submission_id,
                        last_checked_time
                    )
                    VALUES (1, 'alice', 10, 50, 100)
                    """
                )

            fetch = AsyncMock(
                side_effect=[
                    [high],
                    [waiting, high],
                    [accepted, high],
                    [accepted, high],
                ]
            )
            with (
                patch.object(main, "DB_PATH", db_path),
                patch.object(main.aiohttp, "ClientSession", ClientSession),
                patch.object(
                    main.time,
                    "time",
                    return_value=main.SUBMISSION_LOOKBACK_SECONDS + 100,
                ),
                patch.object(main, "fetch_user_submissions_since", fetch),
                patch.object(
                    main,
                    "get_accessible_channels",
                    new=AsyncMock(return_value=({10: channel}, set())),
                ),
                patch.object(main, "get_problem_catalog", populate_catalog),
                patch.object(
                    main,
                    "resolve_author",
                    new=AsyncMock(return_value=("Alice", "avatar", "profile")),
                ),
            ):
                await main.check_ac_submissions.coro()
                await main.check_ac_submissions.coro()
                await main.check_ac_submissions.coro()
                await main.check_ac_submissions.coro()

            with sqlite3.connect(db_path) as conn:
                notified = conn.execute(
                    """
                    SELECT submission_id
                    FROM notified_submissions
                    ORDER BY submission_id
                    """
                ).fetchall()
                legacy_cursor = conn.execute(
                    """
                    SELECT last_submission_id, last_checked_time
                    FROM subscriptions
                    """
                ).fetchone()

        self.assertEqual(channel.send.await_count, 3)
        self.assertEqual(notified, [(100,), (200,)])
        self.assertEqual(legacy_cursor, (50, 100))
        self.assertEqual(
            [call.args for call in fetch.await_args_list],
            [(ANY, "alice", 100)] * 4,
        )


if __name__ == "__main__":
    unittest.main()
