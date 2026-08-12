import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

import main


def channel_with_permissions(**overrides):
    permissions = {
        "view_channel": True,
        "send_messages": True,
        "embed_links": True,
        **overrides,
    }
    channel = Mock()
    channel.guild.me = object()
    channel.permissions_for.return_value = SimpleNamespace(**permissions)
    return channel


def discord_error(error_type, status):
    response = SimpleNamespace(status=status, reason="test")
    return error_type(response, {"message": "test", "code": 0})


class HttpClient:
    def __init__(self, status=200):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, url):
        return self


def register_interaction(channel, guild_id=None):
    return SimpleNamespace(
        channel=channel,
        guild_id=guild_id,
        response=SimpleNamespace(
            send_message=AsyncMock(),
            defer=AsyncMock(),
        ),
        edit_original_response=AsyncMock(),
    )


class ChannelAccessTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = HttpClient()
        self.client_session_patch = patch.object(
            main.aiohttp,
            "ClientSession",
            return_value=self.session,
        )
        self.client_session_patch.start()
        self.addCleanup(self.client_session_patch.stop)

    async def test_registers_current_dm_when_channel_is_omitted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot.db"
            main.init_db(db_path)
            channel = Mock(spec=discord.DMChannel)
            channel.id = 101
            interaction = register_interaction(channel)

            with patch.object(main, "DB_PATH", db_path):
                await main.register.callback(interaction, "alice")

            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT discord_id, atcoder_handle, channel_id,
                           notify_wrong_answers
                    FROM subscriptions
                    """
                ).fetchone()

        self.assertEqual(row, (None, "alice", 101, 0))
        interaction.response.defer.assert_awaited_once_with(thinking=True)
        interaction.edit_original_response.assert_awaited_once_with(
            content=(
                "Discordユーザーなしで AtCoderのハンドル 「alice」 を登録しました！\n"
                "ACをした際の通知は このDM に送信されます。"
            )
        )

    async def test_rejects_server_without_bot(self):
        channel = Mock(spec=discord.TextChannel)
        interaction = register_interaction(channel, guild_id=202)

        with patch.object(main.bot, "get_guild", return_value=None) as get_guild:
            await main.register.callback(interaction, "alice")

        get_guild.assert_called_once_with(202)
        interaction.response.send_message.assert_awaited_once_with(
            "このサーバーにはBot本体が参加していません。"
            "Botをサーバーに追加してから再試行してください。",
            ephemeral=True,
        )
        interaction.response.defer.assert_not_awaited()

    def test_register_is_guild_install_only(self):
        command = main.bot.tree.get_command("register")

        self.assertTrue(command.allowed_installs.guild)
        self.assertFalse(command.allowed_installs.user)

    async def test_registers_wrong_answer_notifications(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot.db"
            main.init_db(db_path)
            channel = Mock(spec=discord.DMChannel)
            channel.id = 101
            interaction = register_interaction(channel)

            with patch.object(main, "DB_PATH", db_path):
                await main.register.callback(
                    interaction,
                    "alice",
                    notify_wrong_answers=True,
                )

            with sqlite3.connect(db_path) as conn:
                enabled = conn.execute(
                    "SELECT notify_wrong_answers FROM subscriptions"
                ).fetchone()[0]

        self.assertEqual(enabled, 1)
        self.assertIn(
            "ACと不正解の通知は このDM に送信されます。",
            interaction.edit_original_response.await_args.kwargs["content"],
        )

    async def test_updates_existing_registration_and_reports_all_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot.db"
            main.init_db(db_path)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO subscriptions (
                        discord_id, atcoder_handle, channel_id,
                        notify_wrong_answers
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (1, "alice", 101, True),
                )
            channel = Mock(spec=discord.DMChannel)
            channel.id = 101
            interaction = register_interaction(channel)

            with patch.object(main, "DB_PATH", db_path):
                await main.register.callback(interaction, "alice")

            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT discord_id, notify_wrong_answers
                    FROM subscriptions
                    """
                ).fetchall()

        self.assertEqual(rows, [(None, 0)])
        interaction.edit_original_response.assert_awaited_once_with(
            content=(
                "AtCoderのハンドル 「alice」 の既存登録を更新しました！\n"
                "Discordユーザー: なし\n"
                "通知対象: ACのみ\n"
                "通知先: このDM"
            )
        )

    async def test_rejects_nonexistent_atcoder_handle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot.db"
            main.init_db(db_path)
            self.session.status = 404
            channel = Mock(spec=discord.DMChannel)
            channel.id = 101
            interaction = register_interaction(channel)

            with patch.object(main, "DB_PATH", db_path):
                await main.register.callback(interaction, "missing-user")

            with sqlite3.connect(db_path) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM subscriptions"
                ).fetchone()[0]

        self.assertEqual(count, 0)
        interaction.edit_original_response.assert_awaited_once_with(
            content="AtCoderのハンドル 「missing-user」 は存在しません。"
        )

    async def test_does_not_register_when_atcoder_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot.db"
            main.init_db(db_path)
            self.session.status = 500
            channel = Mock(spec=discord.DMChannel)
            channel.id = 101
            interaction = register_interaction(channel)

            with patch.object(main, "DB_PATH", db_path):
                await main.register.callback(interaction, "alice")

            with sqlite3.connect(db_path) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM subscriptions"
                ).fetchone()[0]

        self.assertEqual(count, 0)
        interaction.edit_original_response.assert_awaited_once_with(
            content=(
                "AtCoderユーザーを確認できませんでした。"
                "時間をおいて再試行してください。"
            )
        )

    async def test_checks_each_channel_once(self):
        channel = channel_with_permissions()
        with (
            patch.object(main.bot, "get_channel", return_value=channel) as get_channel,
            patch.object(main.bot, "fetch_channel", new=AsyncMock()) as fetch_channel,
            patch.object(main, "unregister_channel") as unregister,
        ):
            channels, retry_channel_ids = await main.get_accessible_channels(
                [101, 101]
            )

        self.assertEqual(channels, {101: channel})
        self.assertEqual(retry_channel_ids, set())
        get_channel.assert_called_once_with(101)
        fetch_channel.assert_not_awaited()
        unregister.assert_not_called()

    async def test_deletes_channel_without_send_permission(self):
        channel = channel_with_permissions(send_messages=False)
        with (
            patch.object(main.bot, "get_channel", return_value=channel),
            patch.object(main, "unregister_channel") as unregister,
        ):
            channels, retry_channel_ids = await main.get_accessible_channels([101])

        self.assertEqual(channels, {})
        self.assertEqual(retry_channel_ids, set())
        unregister.assert_called_once_with(101, "missing_permissions")

    async def test_deletes_channel_not_found_by_discord(self):
        error = discord_error(discord.NotFound, 404)
        with (
            patch.object(main.bot, "get_channel", return_value=None),
            patch.object(
                main.bot,
                "fetch_channel",
                new=AsyncMock(side_effect=error),
            ),
            patch.object(main, "unregister_channel") as unregister,
        ):
            channels, retry_channel_ids = await main.get_accessible_channels([101])

        self.assertEqual(channels, {})
        self.assertEqual(retry_channel_ids, set())
        unregister.assert_called_once_with(101, error)

    async def test_keeps_registration_after_temporary_error(self):
        error = discord_error(discord.HTTPException, 500)
        with (
            patch.object(main.bot, "get_channel", return_value=None),
            patch.object(
                main.bot,
                "fetch_channel",
                new=AsyncMock(side_effect=error),
            ),
            patch.object(main, "unregister_channel") as unregister,
        ):
            channels, retry_channel_ids = await main.get_accessible_channels([101])

        self.assertEqual(channels, {})
        self.assertEqual(retry_channel_ids, {101})
        unregister.assert_not_called()


if __name__ == "__main__":
    unittest.main()
