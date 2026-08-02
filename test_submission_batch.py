import unittest
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
    async def test_fetches_one_batch(self):
        session = Session([[submission(1, 101)]])

        result = await main.fetch_submissions_since(session, 100)

        self.assertEqual(result, [submission(1, 101)])
        self.assertEqual(session.urls, [main.ATCODER_SUBMISSIONS_URL.format(from_second=100)])

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
            result = await main.fetch_submissions_since(session, 100)

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
                main.ATCODER_SUBMISSIONS_URL.format(from_second=100),
                main.ATCODER_SUBMISSIONS_URL.format(from_second=102),
                main.ATCODER_SUBMISSIONS_URL.format(from_second=103),
            ],
        )
        self.assertEqual(sleep.await_count, 2)

    async def test_stops_without_advancing_when_page_cannot_progress(self):
        session = Session(
            [[submission(1, 100), submission(2, 100)]]
        )
        with patch.object(main, "ATCODER_SUBMISSION_PAGE_LIMIT", 2):
            result = await main.fetch_submissions_since(session, 100)

        self.assertIsNone(result)

    def test_groups_only_registered_handles_case_insensitively(self):
        alice = submission(1, 101, "Alice")
        bob = submission(2, 102, "bob")

        grouped = main.group_registered_submissions(
            [alice, bob],
            ["ALICE"],
        )

        self.assertEqual(grouped, {"alice": [alice]})

    async def test_retries_waiting_submission_until_it_becomes_ac(self):
        waiting = {
            **submission(1, 101),
            "contest_id": "abc001",
            "problem_id": "abc001_a",
            "language": "Python",
            "result": "WJ",
        }
        accepted = {**waiting, "result": "AC"}
        unrelated = submission(2, 102, "bob")
        poll_time = 100
        channel = SimpleNamespace(send=AsyncMock())

        def get_poll_time():
            return poll_time

        def set_poll_time(value):
            nonlocal poll_time
            poll_time = value

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
                        atcoder_handle,
                        channel_id,
                        last_checked_time
                    )
                    VALUES ('alice', 10, 100)
                    """
                )

            fetch = AsyncMock(side_effect=[[waiting, unrelated], [accepted]])
            with (
                patch.object(main, "DB_PATH", db_path),
                patch.object(main.aiohttp, "ClientSession", ClientSession),
                patch.object(main, "get_submission_poll_time", get_poll_time),
                patch.object(main, "set_submission_poll_time", set_poll_time),
                patch.object(main, "fetch_submissions_since", fetch),
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
                self.assertEqual(poll_time, 101)
                channel.send.assert_not_awaited()

                await main.check_ac_submissions.coro()

        self.assertEqual(poll_time, 101)
        channel.send.assert_awaited_once()
        self.assertEqual(
            [call.args[1] for call in fetch.await_args_list],
            [100, 101],
        )


if __name__ == "__main__":
    unittest.main()
