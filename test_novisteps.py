import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import main
from database import init_db
from novisteps import (
    NOVISTEPS_DATA_URL,
    NOVISTEPS_UPDATE_SECONDS,
    get_novisteps_grades,
    parse_novisteps_grades,
)
from test_submission_batch import Response, Session


def synthetic_page():
    """Build fictional page data; never commit a downloaded NoviSteps dataset."""
    data = [{}]

    def add(value):
        data.append(value)
        return len(data) - 1

    tasks = []
    for problem_id, grade in (
        ("example_a", "Q4"),
        ("example_b", "PENDING"),
        ("example_c", "PENDING"),
        ("example_a", "Q4"),
    ):
        tasks.append(add({"task_id": add(problem_id), "grade": add(grade), "user_id": -1}))
    votes = ["Map"]
    for problem_id, grade in (("example_a", "D2"), ("example_b", "Q2")):
        votes.extend((
            add(problem_id),
            add({"taskId": add(problem_id), "grade": add(grade), "isExperimental": add(False)}),
        ))
    data[0] = {"taskResults": add(tasks), "voteResults": add(votes)}
    return {"type": "data", "nodes": [None, {"data": [{"layout": 1}, False]}, {"data": data}]}


class NoviStepsParserTest(unittest.TestCase):
    def test_confirmed_grade_wins_and_pending_vote_is_provisional(self):
        self.assertEqual(parse_novisteps_grades(synthetic_page()), {
            "example_a": {"grade": "Q4", "provisional": False},
            "example_b": {"grade": "Q2", "provisional": True},
        })

    def test_rejects_invalid_graphs_instead_of_saving_partial_data(self):
        original = synthetic_page()
        variants = [None, {}, {"nodes": []}]
        for invalid in (-1, True, 9999):
            page = copy.deepcopy(original)
            page["nodes"][-1]["data"][0]["taskResults"] = invalid
            variants.append(page)
        for invalid in ("Q12", [], None):
            page = copy.deepcopy(original)
            data = page["nodes"][-1]["data"]
            first = data[data[0]["taskResults"]][0]
            data[data[first]["grade"]] = invalid
            variants.append(page)
        page = copy.deepcopy(original)
        data = page["nodes"][-1]["data"]
        data[data[0]["voteResults"]][0] = "Set"
        variants.append(page)
        page = copy.deepcopy(original)
        data = page["nodes"][-1]["data"]
        first = data[data[0]["taskResults"]][0]
        data[data[first]["task_id"]] = "bad/id"
        variants.append(page)
        page = copy.deepcopy(original)
        data = page["nodes"][-1]["data"]
        duplicate = data[data[0]["taskResults"]][-1]
        data[data[duplicate]["grade"]] = "D1"
        variants.append(page)
        page = copy.deepcopy(original)
        data = page["nodes"][-1]["data"]
        data[data[0]["taskResults"]].clear()
        variants.append(page)
        page = copy.deepcopy(original)
        page["nodes"].append(copy.deepcopy(page["nodes"][-1]))
        variants.append(page)
        for index, invalid in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(ValueError):
                parse_novisteps_grades(invalid)


class NoviStepsCacheTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = Path(self.directory.name) / "bot.db"
        init_db(self.db_path)

    async def test_daily_cache_survives_reinitialization_and_failed_refreshes(self):
        session = Session([synthetic_page()])
        grades = await get_novisteps_grades(session, self.db_path, 1000)
        self.assertEqual(session.urls, [NOVISTEPS_DATA_URL])
        init_db(self.db_path)
        no_requests = Session([])
        for now in (1001, 1000 + NOVISTEPS_UPDATE_SECONDS - 1):
            self.assertEqual(await get_novisteps_grades(no_requests, self.db_path, now), grades)
        self.assertEqual(no_requests.urls, [])

        for attempt, response in enumerate((Response(None, status=503), {"nodes": []}), 1):
            now = 1000 + attempt * NOVISTEPS_UPDATE_SECONDS
            failed = Session([response])
            self.assertEqual(await get_novisteps_grades(failed, self.db_path, now), grades)
            self.assertEqual(failed.urls, [NOVISTEPS_DATA_URL])
            self.assertEqual(await get_novisteps_grades(no_requests, self.db_path, now + 60), grades)
            with sqlite3.connect(self.db_path) as conn:
                last_attempt, fetched_at, payload = conn.execute(
                    "SELECT last_attempt_at, fetched_at, payload_json FROM novisteps_cache"
                ).fetchone()
            self.assertEqual((last_attempt, fetched_at), (now, 1000))
            self.assertEqual(json.loads(payload), grades)
            self.assertNotIn("user_id", payload)
        self.assertEqual(no_requests.urls, [])

        recovered_at = 1000 + 3 * NOVISTEPS_UPDATE_SECONDS
        self.assertEqual(
            await get_novisteps_grades(Session([synthetic_page()]), self.db_path, recovered_at),
            grades,
        )
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT fetched_at FROM novisteps_cache").fetchone()[0], recovered_at)

    async def test_initial_failure_returns_empty_cache_without_repeated_requests(self):
        failed = Session([RuntimeError("offline")])
        self.assertEqual(await get_novisteps_grades(failed, self.db_path, 0), {})
        no_requests = Session([])
        self.assertEqual(await get_novisteps_grades(no_requests, self.db_path, 60), {})
        self.assertEqual(no_requests.urls, [])
        recovered = await get_novisteps_grades(
            Session([synthetic_page()]), self.db_path, NOVISTEPS_UPDATE_SECONDS
        )
        self.assertIn("example_a", recovered)


class DifficultyDisplayTest(unittest.TestCase):
    def test_independent_sources_links_provisional_labels_and_color_priority(self):
        grade = {"grade": "D1", "provisional": False}
        link = "NoviSteps: [1D](https://atcoder-novisteps.vercel.app/votes/original_task)"
        for difficulty, supplemental, expected, color in (
            (1703, grade, f"diff: 1703 | {link}", main.difficulty_to_color(1703)),
            (0, None, "diff: 0", main.difficulty_to_color(0)),
            (None, grade, link, main.difficulty_to_color(1200)),
            (None, {**grade, "provisional": True}, link + "（暫定）", main.difficulty_to_color(1200)),
            (None, None, "難易度: 不明", main.difficulty_to_color(None)),
        ):
            with self.subTest(difficulty=difficulty, grade=supplemental):
                self.assertEqual(
                    main.build_difficulty_display(difficulty, supplemental, "original_task", "A"),
                    (expected, color),
                )

    def test_all_grade_colors_and_official_stars_as_last_resort(self):
        expected_levels = {
            **{f"Q{i}": 0 for i in range(3, 12)},
            "Q2": 1, "Q1": 2,
            "D1": 3, "D2": 4, "D3": 5, "D4": 6, "D5": 7, "D6": 7,
        }
        for grade, level in expected_levels.items():
            with self.subTest(grade=grade):
                text, color = main.build_difficulty_display(
                    None, {"grade": grade, "provisional": False}, "example_a", "A"
                )
                self.assertNotIn("diff:", text)
                self.assertEqual(color, main.difficulty_to_color(level * 400))
        for star, threshold in ((1, 0), (2, 0), (3, 400), (4, 800), (5, 1200), (6, 1600), (7, 2000)):
            self.assertEqual(
                main.build_difficulty_display(None, None, "typical90_example", f"Example（★{star}）"),
                ("難易度: 不明", main.difficulty_to_color(threshold)),
            )
        grade = {"grade": "Q2", "provisional": False}
        self.assertEqual(
            main.build_difficulty_display(None, grade, "typical90_example", "Example（★4）")[1],
            main.difficulty_to_color(400),
        )


if __name__ == "__main__":
    unittest.main()
