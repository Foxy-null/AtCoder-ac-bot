import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import discord

import main
from test_submission_batch import Session


class PendingDifficultyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = Path(self.directory.name) / "bot.db"
        main.init_db(self.db_path)
        patcher = patch.object(main, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO subscriptions (atcoder_handle, channel_id) VALUES (?, 10)",
                [("alice",), ("bob",)],
            )
        self.edit = AsyncMock()
        self.channel = SimpleNamespace(
            get_partial_message=Mock(return_value=SimpleNamespace(edit=self.edit))
        )

    def queue(self, message_id=101, subscription_id=1, deadline=2000):
        embed = discord.Embed(
            title="A. Example AC",
            url="https://atcoder.jp/contests/abc999/tasks/abc999_a",
            description=(
                "[🔎提出](https://atcoder.jp/contests/abc999/submissions/100) | "
                "diff: 未確定 | NoviSteps: "
                "[1D](https://atcoder-novisteps.vercel.app/votes/abc999_a)"
            ),
            color=main.difficulty_to_color(1200),
        )
        embed.set_author(name="Alice", url="https://atcoder.jp/users/alice")
        embed.set_footer(text="提出日時: 2026-11-21 12:00:00 | Python")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO pending_difficulty_notifications "
                "(message_id, subscription_id, problem_id, deadline, embed_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (message_id, subscription_id, "abc999_a", deadline, json.dumps(embed.to_dict())),
            )
        return embed.to_dict()

    def pending_count(self):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT count(*) FROM pending_difficulty_notifications"
            ).fetchone()[0]

    async def update(self, models, now=1000):
        await main.update_pending_difficulties(
            None, {"difficulty": models}, {10: self.channel}, now
        )

    async def test_updates_all_original_messages_preserving_novisteps_and_layout(self):
        original = self.queue()
        self.queue(message_id=102, subscription_id=2)
        session = Session([[], {"abc999_a": {"difficulty": 0}}, [], []])
        with patch.object(main.asyncio, "sleep", AsyncMock()):
            await main.update_pending_difficulties(session, {}, {10: self.channel}, 1000)
        self.assertEqual(session.urls, [
            main.ATCODER_PROBLEMS_URL, main.ATCODER_DIFFICULTY_URL,
            main.ATCODER_CONTESTS_URL, main.ATCODER_CONTEST_PROBLEMS_URL,
        ])
        await self.update({"abc999_a": {"difficulty": 0}})

        self.assertEqual(self.pending_count(), 0)
        self.assertEqual(self.channel.get_partial_message.call_args_list, [
            call(101), call(102),
        ])
        self.assertEqual(self.edit.await_count, 2)
        expected = {
            **original,
            "description": original["description"].replace("diff: 未確定", "diff: 148"),
            "color": 0x808080,
        }
        for edit_call in self.edit.await_args_list:
            self.assertEqual(edit_call.kwargs["embed"].to_dict(), expected)

    async def test_expiry_ends_monitoring_despite_outage_or_late_difficulty(self):
        for models in (None, {"abc999_a": {"difficulty": 800}}):
            with self.subTest(models=models):
                original = self.queue()
                self.edit.reset_mock()
                await self.update(models, now=2000)
                self.edit.assert_awaited_once()
                self.assertEqual(self.pending_count(), 0)
                self.assertEqual(self.edit.await_args.kwargs["embed"].to_dict(), {
                    **original,
                    "description": original["description"].replace("diff: 未確定", "diff: 不明"),
                })

    async def test_missing_or_invalid_models_are_retried_without_editing(self):
        self.queue()
        for model in (None, {}, [], {"difficulty": None}, {"difficulty": True},
                      {"difficulty": "800"}, {"difficulty": float("inf")}):
            with self.subTest(model=model):
                await self.update(None if model is None else {"abc999_a": model})
                self.edit.assert_not_awaited()
                self.assertEqual(self.pending_count(), 1)
        await self.update({"abc999_a": {"difficulty": 1200}})
        self.edit.assert_awaited_once()
        self.assertEqual(self.pending_count(), 0)

    async def test_retries_transient_edits_and_discards_deleted_or_forbidden_messages(self):
        models = {"abc999_a": {"difficulty": 800}}
        for first_models, now, expected in ((models, 1000, "800"), (None, 2000, "不明")):
            with self.subTest(result=expected):
                self.queue()
                self.edit.reset_mock()
                self.edit.side_effect = RuntimeError("offline")
                await self.update(first_models, now=now)
                self.assertEqual(self.pending_count(), 1)

                main.init_db(self.db_path)
                self.edit.side_effect = None
                await self.update({"abc999_a": {"difficulty": 1200}}, now=3000)
                self.assertEqual(self.pending_count(), 0)
                self.assertEqual(self.edit.await_count, 2)
                self.assertIn(f"diff: {expected}", self.edit.await_args.kwargs["embed"].description)

        for status, exception in ((404, discord.NotFound), (403, discord.Forbidden)):
            with self.subTest(status=status):
                self.queue()
                self.edit.reset_mock()
                self.edit.side_effect = exception(
                    SimpleNamespace(status=status, reason="test"), "test"
                )
                await self.update(models)
                await self.update(models)
                self.edit.assert_awaited_once()
                self.assertEqual(self.pending_count(), 0)


if __name__ == "__main__":
    unittest.main()
