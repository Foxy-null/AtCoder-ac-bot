import unittest
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


if __name__ == "__main__":
    unittest.main()
