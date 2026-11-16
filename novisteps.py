import json
import re
import sqlite3
from contextlib import closing

import aiohttp


NOVISTEPS_DATA_URL = "https://atcoder-novisteps.vercel.app/problems/__data.json"
NOVISTEPS_UPDATE_SECONDS = 24 * 60 * 60
NOVISTEPS_GRADES = {f"Q{i}" for i in range(1, 12)} | {f"D{i}" for i in range(1, 7)}


def parse_novisteps_grades(payload):
    """Read only task IDs and grades from the page's devalue reference graph."""
    if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
        raise ValueError("invalid page data")
    candidates = []
    for node in payload["nodes"]:
        data = node.get("data") if isinstance(node, dict) else None
        if (
            isinstance(data, list)
            and data
            and isinstance(data[0], dict)
            and {"taskResults", "voteResults"} <= data[0].keys()
        ):
            candidates.append(data)
    if len(candidates) != 1:
        raise ValueError("task data node not found or ambiguous")
    data = candidates[0]

    def dereference(index):
        if type(index) is not int or not 0 <= index < len(data):
            raise ValueError("invalid data reference")
        return data[index]

    def task_id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", value):
            raise ValueError("invalid task ID")
        return value

    vote_data = dereference(data[0]["voteResults"])
    if (
        not isinstance(vote_data, list)
        or not vote_data
        or vote_data[0] != "Map"
        or len(vote_data) % 2 != 1
    ):
        raise ValueError("invalid vote map")
    votes = {}
    for key, value in zip(vote_data[1::2], vote_data[2::2]):
        problem_id = task_id(dereference(key))
        vote = dereference(value)
        if not isinstance(vote, dict) or not {"taskId", "grade"} <= vote.keys():
            raise ValueError("invalid vote record")
        grade = dereference(vote["grade"])
        if (
            dereference(vote["taskId"]) != problem_id
            or not isinstance(grade, str)
            or grade not in NOVISTEPS_GRADES
        ):
            raise ValueError("invalid vote grade")
        if problem_id in votes and votes[problem_id] != grade:
            raise ValueError("conflicting vote grades")
        votes[problem_id] = grade

    tasks = dereference(data[0]["taskResults"])
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("empty or invalid task list")
    confirmed = {}
    for reference in tasks:
        task = dereference(reference)
        if not isinstance(task, dict) or not {"task_id", "grade"} <= task.keys():
            raise ValueError("invalid task record")
        problem_id = task_id(dereference(task["task_id"]))
        grade = dereference(task["grade"])
        if not isinstance(grade, str) or grade not in NOVISTEPS_GRADES | {"PENDING"}:
            raise ValueError("invalid task grade")
        if problem_id in confirmed and confirmed[problem_id] != grade:
            raise ValueError("conflicting task grades")
        confirmed[problem_id] = grade

    result = {}
    for problem_id, grade in confirmed.items():
        provisional = grade == "PENDING"
        if provisional:
            grade = votes.get(problem_id)
        if grade is not None:
            result[problem_id] = {"grade": grade, "provisional": provisional}
    if not result:
        raise ValueError("no usable grades")
    return result


async def get_novisteps_grades(session, db_path, current_time):
    with closing(sqlite3.connect(db_path)) as conn, conn:
        # Claim the daily attempt before HTTP so failures and restarts cannot flood the site.
        conn.execute("BEGIN IMMEDIATE")
        last_attempt, payload_json = conn.execute(
            "SELECT last_attempt_at, payload_json FROM novisteps_cache WHERE id = 1"
        ).fetchone()
        try:
            cached = json.loads(payload_json)
        except ValueError:
            cached = {}
        if last_attempt is not None and current_time < last_attempt + NOVISTEPS_UPDATE_SECONDS:
            return cached
        conn.execute(
            "UPDATE novisteps_cache SET last_attempt_at = ? WHERE id = 1",
            (current_time,),
        )

    try:
        # shortcut: this is internal page data; replace the parser when a public API exists.
        async with session.get(
            NOVISTEPS_DATA_URL, timeout=aiohttp.ClientTimeout(total=15)
        ) as response:
            if response.status != 200:
                raise ValueError(f"status={response.status}")
            grades = parse_novisteps_grades(await response.json())
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE novisteps_cache SET fetched_at = ?, payload_json = ? WHERE id = 1
                """,
                (current_time, json.dumps(grades, separators=(",", ":"))),
            )
        print(f"NoviStepsの級段を更新しました: count={len(grades)}")
        return grades
    except Exception as error:
        print(f"NoviStepsの級段更新に失敗しました: error={error}")
        return cached
