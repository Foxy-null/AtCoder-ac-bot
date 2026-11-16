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

    def get(self, url, **kwargs):
        self.urls.append(url)
        page = next(self.pages)
        if isinstance(page, Exception):
            raise page
        return page if isinstance(page, Response) else Response(page)


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


def contest(contest_id, end_time, rate_change="~ 1999"):
    return {
        "id": contest_id,
        "title": contest_id,
        "start_epoch_second": end_time - 6000,
        "duration_second": 6000,
        "rate_change": rate_change,
    }


class SubmissionBatchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.novisteps = AsyncMock(return_value={})
        patcher = patch.object(main, "get_novisteps_grades", self.novisteps)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_difficulty_wait_uses_earliest_contest_end_and_only_unprocessed_models(self):
        deadline = 1000 + main.DIFFICULTY_WAIT_SECONDS
        for model, ends, now, expected in (
            (None, [1000], deadline - 1, True),
            (None, [1000], deadline, False),
            (None, [1000], deadline + 1, False),
            (None, [2000, 1000], deadline, False),
            (None, [], deadline - 1, False),
            ({}, [1000], deadline - 1, False),
            ({"difficulty": None}, [1000], deadline - 1, False),
        ):
            with self.subTest(model=model, ends=ends, now=now):
                self.assertEqual(
                    main.should_wait_for_difficulty(model, ends, now), expected
                )

    async def test_loads_catalog_once_per_poll_and_retries_failed_resources_next_poll(self):
        problem = {
            "id": "abc999_a",
            "contest_id": "abc999",
            "title": "A. Problem",
        }
        pair = {"problem_id": "abc999_a", "contest_id": "abc999"}
        models = {"abc999_a": {"difficulty": 800}}
        session = Session([
            [problem], Response(None, status=503), [contest("abc999", 7000)], [pair],
            [problem], models, [contest("abc999", 7000)], [pair],
        ])
        failed = {}
        recovered = {}
        with patch.object(main.asyncio, "sleep", new=AsyncMock()):
            await main.get_problem_catalog(session, failed)
            await main.get_problem_catalog(session, failed)
            self.assertEqual(len(session.urls), 4)
            await main.get_problem_catalog(session, recovered)

        self.assertIsNone(failed["difficulty"])
        self.assertEqual(recovered["difficulty"], models)
        self.assertEqual(recovered["problems"], {("abc999", "abc999_a"): "A. Problem"})
        self.assertEqual(recovered["contest_problems"], {"abc999_a": {"abc999"}})
        self.assertEqual(session.urls, [
            main.ATCODER_PROBLEMS_URL,
            main.ATCODER_DIFFICULTY_URL,
            main.ATCODER_CONTESTS_URL,
            main.ATCODER_CONTEST_PROBLEMS_URL,
        ] * 2)

    async def test_rejects_invalid_catalog_shapes(self):
        for resource, invalid in (
            ("difficulty", []),
            ("contests", [{"id": "broken"}]),
            ("contest_problems", {}),
        ):
            with self.subTest(resource=resource):
                cache = {name: {} for name in (
                    "problems", "difficulty", "contests", "contest_problems"
                ) if name != resource}
                with patch.object(main.asyncio, "sleep", new=AsyncMock()):
                    await main.get_problem_catalog(Session([invalid]), cache)
                self.assertIsNone(cache[resource])

    async def test_reads_official_contest_kind_without_guessing_from_sponsor_name(self):
        page = """
            <tr><td><span title="Heuristic">Ⓗ</span>
                <a href="/contests/other-final">Other</a></td></tr>
            <tr><td><span title='Algorithm'>Ⓐ</span>
                <a href='/contests/sponsor-final'>Sponsor</a></td></tr>
            <tr><td><span title="Heuristic">Ⓗ</span>
                <a href="/contests/marathon-final">Marathon</a></td></tr>
        """
        session = Session([page, page, page])
        cache = {}
        for contest_id, expected in (
            ("sponsor-final", "Algorithm"),
            ("marathon-final", "Heuristic"),
            ("unlisted-final", "Unknown"),
        ):
            with self.subTest(contest_id=contest_id):
                info = contest(contest_id, 7000)
                self.assertEqual(await main.get_contest_kind(session, info, cache), expected)
                self.assertEqual(await main.get_contest_kind(session, info, cache), expected)
        self.assertEqual(len(session.urls), 3)

    async def test_retries_contest_kind_fetch_failure_on_next_poll(self):
        info = contest("sponsor-final", 7000)
        session = Session([
            RuntimeError("unavailable"),
            '<tr><td><span title="Algorithm">Ⓐ</span>'
            '<a href="/contests/sponsor-final">Sponsor</a></td></tr>',
        ])
        cache = {}
        self.assertIsNone(await main.get_contest_kind(session, info, cache))
        self.assertIsNone(await main.get_contest_kind(session, info, cache))
        self.assertEqual(len(session.urls), 1)
        self.assertEqual(await main.get_contest_kind(session, info, {}), "Algorithm")

    async def test_known_unavailable_model_does_not_need_contest_information(self):
        cache = {
            "problems": {},
            "difficulty": {"abc999_a": {}},
            "contests": None,
            "contest_problems": None,
        }
        result = await main.resolve_problem_difficulty(
            None,
            {"contest_id": "abc999", "problem_id": "abc999_a"},
            cache,
            1000,
        )
        self.assertEqual(result, (True, None))

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
            cache["problems"] = {}
            cache["difficulty"] = {}
            cache.update(contests={}, contest_problems={})

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
        last_embed = channel.send.await_args_list[-1].kwargs["embed"]
        self.assertTrue(last_embed.title.startswith("abc001 abc001_a"))
        self.assertIn("難易度: 不明", last_embed.description)
        self.assertEqual(notified, [(100,), (200,)])
        self.assertEqual(legacy_cursor, (50, 100))
        self.assertEqual(
            [call.args for call in fetch.await_args_list],
            [(ANY, "alice", 100)] * 4,
        )

    async def test_notifies_wrong_answers_only_when_enabled(self):
        wrong = {
            **submission(100, 101),
            "contest_id": "abc001",
            "problem_id": "abc001_a",
            "language": "Python",
            "result": "WA",
        }
        waiting = {**wrong, "id": 101, "result": "WJ"}
        ac_only_channel = SimpleNamespace(send=AsyncMock())
        all_results_channel = SimpleNamespace(send=AsyncMock())

        async def populate_catalog(session, cache):
            cache["problems"] = {("abc001", "abc001_a"): "A"}
            cache["difficulty"] = {"abc001_a": {"difficulty": 800}}
            cache.update(contests={}, contest_problems={})

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot.db"
            main.init_db(db_path)
            with sqlite3.connect(db_path) as conn:
                conn.executemany(
                    """
                    INSERT INTO subscriptions (
                        atcoder_handle,
                        channel_id,
                        last_checked_time,
                        notify_wrong_answers
                    )
                    VALUES ('alice', ?, 100, ?)
                    """,
                    [(10, False), (20, True)],
                )

            with (
                patch.object(main, "DB_PATH", db_path),
                patch.object(main.aiohttp, "ClientSession", ClientSession),
                patch.object(main.time, "time", return_value=200),
                patch.object(
                    main,
                    "fetch_user_submissions_since",
                    new=AsyncMock(return_value=[wrong, waiting]),
                ),
                patch.object(
                    main,
                    "get_accessible_channels",
                    new=AsyncMock(
                        return_value=(
                            {
                                10: ac_only_channel,
                                20: all_results_channel,
                            },
                            set(),
                        )
                    ),
                ),
                patch.object(main, "get_problem_catalog", populate_catalog),
                patch.object(
                    main,
                    "resolve_author",
                    new=AsyncMock(return_value=("Alice", "avatar", "profile")),
                ),
            ):
                await main.check_ac_submissions.coro()

        ac_only_channel.send.assert_not_awaited()
        all_results_channel.send.assert_awaited_once()
        embed = all_results_channel.send.await_args.kwargs["embed"]
        self.assertEqual(embed.title, "A <:WA:1537153868134096976>")
        self.assertEqual(
            embed.footer.text,
            "提出日時: 1970-01-01 09:01:41 | Python",
        )

    async def test_waits_for_new_contest_difficulty_and_notifies_when_available(self):
        accepted = {
            **submission(100, 1000),
            "contest_id": "abc001",
            "problem_id": "abc001_a",
            "language": "Python",
            "result": "AC",
        }
        channel = SimpleNamespace(send=AsyncMock())
        catalogs = iter(
            [
                ({}, None),
                ({("abc001", "abc001_a"): "A"}, {}),
                (
                    {("abc001", "abc001_a"): "A"},
                    {"abc001_a": {"difficulty": -201}},
                ),
            ]
        )

        async def populate_catalog(session, cache):
            cache["problems"], cache["difficulty"] = next(catalogs)
            cache["contests"] = {"abc001": contest("abc001", 1100)}
            cache["contest_problems"] = {"abc001_a": {"abc001"}}

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
                        last_checked_time
                    )
                    VALUES (1, 'alice', 10, 900)
                    """
                )

            with (
                patch.object(main, "DB_PATH", db_path),
                patch.object(main.aiohttp, "ClientSession", ClientSession),
                patch.object(
                    main.time,
                    "time",
                    return_value=(
                        1100 + main.DIFFICULTY_WAIT_SECONDS - 1
                    ),
                ),
                patch.object(
                    main,
                    "fetch_user_submissions_since",
                    new=AsyncMock(return_value=[accepted]),
                ),
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
                channel.send.assert_not_awaited()
                with sqlite3.connect(db_path) as conn:
                    self.assertEqual(conn.execute(
                        "SELECT count(*) FROM notified_submissions"
                    ).fetchone()[0], 0)

                await main.check_ac_submissions.coro()

            with sqlite3.connect(db_path) as conn:
                notified = conn.execute(
                    "SELECT submission_id FROM notified_submissions"
                ).fetchall()

        channel.send.assert_awaited_once()
        self.assertIn(
            "diff: 90",
            channel.send.await_args.kwargs["embed"].description,
        )
        self.assertEqual(notified, [(100,)])

    async def test_notification_policy_for_problem_sources_and_missing_metadata(self):
        now = 1_000_000
        recent_end = now - 3600
        old_end = now - main.DIFFICULTY_WAIT_SECONDS
        archive = """
            <tr><td><span title="Algorithm">Ⓐ</span>
                <a href="/contests/sponsor-final">Sponsor</a></td></tr>
            <tr><td><span title="Heuristic">Ⓗ</span>
                <a href="/contests/marathon-final">Marathon</a></td></tr>
        """
        cases = [
            ("typical90", "typical90_l", None,
             [contest("typical90", 3_153_600_000, "-")], "難易度: 不明"),
            ("awc0001", "awc0001_a", None,
             [contest("awc0001", recent_end, "-")], "難易度: 不明"),
            ("abc010", "abc010_a", None,
             [contest("abc010", old_end)], "難易度: 不明"),
            ("adt_all_recent", "abc010_a", None,
             [contest("abc010", old_end), contest("adt_all_recent", recent_end, "-")],
             "難易度: 不明"),
            ("abc999", "shared_a", None,
             [contest("abc010", old_end), contest("abc999", recent_end)],
             "難易度: 不明"),
            ("abc999", "abc999_a", {},
             [contest("abc999", recent_end)], "難易度: 不明"),
            ("abc999", "abc999_a", {"difficulty": 800},
             [contest("abc999", recent_end)], "diff: 800"),
            ("abs", "abc010_a", {"difficulty": 800, "is_experimental": True},
             [contest("abs", 3_153_600_000, "-"), contest("abc010", old_end, "-")],
             "diff: 800"),
            ("abc999", "abc999_a", {"difficulty": 0},
             [contest("abc999", recent_end)], "diff: 148"),
            ("ahc999", "ahc999_a", {"difficulty": 2300},
             [contest("ahc999", recent_end, "All")], "難易度: 不明"),
            ("ahc999", "ahc999_a", {},
             [contest("ahc999", recent_end, "All")], "難易度: 不明"),
            ("marathon-final", "marathon_a", {"difficulty": 2300},
             [contest("marathon-final", recent_end, "All")], "難易度: 不明"),
            ("marathon-final", "marathon_a", {},
             [contest("marathon-final", recent_end, "All")], "難易度: 不明"),
            ("sponsor-final", "sponsor_a", None,
             [contest("sponsor-final", recent_end)], None),
            ("sponsor-final", "old_unrated_a", None,
             [contest("unrated-original", old_end, "-"), contest("sponsor-final", recent_end)],
             None),
            ("abc999", "abc999_a", None,
             [contest("abc999", recent_end)], None),
            ("abc999", "abc999_a", {"difficulty": "invalid"},
             [contest("abc999", recent_end)], None),
            ("unknown", "unknown_a", None, [], None),
            ("unlisted-final", "unlisted_a", None,
             [contest("unlisted-final", recent_end)], None),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            for index, (cid, pid, model, sources, expected) in enumerate(cases):
                with self.subTest(contest_id=cid, problem_id=pid, model=model):
                    self.novisteps.return_value = (
                        {pid: {"grade": "D1", "provisional": False}}
                        if cid in {"ahc999", "marathon-final", "sponsor-final", "abs"}
                        else {}
                    )
                    db_path = Path(temp_dir) / f"{index}.db"
                    main.init_db(db_path)
                    with sqlite3.connect(db_path) as conn:
                        conn.execute(
                            "INSERT INTO subscriptions "
                            "(discord_id, atcoder_handle, channel_id, last_checked_time) "
                            "VALUES (1, 'alice', 10, ?)", (now - 1000,),
                        )
                    accepted = {
                        **submission(100, now - 1),
                        "contest_id": cid, "problem_id": pid,
                        "language": "Python", "result": "AC",
                    }

                    async def populate_catalog(session, cache):
                        cache.update(
                            problems={},
                            difficulty={} if model is None else {pid: model},
                            contests={c["id"]: c for c in sources},
                            contest_problems={pid: {c["id"] for c in sources}},
                        )

                    client = ClientSession()
                    client.get = lambda url, **kwargs: Response(archive)
                    channel = SimpleNamespace(send=AsyncMock())
                    with (
                        patch.object(main, "DB_PATH", db_path),
                        patch.object(main.aiohttp, "ClientSession", return_value=client),
                        patch.object(main.time, "time", return_value=now) as clock,
                        patch.object(
                            main, "fetch_user_submissions_since",
                            AsyncMock(return_value=[accepted]),
                        ),
                        patch.object(
                            main, "get_accessible_channels",
                            AsyncMock(return_value=({10: channel}, set())),
                        ),
                        patch.object(main, "get_problem_catalog", populate_catalog),
                        patch.object(main, "fetch_problem_title", AsyncMock(return_value="A")),
                        patch.object(
                            main, "resolve_author",
                            AsyncMock(return_value=("Alice", "avatar", "profile")),
                        ),
                    ):
                        await main.check_ac_submissions.coro()
                        await main.check_ac_submissions.coro()
                        expected_count = int(expected is not None)
                        self.assertEqual(channel.send.await_count, expected_count)
                        with sqlite3.connect(db_path) as conn:
                            self.assertEqual(conn.execute(
                                "SELECT count(*) FROM notified_submissions"
                            ).fetchone()[0], expected_count)
                        if expected is not None:
                            embed = channel.send.await_args.kwargs["embed"]
                            self.assertEqual(embed.title, f"A {main.RESULT_EMOJIS['AC']}")
                            self.assertIn(expected, embed.description)
                            if cid in {"ahc999", "marathon-final"}:
                                self.assertNotIn("NoviSteps", embed.description)
                                self.assertEqual(embed.color.value, 0)
                            if cid == "abs":
                                self.assertIn(
                                    "NoviSteps: [1D](https://atcoder-novisteps.vercel.app/votes/abc010_a)",
                                    embed.description,
                                )
                        elif cid in {"unknown", "unlisted-final"}:
                            clock.return_value = now + main.CATALOG_RETRY_SECONDS
                            await main.check_ac_submissions.coro()
                            channel.send.assert_awaited_once()

    async def test_notification_layout_with_independent_difficulty_and_grades(self):
        now = 1000
        items = [
            {
                **submission(100 + index, 999),
                "contest_id": "abc999", "problem_id": f"abc999_{letter.lower()}",
                "language": "Python", "result": result,
            }
            for index, (letter, result) in enumerate((
                ("A", "AC"), ("B", "WA"), ("C", "NEW"), ("D", "AC"),
            ))
        ]
        channel = SimpleNamespace(send=AsyncMock())
        self.novisteps.return_value = {
            "abc999_a": {"grade": "D1", "provisional": False},
            "abc999_c": {"grade": "Q2", "provisional": True},
        }

        async def populate_catalog(session, cache):
            cache.update(
                problems={("abc999", item["problem_id"]): chr(65 + i) for i, item in enumerate(items)},
                difficulty={
                    "abc999_a": {"difficulty": 1703},
                    "abc999_b": {"difficulty": 0},
                    "abc999_c": {}, "abc999_d": {},
                },
                contests={"abc999": contest("abc999", -300000)},
                contest_problems={item["problem_id"]: {"abc999"} for item in items},
            )

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "bot.db"
            main.init_db(db_path)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "INSERT INTO subscriptions "
                    "(atcoder_handle, channel_id, last_checked_time, notify_wrong_answers) "
                    "VALUES ('alice', 10, 900, 1)"
                )
            with (
                patch.object(main, "DB_PATH", db_path),
                patch.object(main.time, "time", return_value=now),
                patch.object(main.aiohttp, "ClientSession", ClientSession),
                patch.object(main, "fetch_user_submissions_since", AsyncMock(return_value=items)),
                patch.object(main, "get_accessible_channels", AsyncMock(return_value=({10: channel}, set()))),
                patch.object(main, "get_problem_catalog", populate_catalog),
                patch.object(main, "fetch_atcoder_profile", AsyncMock(return_value={})),
                patch.object(main, "resolve_author", AsyncMock(return_value=("Alice", "avatar", "profile"))),
            ):
                await main.check_ac_submissions.coro()
            self.novisteps.assert_awaited_once_with(ANY, db_path, now)
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM notified_submissions").fetchone()[0], 4)

        self.assertEqual(channel.send.await_count, 4)
        expected = [
            ("diff: 1703 | NoviSteps: [1D](https://atcoder-novisteps.vercel.app/votes/abc999_a)", 0x3636FC),
            ("diff: 148", 0x808080),
            ("NoviSteps: [2Q](https://atcoder-novisteps.vercel.app/votes/abc999_c)（暫定）", 0x804000),
            ("難易度: 不明", 0),
        ]
        for index, (text, color) in enumerate(expected):
            embed = channel.send.await_args_list[index].kwargs["embed"]
            self.assertEqual(
                embed.description,
                f"[🔎提出](https://atcoder.jp/contests/abc999/submissions/{100 + index}) | {text}",
            )
            self.assertEqual(embed.footer.text, "提出日時: 1970-01-01 09:16:39 | Python")
            self.assertNotIn("Python", embed.description)
            self.assertEqual(embed.color.value, color)
        self.assertEqual(channel.send.await_args_list[2].kwargs["embed"].title, "C NEW")


if __name__ == "__main__":
    unittest.main()
